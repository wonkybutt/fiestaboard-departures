"""Departures plugin for FiestaBoard.

Displays upcoming calendar events from an iCalendar feed in a departures-board style.
Each event row shows the event name, FSP status indicators, and a day/hour countdown.
"""

import logging
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from icalendar import Calendar

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

_TRUTHY = {"yes", "true", "1"}

# 2026-05-09: Vestaboard color tile markers for FLIGHT indicator
_TILE_GREEN = "{66}"  # valid flight time present
_TILE_RED = "{63}"    # missing or invalid flight time


def _normalize_url(url: str) -> str:
    """Rewrite webcal:// to https:// for HTTP transport."""
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    if url.startswith("webcal:"):
        return "https:" + url[len("webcal:"):]
    return url


class DeparturesPlugin(PluginBase):
    """Departures-board display of upcoming iCalendar events.

    Each event shows its name, FSP status indicators, and a countdown.
    FLIGHT accepts a 24h time (HHMM or HH:MM); a green tile indicates a parsed
    time, red indicates missing or invalid. Events beyond lookback_days in the
    past are hidden; all future events are tracked without a cap.
    """

    @property
    def plugin_id(self) -> str:
        return "departures"

    def fetch_data(self) -> PluginResult:
        """Fetch and parse the ICS feed, returning per-event countdown variables."""
        try:
            calendar_url = self.config.get("calendar_url", "")
            if not calendar_url:
                return PluginResult(available=False, error="Calendar URL not configured")

            calendar_url = _normalize_url(calendar_url)
            # 2026-05-09: removed max_events cap — all future events are tracked
            # 2026-05-09: lookback_days replaces hardcoded 3-day past cutoff
            lookback_days = int(self.config.get("lookback_days", 3))
            timezone_str = self.config.get("timezone", "UTC")

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

                # Exclude events beyond the configured lookback window
                if days_delta < -lookback_days:
                    continue

                summary = str(component.get("SUMMARY", "")).strip()
                description = str(component.get("DESCRIPTION", "")).strip()
                flags = self._parse_flags(description)

                raw_events.append({
                    "date": event_date,
                    "days_delta": days_delta,
                    "name": summary,
                    "flight_time": flags["flight_time"],
                    "stay": flags["stay"],
                    "paid": flags["paid"],
                })

            raw_events.sort(key=lambda e: e["date"])
            events = raw_events

            data: Dict[str, Any] = {"event_count": len(events)}
            for i, event in enumerate(events):
                days_str = self._format_countdown(event, tz, now_dt)
                f_char = _TILE_GREEN if event["flight_time"] is not None else _TILE_RED
                s_char = "S" if event["stay"] else "-"
                p_char = "P" if event["paid"] else "-"
                flight_time_str = (
                    event["flight_time"].strftime("%H:%M")
                    if event["flight_time"] is not None
                    else ""
                )
                data[f"event_{i}_name"] = event["name"][:12]
                data[f"event_{i}_days"] = days_str
                data[f"event_{i}_f_char"] = f_char
                data[f"event_{i}_flight_time"] = flight_time_str
                data[f"event_{i}_stay"] = "true" if event["stay"] else "false"
                data[f"event_{i}_paid"] = "true" if event["paid"] else "false"

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

    def _format_countdown(
        self, event: Dict[str, Any], tz: ZoneInfo, now_dt: datetime
    ) -> str:
        """Return the countdown string for an event.

        If the event has a parsed flight time, uses that datetime for hours
        countdown (< 24h) or days. Without a flight time, uses whole days only.
        """
        flight_time = event["flight_time"]
        if flight_time is not None:
            event_datetime = datetime.combine(
                event["date"], flight_time
            ).replace(tzinfo=tz)
            delta_seconds = (event_datetime - now_dt).total_seconds()
            if delta_seconds <= 0:
                return "DPRTD"
            elif delta_seconds < 86400:
                return f"{int(delta_seconds / 3600)}H"
            else:
                return f"{int(delta_seconds / 86400)}D"
        else:
            days_delta = event["days_delta"]
            return "DPRTD" if days_delta <= 0 else f"{days_delta}D"

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

    def _parse_flags(self, description: str) -> Dict[str, Any]:
        """Parse FSP flags from an event description.

        FLIGHT accepts a 24h time (HHMM or HH:MM); invalid values yield None.
        STAY and PAID accept yes/true/1 (case-insensitive); missing defaults to False.
        """
        flags: Dict[str, Any] = {"flight_time": None, "stay": False, "paid": False}
        if not description:
            return flags
        for token in description.split():
            if ":" not in token:
                continue
            key, _, val = token.partition(":")
            key_lower = key.lower()
            if key_lower == "flight":
                flags["flight_time"] = self._parse_flight_time(val)
            elif key_lower in ("stay", "paid"):
                flags[key_lower] = val.lower() in _TRUTHY
        return flags

    def _format_display(
        self,
        events: List[Dict[str, Any]],
        tz: ZoneInfo,
        now_dt: datetime,
    ) -> List[str]:
        """Format events as board rows in departures-board style.

        Row format (22 tiles): {name:<12} {f_char}{s_char}{p_char} {days}
        Color tile markers ({63}, {66}) count as 1 tile but inflate string
        length by 3 characters, so string length is not used for truncation.
        """
        lines: List[str] = []
        for event in events:
            days_str = self._format_countdown(event, tz, now_dt)
            f_char = _TILE_GREEN if event["flight_time"] is not None else _TILE_RED
            s_char = "S" if event["stay"] else "-"
            p_char = "P" if event["paid"] else "-"
            name = event["name"][:12]
            line = f"{name:<12} {f_char}{s_char}{p_char} {days_str}"
            lines.append(line)

        while len(lines) < 6:
            lines.append("")

        return lines[:6]


Plugin = DeparturesPlugin
