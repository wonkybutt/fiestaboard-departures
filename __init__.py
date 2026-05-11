"""Departures plugin for FiestaBoard.

Displays upcoming calendar events from an iCalendar feed in a departures-board style.
Each event row shows the event name, Go/No-Go indicators, and a day/hour countdown.
Supports cycling through all events N rows at a time on a configurable interval.
"""

import logging
import math
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from icalendar import Calendar

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

# 2026-05-10: boolean indicators use a denylist — anything not in _FALSY is true
_FALSY = {"no", "false", "0"}

# 2026-05-09: Vestaboard color tile markers for Go/No-Go indicators
_TILE_GREEN = "{66}"  # go / confirmed
_TILE_RED = "{63}"    # no-go / missing or invalid

# Default indicator keys
_DEFAULT_INDICATORS = ["FLIGHT", "STAY", "PAID"]


def _normalize_url(url: str) -> str:
    """Rewrite webcal:// to https:// for HTTP transport."""
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    if url.startswith("webcal:"):
        return "https:" + url[len("webcal:"):]
    return url


def _is_falsy(val: str) -> bool:
    """Return True if val represents a negative/empty value for a boolean indicator."""
    return val.strip().lower() in _FALSY or val.strip() == ""


def _build_row(event: Dict[str, Any], tz: ZoneInfo, now_dt: datetime) -> str:
    """Build a single 22-tile display row for an event.

    Name width scales with indicator count (n):
      n=1: name 16 tiles — {name:<16} {i1}{days:>4}
      n=2: name 15 tiles — {name:<15} {i1}{i2}{days:>4}
      n=3: name 14 tiles — {name:<14} {i1}{i2}{i3}{days:>4}
    Departed format: {name:<13} DEPARTED  (22 tiles, no indicators)
    Color markers count as 1 tile but are 4 characters in string length.
    """
    days_str = _format_countdown(event, tz, now_dt)
    # 2026-05-09: departed events show NAME DEPARTED with no indicator tiles
    if days_str == "DPTD":
        name = event["name"][:13]
        return f"{name:<13} DEPARTED"

    tiles = event["indicator_tiles"]
    n = len(tiles)
    # 2026-05-10: name width = 17 - n (1→16, 2→15, 3→14)
    name_width = 17 - n
    name = event["name"][:name_width]
    indicator_str = "".join(tiles)
    return f"{name:<{name_width}} {indicator_str}{days_str:>4}"


def _format_countdown(event: Dict[str, Any], tz: ZoneInfo, now_dt: datetime) -> str:
    """Return the countdown string for an event.

    If the event has a parsed time_value (from first indicator), uses that datetime
    for hours countdown (< 24h) or days. Without a time value, uses whole days only.
    """
    time_value = event["time_value"]
    if time_value is not None:
        event_datetime = datetime.combine(
            event["date"], time_value
        ).replace(tzinfo=tz)
        delta_seconds = (event_datetime - now_dt).total_seconds()
        if delta_seconds <= 0:
            return "DPTD"
        elif delta_seconds < 86400:
            return f"{int(delta_seconds / 3600)}H"
        else:
            return f"{int(delta_seconds / 86400)}D"
    else:
        days_delta = event["days_delta"]
        return "DPTD" if days_delta <= 0 else f"{days_delta}D"


class DeparturesPlugin(PluginBase):
    """Departures-board display of upcoming iCalendar events.

    Each event shows its name, configurable Go/No-Go indicators, and a countdown.
    The first indicator expects a 24h time value (HHMM or HH:MM) and drives the
    hour countdown. Additional indicators use a denylist: any value other than
    no/false/0/blank shows green. Missing indicators default to red.

    Events beyond lookback_days in the past are hidden. All future events
    are tracked. The display cycles through events display_rows at a time,
    advancing every cycle_seconds seconds.
    """

    def __init__(self, manifest: Dict[str, Any]) -> None:
        super().__init__(manifest)
        # 2026-05-09: cycling state — persists across fetch calls in memory
        self._cycle_page: int = 0
        self._cycle_last_advance: Optional[datetime] = None

    @property
    def plugin_id(self) -> str:
        return "departures"

    def fetch_data(self) -> PluginResult:
        """Fetch and parse the ICS feed, returning per-event and cycling row variables."""
        try:
            calendar_url = self.config.get("calendar_url", "")
            if not calendar_url:
                return PluginResult(available=False, error="Calendar URL not configured")

            calendar_url = _normalize_url(calendar_url)
            lookback_days = int(self.config.get("lookback_days", 3))
            display_rows = max(1, int(self.config.get("display_rows", 1)))
            cycle_seconds = max(30, int(self.config.get("cycle_seconds", 300)))
            timezone_str = self.config.get("timezone", "UTC")

            # 2026-05-10: configurable Go/No-Go indicator keys (1–3 separate fields)
            # If no indicator fields are present → use all defaults.
            # If any indicator field is present → only include explicitly set (non-empty) ones.
            _indicator_fields = ("indicator_1", "indicator_2", "indicator_3")
            any_configured = any(f in self.config for f in _indicator_fields)
            indicator_keys = []
            for field, default in zip(_indicator_fields, _DEFAULT_INDICATORS):
                if not any_configured:
                    indicator_keys.append(default)
                else:
                    val = str(self.config.get(field, "")).strip().upper()
                    if field == "indicator_1":
                        indicator_keys.append(val if val else default)
                    elif val:
                        indicator_keys.append(val)

            try:
                tz = ZoneInfo(timezone_str)
            except (ZoneInfoNotFoundError, ValueError):
                return PluginResult(
                    available=False, error=f"Invalid timezone: {timezone_str}"
                )

            try:
                response = requests.get(
                    calendar_url,
                    timeout=10,
                    headers={"User-Agent": "FiestaBoard/1.0"},
                )
                response.raise_for_status()
                ics_text = response.text
            except requests.RequestException as e:
                logger.warning("Network error fetching ICS feed: %s", e)
                return PluginResult(available=False, error=f"Network error: {e}")

            try:
                cal = Calendar.from_ical(ics_text)
            except Exception as e:
                logger.warning("Failed to parse ICS feed: %s", e)
                return PluginResult(available=False, error=f"ICS parse error: {e}")

            today = self._get_today(tz)
            now_dt = self._get_now(tz)
            raw_events: List[Dict[str, Any]] = []

            for component in cal.walk():
                if component.name != "VEVENT":
                    continue

                dtstart = component.get("DTSTART")
                if dtstart is None:
                    continue

                event_dt = self._normalize_dtstart(dtstart)
                if event_dt is None:
                    continue

                event_date = event_dt.astimezone(tz).date()
                days_delta = (event_date - today).days

                if days_delta < -lookback_days:
                    continue

                summary = str(component.get("SUMMARY", "")).strip()
                description = str(component.get("DESCRIPTION", "")).strip()
                parsed = self._parse_indicators(description, indicator_keys)

                raw_events.append({
                    "date": event_date,
                    "days_delta": days_delta,
                    "name": summary,
                    "time_value": parsed["time_value"],
                    "indicator_tiles": parsed["indicator_tiles"],
                })

            raw_events.sort(key=lambda e: e["date"])
            events = raw_events

            # -- Cycling row variables --
            data: Dict[str, Any] = {"event_count": len(events)}
            # 2026-05-09: advance page when cycle_seconds has elapsed
            total_pages = max(1, math.ceil(len(events) / display_rows)) if events else 1
            wall_now = datetime.now()

            if self._cycle_last_advance is None:
                self._cycle_last_advance = wall_now
                self._cycle_page = 0
            elif (wall_now - self._cycle_last_advance).total_seconds() >= cycle_seconds:
                self._cycle_page = (self._cycle_page + 1) % total_pages
                self._cycle_last_advance = wall_now

            # Clamp page in case event count shrank since last fetch
            self._cycle_page = self._cycle_page % total_pages

            start_idx = self._cycle_page * display_rows
            page_events = events[start_idx:start_idx + display_rows]

            data["current_page"] = self._cycle_page + 1
            data["total_pages"] = total_pages

            for slot in range(display_rows):
                key = f"row_{slot + 1}"
                if slot < len(page_events):
                    data[key] = _build_row(page_events[slot], tz, now_dt)
                else:
                    data[key] = ""

            return PluginResult(
                available=True,
                data=data,
                formatted_lines=self._format_display(events, tz, now_dt),
            )

        except Exception as e:
            logger.exception("Unexpected error in Departures plugin")
            return PluginResult(available=False, error=str(e))

    def _get_today(self, tz: ZoneInfo) -> date:
        """Return the current date in the given timezone."""
        return datetime.now(tz).date()

    def _get_now(self, tz: ZoneInfo) -> datetime:
        """Return the current datetime in the given timezone."""
        return datetime.now(tz)

    def _normalize_dtstart(self, dtstart: Any) -> Optional[datetime]:
        """Convert a DTSTART property value to a timezone-aware UTC datetime."""
        try:
            dt = dtstart.dt
            if isinstance(dt, datetime):
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            elif isinstance(dt, date):
                return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        except Exception as e:
            logger.debug("Could not normalize DTSTART: %s", e)
        return None

    def _parse_flight_time(self, val: str) -> Optional[time]:
        """Parse a 24h time string in HHMM or HH:MM format.

        Returns a time object on success, None for any invalid input.
        Accepted formats: '1018', '2033', '13:37', '00:00'.
        """
        val = val.strip()
        try:
            if len(val) == 5 and val[2] == ":":
                h, m = int(val[:2]), int(val[3:])
            elif len(val) == 4:
                h, m = int(val[:2]), int(val[2:])
            else:
                return None
            if 0 <= h <= 23 and 0 <= m <= 59:
                return time(h, m)
        except ValueError:
            pass
        return None

    def _parse_indicators(
        self, description: str, indicator_keys: List[str]
    ) -> Dict[str, Any]:
        """Parse Go/No-Go indicators from an event description.

        The first key expects a 24h time value (HHMM or HH:MM); green if valid,
        red if missing or unparseable. Subsequent keys use a denylist: any value
        other than no/false/0/blank is green; missing defaults to red.

        Returns:
            time_value: Optional[time] from the first indicator
            indicator_tiles: List[str] of _TILE_GREEN/_TILE_RED, one per key
        """
        # 2026-05-10: build lookup from description tokens
        token_map: Dict[str, str] = {}
        for token in description.split():
            if ":" in token:
                k, _, v = token.partition(":")
                token_map[k.upper()] = v

        time_value: Optional[time] = None
        indicator_tiles: List[str] = []

        for idx, key in enumerate(indicator_keys):
            val = token_map.get(key, "")
            if idx == 0:
                # First indicator: expects a 24h time
                parsed_time = self._parse_flight_time(val) if val else None
                time_value = parsed_time
                indicator_tiles.append(_TILE_GREEN if parsed_time is not None else _TILE_RED)
            else:
                # Subsequent indicators: denylist logic
                indicator_tiles.append(_TILE_RED if _is_falsy(val) else _TILE_GREEN)

        return {"time_value": time_value, "indicator_tiles": indicator_tiles}

    def _format_display(
        self,
        events: List[Dict[str, Any]],
        tz: ZoneInfo,
        now_dt: datetime,
    ) -> List[str]:
        """Format the first 6 events as board rows for formatted_lines output."""
        lines = [_build_row(e, tz, now_dt) for e in events[:6]]
        while len(lines) < 6:
            lines.append("")
        return lines


Plugin = DeparturesPlugin
