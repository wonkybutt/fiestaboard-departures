"""Tests for the departures plugin."""

from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from external_plugins.departures import DeparturesPlugin, _TILE_GREEN, _TILE_RED

# Fixed "today" and "now" used across all time-sensitive tests
TODAY = date(2026, 5, 9)
NOW_DT = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)  # noon UTC

# ---------------------------------------------------------------------------
# ICS fixture strings
# ---------------------------------------------------------------------------

ICS_3_EVENTS = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:Hawaii
DTSTART;VALUE=DATE:20260520
DESCRIPTION:FLIGHT:1018 STAY:yes PAID:yes
END:VEVENT
BEGIN:VEVENT
SUMMARY:Austin
DTSTART;VALUE=DATE:20260601
DESCRIPTION:STAY:yes PAID:true
END:VEVENT
BEGIN:VEVENT
SUMMARY:London Adv
DTSTART;VALUE=DATE:20260615
DESCRIPTION:FLIGHT:2033 PAID:1
END:VEVENT
END:VCALENDAR"""

ICS_NO_FLAGS = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:Mystery Trip
DTSTART;VALUE=DATE:20260520
END:VEVENT
END:VCALENDAR"""

ICS_OLD_EVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:Old Event
DTSTART;VALUE=DATE:20260504
DESCRIPTION:FLIGHT:1000 STAY:yes PAID:yes
END:VEVENT
END:VCALENDAR"""

ICS_RECENT_PAST = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:Recent Past
DTSTART;VALUE=DATE:20260506
DESCRIPTION:FLIGHT:0900 STAY:no PAID:no
END:VEVENT
END:VCALENDAR"""

ICS_TODAY = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:Today Event
DTSTART;VALUE=DATE:20260509
DESCRIPTION:FLIGHT:1018 STAY:yes PAID:yes
END:VEVENT
END:VCALENDAR"""

ICS_FIVE_EVENTS = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:Event A
DTSTART;VALUE=DATE:20260515
DESCRIPTION:FLIGHT:0800 STAY:yes PAID:yes
END:VEVENT
BEGIN:VEVENT
SUMMARY:Event B
DTSTART;VALUE=DATE:20260520
DESCRIPTION:STAY:no PAID:no
END:VEVENT
BEGIN:VEVENT
SUMMARY:Event C
DTSTART;VALUE=DATE:20260525
DESCRIPTION:FLIGHT:14:30 STAY:yes PAID:yes
END:VEVENT
BEGIN:VEVENT
SUMMARY:Event D
DTSTART;VALUE=DATE:20260601
DESCRIPTION:STAY:no PAID:no
END:VEVENT
BEGIN:VEVENT
SUMMARY:Event E
DTSTART;VALUE=DATE:20260615
DESCRIPTION:FLIGHT:23:59 STAY:yes PAID:yes
END:VEVENT
END:VCALENDAR"""

ICS_AWARE_DATETIME = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:UTC Flight
DTSTART:20260520T120000Z
DESCRIPTION:FLIGHT:1200 STAY:yes PAID:yes
END:VEVENT
BEGIN:VEVENT
SUMMARY:Naive Flight
DTSTART:20260601T090000
DESCRIPTION:STAY:no PAID:no
END:VEVENT
END:VCALENDAR"""

ICS_TOMORROW_FLIGHT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:Red Eye
DTSTART;VALUE=DATE:20260510
DESCRIPTION:FLIGHT:0800 STAY:yes PAID:yes
END:VEVENT
END:VCALENDAR"""

ICS_HOURS_AWAY = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:Late Dep
DTSTART;VALUE=DATE:20260509
DESCRIPTION:FLIGHT:23:00 STAY:no PAID:no
END:VEVENT
END:VCALENDAR"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_get(ics_text: str, status: int = 200) -> MagicMock:
    """Return a mock requests.Response with the given ICS text."""
    m = MagicMock()
    m.text = ics_text
    m.status_code = status
    if status >= 400:
        m.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    else:
        m.raise_for_status.return_value = None
    return m


def _make_plugin(sample_manifest, config: dict) -> DeparturesPlugin:
    plugin = DeparturesPlugin(sample_manifest)
    plugin.config = config
    return plugin


def _patch_time(today=TODAY, now_dt=NOW_DT):
    """Return a context manager pair patching _get_today and _get_now."""
    return (
        patch.object(DeparturesPlugin, "_get_today", return_value=today),
        patch.object(DeparturesPlugin, "_get_now", return_value=now_dt),
    )


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TestDeparturesPlugin:
    """Tests for DeparturesPlugin.fetch_data()."""

    def test_plugin_id(self, sample_manifest):
        plugin = DeparturesPlugin(sample_manifest)
        assert plugin.plugin_id == "departures"

    # ------------------------------------------------------------------
    # Successful fetch with 3 events
    # ------------------------------------------------------------------

    def test_successful_fetch_event_count(self, sample_manifest, sample_config):
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.available is True
        assert result.error is None
        assert result.data["event_count"] == 3

    def test_successful_fetch_variable_keys(self, sample_manifest, sample_config):
        """Row variables are published; per-event variables are not."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        data = result.data
        assert "row_1" in data
        assert "current_page" in data
        assert "total_pages" in data
        for i in range(3):
            assert f"event_{i}_name" not in data
            assert f"event_{i}_days" not in data

    def test_successful_fetch_day_counts(self, sample_manifest, sample_config):
        """Hawaii=May20 (+10D), Austin=Jun1 (+23D), London=Jun15 (+37D) from May9 noon."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        lines = result.formatted_lines
        # Hawaii has FLIGHT:1018 — countdown is time-based from noon UTC May 9
        # May 20 10:18 UTC minus May 9 12:00 UTC = 10 days 22h18m → 10D
        assert "10D" in lines[0]
        # Austin has no flight time — whole days: May9→Jun1 = 23D
        assert "23D" in lines[1]
        # London has FLIGHT:2033 — time-based: May9 12:00 to Jun15 20:33 = 37D
        assert "37D" in lines[2]

    def test_events_sorted_chronologically(self, sample_manifest, sample_config):
        """Events should appear in date order regardless of ICS order."""
        reversed_ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Later
DTSTART;VALUE=DATE:20260615
DESCRIPTION:STAY:no PAID:no
END:VEVENT
BEGIN:VEVENT
SUMMARY:Earlier
DTSTART;VALUE=DATE:20260520
DESCRIPTION:STAY:no PAID:no
END:VEVENT
END:VCALENDAR"""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(reversed_ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert "Earlier" in result.formatted_lines[0]
        assert "Later" in result.formatted_lines[1]

    # ------------------------------------------------------------------
    # First indicator — time parsing → green/red tile in row
    # ------------------------------------------------------------------

    def test_valid_flight_time_green_in_row(self, sample_manifest, sample_config):
        """FLIGHT:1018 → green tile in row_1."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert _TILE_GREEN in result.data["row_1"]

    def test_missing_flight_time_red_in_row(self, sample_manifest, sample_config):
        """No FLIGHT field → red tile in row for that event."""
        plugin = _make_plugin(sample_manifest, sample_config)
        # Austin (index 1) has no FLIGHT — force display_rows=2 to get row_2
        config = {**sample_config, "display_rows": 2}
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert _TILE_RED in result.data["row_2"]

    def test_invalid_flight_time_red_in_row(self, sample_manifest, sample_config):
        """FLIGHT:yes (non-time value) → red tile."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Bad Flag
DTSTART;VALUE=DATE:20260520
DESCRIPTION:FLIGHT:yes STAY:no PAID:no
END:VEVENT
END:VCALENDAR"""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert _TILE_RED in result.data["row_1"]

    # ------------------------------------------------------------------
    # Boolean indicators — denylist logic
    # ------------------------------------------------------------------

    def test_boolean_indicator_truthy_values_green(self, sample_manifest):
        """Any value not in the falsy set → green tile."""
        for truthy_val in ["yes", "true", "1", "booked", "partial", "confirmed", "YES"]:
            ics = f"""\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Trip
DTSTART;VALUE=DATE:20260520
DESCRIPTION:STAY:{truthy_val}
END:VEVENT
END:VCALENDAR"""
            config = {
                "calendar_url": "https://example.com/calendar.ics",
                "timezone": "UTC",
                "indicator_1": "FLIGHT",
                "indicator_2": "STAY",
            }
            plugin = _make_plugin(sample_manifest, config)
            with patch("requests.get", return_value=_mock_get(ics)):
                with _patch_time()[0], _patch_time()[1]:
                    result = plugin.fetch_data()

            row = result.data["row_1"]
            assert _TILE_GREEN in row, f"Expected green for STAY:{truthy_val}, got: {row!r}"

    def test_boolean_indicator_falsy_values_red(self, sample_manifest):
        """no/false/0/blank → red tile."""
        for falsy_val in ["no", "false", "0", "NO", "FALSE"]:
            ics = f"""\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Trip
DTSTART;VALUE=DATE:20260520
DESCRIPTION:STAY:{falsy_val}
END:VEVENT
END:VCALENDAR"""
            config = {
                "calendar_url": "https://example.com/calendar.ics",
                "timezone": "UTC",
                "indicator_1": "FLIGHT",
                "indicator_2": "STAY",
            }
            plugin = _make_plugin(sample_manifest, config)
            with patch("requests.get", return_value=_mock_get(ics)):
                with _patch_time()[0], _patch_time()[1]:
                    result = plugin.fetch_data()

            row = result.data["row_1"]
            assert _TILE_RED in row, f"Expected red for STAY:{falsy_val}, got: {row!r}"

    def test_missing_boolean_indicator_defaults_red(self, sample_manifest):
        """Missing indicator key → red tile."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Trip
DTSTART;VALUE=DATE:20260520
DESCRIPTION:FLIGHT:1000
END:VEVENT
END:VCALENDAR"""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "indicator_1": "FLIGHT",
            "indicator_2": "STAY",
            "indicator_3": "PAID",
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        # STAY and PAID are missing → red tiles
        row = result.data["row_1"]
        assert _TILE_RED in row

    # ------------------------------------------------------------------
    # Configurable indicator keys
    # ------------------------------------------------------------------

    def test_custom_indicator_keys_parsed(self, sample_manifest):
        """Custom key names (HOTEL, TICKETS) are parsed from description."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Festival
DTSTART;VALUE=DATE:20260520
DESCRIPTION:DEPART:1400 HOTEL:booked TICKETS:yes
END:VEVENT
END:VCALENDAR"""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "indicator_1": "DEPART",
            "indicator_2": "HOTEL",
            "indicator_3": "TICKETS",
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        row = result.data["row_1"]
        # DEPART:1400 → valid time → green; HOTEL:booked → green; TICKETS:yes → green
        assert row.count(_TILE_GREEN) == 3

    def test_default_indicators_when_not_configured(self, sample_manifest, sample_config):
        """When indicators not in config, defaults to FLIGHT/STAY/PAID."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        # Hawaii has FLIGHT:1018 STAY:yes PAID:yes → all green
        row = result.data["row_1"]
        assert row.count(_TILE_GREEN) == 3

    def test_only_indicator_1_when_2_and_3_empty(self, sample_manifest):
        """indicator_2 and indicator_3 left empty → only 1 indicator shown."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Solo
DTSTART;VALUE=DATE:20260520
DESCRIPTION:FLIGHT:1000
END:VEVENT
END:VCALENDAR"""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "indicator_1": "FLIGHT",
            "indicator_2": "",
            "indicator_3": "",
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        row = result.data["row_1"]
        # 1 indicator → name width 16; only one tile
        assert row.count(_TILE_GREEN) + row.count(_TILE_RED) == 1

    # ------------------------------------------------------------------
    # Row layout shifts with indicator count
    # ------------------------------------------------------------------

    def test_layout_3_indicators_name_14(self, sample_manifest):
        """3 indicators → name field is 14 tiles wide."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:A Very Long Name Here
DTSTART;VALUE=DATE:20260520
DESCRIPTION:FLIGHT:1000 STAY:yes PAID:yes
END:VEVENT
END:VCALENDAR"""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "indicator_1": "FLIGHT",
            "indicator_2": "STAY",
            "indicator_3": "PAID",
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        row = result.data["row_1"]
        # Row: {name:<14} {i1}{i2}{i3}{days:>4} = 14+1+9+4 = 28 chars = 22 tiles
        assert row.startswith("A Very Long Na")

    def test_layout_2_indicators_name_15(self, sample_manifest):
        """2 indicators → name field is 15 tiles wide."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:A Very Long Name Here
DTSTART;VALUE=DATE:20260520
DESCRIPTION:FLIGHT:1000 STAY:yes
END:VEVENT
END:VCALENDAR"""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "indicator_1": "FLIGHT",
            "indicator_2": "STAY",
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        row = result.data["row_1"]
        assert row.startswith("A Very Long Nam")

    def test_layout_1_indicator_name_16(self, sample_manifest):
        """1 indicator → name field is 16 tiles wide."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:A Very Long Name Here
DTSTART;VALUE=DATE:20260520
DESCRIPTION:FLIGHT:1000
END:VEVENT
END:VCALENDAR"""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "indicator_1": "FLIGHT",
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        row = result.data["row_1"]
        assert row.startswith("A Very Long Name")

    # ------------------------------------------------------------------
    # Hours countdown (first indicator time present, < 24h away)
    # ------------------------------------------------------------------

    def test_hours_countdown_with_flight_time(self, sample_manifest, sample_config):
        """Event same day with FLIGHT:23:00, now=12:00 → 11H."""
        plugin = _make_plugin(sample_manifest, sample_config)
        now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        with patch("requests.get", return_value=_mock_get(ICS_HOURS_AWAY)):
            with patch.object(DeparturesPlugin, "_get_today", return_value=TODAY):
                with patch.object(DeparturesPlugin, "_get_now", return_value=now):
                    result = plugin.fetch_data()

        assert result.available is True
        assert "11H" in result.data["row_1"]

    def test_whole_days_without_flight_time(self, sample_manifest, sample_config):
        """Event < 24h away by date but no flight time → shows whole days."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:No Time
DTSTART;VALUE=DATE:20260510
DESCRIPTION:STAY:no PAID:no
END:VEVENT
END:VCALENDAR"""
        plugin = _make_plugin(sample_manifest, sample_config)
        now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        with patch("requests.get", return_value=_mock_get(ics)):
            with patch.object(DeparturesPlugin, "_get_today", return_value=TODAY):
                with patch.object(DeparturesPlugin, "_get_now", return_value=now):
                    result = plugin.fetch_data()

        assert "1D" in result.data["row_1"]

    def test_flight_time_past_shows_dprtd(self, sample_manifest, sample_config):
        """Flight time in the past on event day → DPTD."""
        plugin = _make_plugin(sample_manifest, sample_config)
        now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        with patch("requests.get", return_value=_mock_get(ICS_TODAY)):
            with patch.object(DeparturesPlugin, "_get_today", return_value=TODAY):
                with patch.object(DeparturesPlugin, "_get_now", return_value=now):
                    result = plugin.fetch_data()

        assert "DEPARTED" in result.data["row_1"]

    # ------------------------------------------------------------------
    # Lookback days
    # ------------------------------------------------------------------

    def test_event_beyond_lookback_excluded(self, sample_manifest):
        """May 4 is 5 days before May 9; default lookback=3 → excluded."""
        config = {"calendar_url": "https://example.com/calendar.ics", "timezone": "UTC"}
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ICS_OLD_EVENT)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.available is True
        assert result.data["event_count"] == 0
        assert result.data["row_1"] == ""

    def test_event_within_lookback_included(self, sample_manifest):
        """May 6 is 3 days before May 9; within default lookback → shown as DPTD."""
        config = {"calendar_url": "https://example.com/calendar.ics", "timezone": "UTC"}
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ICS_RECENT_PAST)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.data["total_pages"] == 1
        assert "DEPARTED" in result.data["row_1"]

    def test_custom_lookback_days(self, sample_manifest):
        """lookback_days=6 → May 4 (5 days past) is included."""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "lookback_days": 6,
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ICS_OLD_EVENT)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.data["total_pages"] == 1

    def test_lookback_days_zero(self, sample_manifest):
        """lookback_days=0 → only today and future shown; yesterday excluded."""
        ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Yesterday
DTSTART;VALUE=DATE:20260508
DESCRIPTION:STAY:no PAID:no
END:VEVENT
BEGIN:VEVENT
SUMMARY:Tomorrow
DTSTART;VALUE=DATE:20260510
DESCRIPTION:STAY:no PAID:no
END:VEVENT
END:VCALENDAR"""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "lookback_days": 0,
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.data["total_pages"] == 1
        assert "Tomorrow" in result.data["row_1"]

    # ------------------------------------------------------------------
    # All events tracked (no cap)
    # ------------------------------------------------------------------

    def test_all_events_tracked(self, sample_manifest):
        """All 5 future events should be returned with no cap."""
        config = {"calendar_url": "https://example.com/calendar.ics", "timezone": "UTC"}
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ICS_FIVE_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.available is True
        # 5 events, display_rows=1 → 5 pages; all 5 appear in formatted_lines
        assert result.data["total_pages"] == 5
        assert result.formatted_lines[4] != ""

    # ------------------------------------------------------------------
    # Network error
    # ------------------------------------------------------------------

    def test_network_error_returns_unavailable(self, sample_manifest, sample_config):
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", side_effect=requests.ConnectionError("timeout")):
            result = plugin.fetch_data()

        assert result.available is False
        assert result.error is not None

    def test_http_error_returns_unavailable(self, sample_manifest, sample_config):
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get("", status=500)):
            result = plugin.fetch_data()

        assert result.available is False
        assert result.error is not None

    # ------------------------------------------------------------------
    # Malformed ICS
    # ------------------------------------------------------------------

    def test_malformed_ics_returns_unavailable(self, sample_manifest, sample_config):
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get("not ics data")):
            with patch(
                "external_plugins.departures.Calendar.from_ical",
                side_effect=ValueError("malformed"),
            ):
                result = plugin.fetch_data()

        assert result.available is False
        assert result.error is not None

    # ------------------------------------------------------------------
    # Timezone-aware and naive DTSTART
    # ------------------------------------------------------------------

    def test_timezone_aware_dtstart(self, sample_manifest, sample_config):
        """DTSTART:20260520T120000Z (UTC aware datetime) is handled."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_AWARE_DATETIME)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.available is True
        assert result.data["total_pages"] == 2
        assert "UTC Flight" in result.data["row_1"]

    def test_timezone_naive_dtstart(self, sample_manifest, sample_config):
        """DTSTART:20260601T090000 (naive datetime) is treated as UTC."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_AWARE_DATETIME)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.available is True
        assert "Naive Flight" in result.formatted_lines[1]

    # ------------------------------------------------------------------
    # webcal:// URL rewriting
    # ------------------------------------------------------------------

    def test_webcal_url_rewritten_to_https(self, sample_manifest):
        config = {"calendar_url": "webcal://example.com/calendar.ics", "timezone": "UTC"}
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)) as mock_get:
            with _patch_time()[0], _patch_time()[1]:
                plugin.fetch_data()

        called_url = mock_get.call_args[0][0]
        assert called_url.startswith("https://")

    # ------------------------------------------------------------------
    # Missing calendar URL / invalid timezone
    # ------------------------------------------------------------------

    def test_missing_calendar_url_returns_unavailable(self, sample_manifest):
        plugin = _make_plugin(sample_manifest, {"timezone": "UTC"})
        result = plugin.fetch_data()
        assert result.available is False
        assert result.error is not None

    def test_invalid_timezone_returns_unavailable(self, sample_manifest):
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "Bogus/Zone",
        }
        plugin = _make_plugin(sample_manifest, config)
        result = plugin.fetch_data()
        assert result.available is False
        assert "timezone" in result.error.lower()

    # ------------------------------------------------------------------
    # formatted_lines
    # ------------------------------------------------------------------

    def test_formatted_lines_always_6(self, sample_manifest, sample_config):
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.formatted_lines is not None
        assert len(result.formatted_lines) == 6

    def test_formatted_lines_empty_when_no_events(self, sample_manifest, sample_config):
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_OLD_EVENT)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.formatted_lines == ["", "", "", "", "", ""]

    def test_formatted_lines_green_tile_for_valid_flight(self, sample_manifest, sample_config):
        """Valid flight time → green tile in formatted_lines."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert _TILE_GREEN in result.formatted_lines[0]

    def test_formatted_lines_red_tile_for_missing_flight(self, sample_manifest, sample_config):
        """No flight time → red tile in formatted_lines."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        # Austin (index 1) has no FLIGHT field
        assert _TILE_RED in result.formatted_lines[1]

    def test_formatted_lines_dprtd_for_past_flight(self, sample_manifest, sample_config):
        """Flight time already passed → DEPARTED row with no indicator tiles."""
        plugin = _make_plugin(sample_manifest, sample_config)
        now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        with patch("requests.get", return_value=_mock_get(ICS_TODAY)):
            with patch.object(DeparturesPlugin, "_get_today", return_value=TODAY):
                with patch.object(DeparturesPlugin, "_get_now", return_value=now):
                    result = plugin.fetch_data()

        row = result.formatted_lines[0]
        assert "DEPARTED" in row
        assert _TILE_GREEN not in row
        assert _TILE_RED not in row

    def test_departed_row_is_22_tiles(self, sample_manifest, sample_config):
        """Departed row must be exactly 22 display tiles (no color markers)."""
        plugin = _make_plugin(sample_manifest, sample_config)
        now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
        with patch("requests.get", return_value=_mock_get(ICS_TODAY)):
            with patch.object(DeparturesPlugin, "_get_today", return_value=TODAY):
                with patch.object(DeparturesPlugin, "_get_now", return_value=now):
                    result = plugin.fetch_data()

        row = result.formatted_lines[0]
        # No color markers in departed rows, so string length == tile count
        assert len(row) == 22

    # ------------------------------------------------------------------
    # Name truncation
    # ------------------------------------------------------------------

    def test_name_truncated_to_14_chars(self, sample_manifest, sample_config):
        long_name_ics = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:A Very Long Event Name That Exceeds Fourteen Characters
DTSTART;VALUE=DATE:20260520
DESCRIPTION:STAY:no PAID:no
END:VEVENT
END:VCALENDAR"""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(long_name_ics)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        # sample_config uses default 3 indicators → name width = 14
        row = result.data["row_1"]
        assert row.startswith("A Very Long Ev")
        assert row[14] == " "

    # ------------------------------------------------------------------
    # Cycling row variables
    # ------------------------------------------------------------------

    def test_row_1_present_on_first_fetch(self, sample_manifest, sample_config):
        """First fetch always returns row_1."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert "row_1" in result.data
        assert result.data["row_1"] != ""

    def test_single_row_page_starts_at_first_event(self, sample_manifest, sample_config):
        """display_rows=1, page 0 → row_1 contains first event name."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert "Hawaii" in result.data["row_1"]

    def test_two_row_display(self, sample_manifest):
        """display_rows=2 → row_1 and row_2 populated on first fetch."""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "display_rows": 2,
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert "Hawaii" in result.data["row_1"]
        assert "Austin" in result.data["row_2"]

    def test_page_advances_after_cycle_seconds(self, sample_manifest, sample_config):
        """After cycle_seconds elapses, page advances to next event."""
        from datetime import timedelta
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "display_rows": 1,
            "cycle_seconds": 60,
        }
        plugin = _make_plugin(sample_manifest, config)

        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result1 = plugin.fetch_data()

        assert "Hawaii" in result1.data["row_1"]
        assert result1.data["current_page"] == 1

        plugin._cycle_last_advance = datetime.now() - timedelta(seconds=61)

        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result2 = plugin.fetch_data()

        assert result2.data["current_page"] == 2
        assert "Austin" in result2.data["row_1"]

    def test_empty_slot_when_last_page_is_partial(self, sample_manifest):
        """3 events, display_rows=2 → page 2 has row_1 filled, row_2 empty."""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "display_rows": 2,
            "cycle_seconds": 30,
        }
        plugin = _make_plugin(sample_manifest, config)
        plugin._cycle_page = 1
        plugin._cycle_last_advance = datetime.now()

        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert "London" in result.data["row_1"]
        assert result.data["row_2"] == ""

    def test_total_pages_calculated_correctly(self, sample_manifest):
        """5 events, display_rows=2 → 3 total pages."""
        config = {
            "calendar_url": "https://example.com/calendar.ics",
            "timezone": "UTC",
            "display_rows": 2,
        }
        plugin = _make_plugin(sample_manifest, config)
        with patch("requests.get", return_value=_mock_get(ICS_FIVE_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.data["total_pages"] == 3

    def test_page_wraps_around(self, sample_manifest, sample_config):
        """After last page, cycling wraps back to page 0."""
        plugin = _make_plugin(sample_manifest, sample_config)
        plugin._cycle_page = 2
        plugin._cycle_last_advance = datetime.now()

        with patch("requests.get", return_value=_mock_get(ICS_3_EVENTS)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.data["current_page"] == 3
        assert "London" in result.data["row_1"]

    def test_no_events_returns_empty_row(self, sample_manifest, sample_config):
        """No events → row_1 is empty string."""
        plugin = _make_plugin(sample_manifest, sample_config)
        with patch("requests.get", return_value=_mock_get(ICS_OLD_EVENT)):
            with _patch_time()[0], _patch_time()[1]:
                result = plugin.fetch_data()

        assert result.data["row_1"] == ""
        assert result.data["total_pages"] == 1


class TestDeparturesPluginInternals:
    """Unit tests for internal helper methods."""

    # ------------------------------------------------------------------
    # _parse_flight_time
    # ------------------------------------------------------------------

    def test_parse_flight_time_hhmm(self, sample_manifest):
        plugin = DeparturesPlugin(sample_manifest)
        assert plugin._parse_flight_time("1018") == time(10, 18)
        assert plugin._parse_flight_time("0000") == time(0, 0)
        assert plugin._parse_flight_time("2359") == time(23, 59)

    def test_parse_flight_time_colon(self, sample_manifest):
        plugin = DeparturesPlugin(sample_manifest)
        assert plugin._parse_flight_time("13:37") == time(13, 37)
        assert plugin._parse_flight_time("00:00") == time(0, 0)
        assert plugin._parse_flight_time("23:59") == time(23, 59)

    def test_parse_flight_time_invalid_returns_none(self, sample_manifest):
        plugin = DeparturesPlugin(sample_manifest)
        for val in ["yes", "no", "true", "1", "9999", "2400", "1260", "", "abc", "1:0"]:
            assert plugin._parse_flight_time(val) is None, f"Expected None for {val!r}"

    # ------------------------------------------------------------------
    # _parse_indicators
    # ------------------------------------------------------------------

    def test_parse_indicators_first_is_time(self, sample_manifest):
        """First indicator with valid time → time_value set, green tile."""
        plugin = DeparturesPlugin(sample_manifest)
        result = plugin._parse_indicators("FLIGHT:1018 STAY:yes PAID:yes", ["FLIGHT", "STAY", "PAID"])
        assert result["time_value"] == time(10, 18)
        assert result["indicator_tiles"][0] == _TILE_GREEN

    def test_parse_indicators_first_invalid_time(self, sample_manifest):
        """First indicator with unparseable value → time_value None, red tile."""
        plugin = DeparturesPlugin(sample_manifest)
        result = plugin._parse_indicators("FLIGHT:yes STAY:yes PAID:yes", ["FLIGHT", "STAY", "PAID"])
        assert result["time_value"] is None
        assert result["indicator_tiles"][0] == _TILE_RED

    def test_parse_indicators_first_missing(self, sample_manifest):
        """First indicator absent → time_value None, red tile."""
        plugin = DeparturesPlugin(sample_manifest)
        result = plugin._parse_indicators("STAY:yes PAID:yes", ["FLIGHT", "STAY", "PAID"])
        assert result["time_value"] is None
        assert result["indicator_tiles"][0] == _TILE_RED

    def test_parse_indicators_boolean_denylist(self, sample_manifest):
        """Boolean indicators: no/false/0/blank → red; anything else → green."""
        plugin = DeparturesPlugin(sample_manifest)
        for falsy in ["no", "false", "0", "NO", "FALSE"]:
            result = plugin._parse_indicators(f"STAY:{falsy}", ["FLIGHT", "STAY"])
            assert result["indicator_tiles"][1] == _TILE_RED, f"Expected red for STAY:{falsy}"

        for truthy in ["yes", "true", "1", "booked", "partial"]:
            result = plugin._parse_indicators(f"STAY:{truthy}", ["FLIGHT", "STAY"])
            assert result["indicator_tiles"][1] == _TILE_GREEN, f"Expected green for STAY:{truthy}"

    def test_parse_indicators_missing_boolean_is_red(self, sample_manifest):
        """Missing boolean indicator key → red tile."""
        plugin = DeparturesPlugin(sample_manifest)
        result = plugin._parse_indicators("FLIGHT:1000", ["FLIGHT", "STAY"])
        assert result["indicator_tiles"][1] == _TILE_RED

    def test_parse_indicators_custom_keys(self, sample_manifest):
        """Custom key names are matched case-insensitively."""
        plugin = DeparturesPlugin(sample_manifest)
        result = plugin._parse_indicators("DEPART:1400 HOTEL:booked", ["DEPART", "HOTEL"])
        assert result["time_value"] == time(14, 0)
        assert result["indicator_tiles"] == [_TILE_GREEN, _TILE_GREEN]

    def test_parse_indicators_empty_description(self, sample_manifest):
        """Empty description → all red, no time value."""
        plugin = DeparturesPlugin(sample_manifest)
        result = plugin._parse_indicators("", ["FLIGHT", "STAY", "PAID"])
        assert result["time_value"] is None
        assert result["indicator_tiles"] == [_TILE_RED, _TILE_RED, _TILE_RED]

    # ------------------------------------------------------------------
    # _normalize_dtstart
    # ------------------------------------------------------------------

    def test_normalize_dtstart_date_only(self, sample_manifest):
        from datetime import date as dt_date
        plugin = DeparturesPlugin(sample_manifest)
        dtstart = MagicMock()
        dtstart.dt = dt_date(2026, 5, 20)
        result = plugin._normalize_dtstart(dtstart)
        assert result is not None
        assert result.tzinfo == timezone.utc
        assert result.year == 2026 and result.month == 5 and result.day == 20

    def test_normalize_dtstart_aware_datetime(self, sample_manifest):
        plugin = DeparturesPlugin(sample_manifest)
        dtstart = MagicMock()
        dtstart.dt = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
        result = plugin._normalize_dtstart(dtstart)
        assert result is not None and result.tzinfo is not None

    def test_normalize_dtstart_naive_datetime(self, sample_manifest):
        plugin = DeparturesPlugin(sample_manifest)
        dtstart = MagicMock()
        dtstart.dt = datetime(2026, 5, 20, 12, 0, 0)
        result = plugin._normalize_dtstart(dtstart)
        assert result is not None and result.tzinfo == timezone.utc

    def test_normalize_dtstart_bad_value_returns_none(self, sample_manifest):
        plugin = DeparturesPlugin(sample_manifest)
        dtstart = MagicMock()
        dtstart.dt = "not a date"
        assert plugin._normalize_dtstart(dtstart) is None
