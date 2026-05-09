import logging
import httpx
import re
import asyncio
from datetime import datetime, timezone
from src.plugins.base import PluginBase, PluginResult

class DeparturesPlugin(PluginBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logging.getLogger(__name__)
        self.events = []
        self.logger.info("LOG: Departures v1.0.0 __init__")

    @property
    def plugin_id(self):
        return "departures"

    async def fetch_data(self) -> bool:
        self.logger.info("LOG: fetch_data() starting")
        url = self.config.get("calendar_url")
        if not url:
            self.logger.warning("LOG: No calendar_url in config")
            return False
        
        if url.startswith("webcal://"):
            url = url.replace("webcal://", "https://", 1)

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                ics_data = response.text
                self.logger.info(f"LOG: Fetched {len(ics_data)} bytes")

            now = datetime.now(timezone.utc)
            new_events = []
            vevents = re.findall(r"BEGIN:VEVENT.*?END:VEVENT", ics_data, re.DOTALL)
            
            for event in vevents:
                summary_match = re.search(r"SUMMARY:(.*)", event)
                dtstart_match = re.search(r"DTSTART(?:;VALUE=DATE)?:(\d{8}T\d{6}Z?)", event)
                
                if summary_match and dtstart_match:
                    summary = summary_match.group(1).strip()
                    dt_str = dtstart_match.group(1)
                    try:
                        if dt_str.endswith('Z'):
                            dt = datetime.strptime(dt_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                        else:
                            dt = datetime.strptime(dt_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                        
                        if dt > now:
                            diff = dt - now
                            days, hours = diff.days, diff.seconds // 3600
                            countdown = f"{days}d {hours}h" if days > 0 else f"{hours}h"
                            new_events.append({"name": summary[:18], "countdown": countdown, "time": dt})
                    except: continue

            new_events.sort(key=lambda x: x["time"])
            self.events = new_events
            self.logger.info(f"LOG: Found {len(self.events)} events")
            return True
        except Exception as e:
            self.logger.error(f"LOG: fetch_data Error: {e}")
            return False

    def get_variables(self) -> PluginResult:
        # LOG: get_variables() requested
        try:
            variables = {
                "heartbeat": datetime.now().strftime("%H:%M:%S"),
                "event_count": str(len(self.events))
            }
            max_events = int(self.config.get("max_events", 6))
            for i in range(1, max_events + 1):
                if (i-1) < len(self.events):
                    variables[f"event_{i}_name"] = self.events[i-1]["name"]
                    variables[f"event_{i}_countdown"] = self.events[i-1]["countdown"]
                else:
                    variables[f"event_{i}_name"] = ""
                    variables[f"event_{i}_countdown"] = ""
            return PluginResult(available=True, data=variables)
        except Exception as e:
            self.logger.error(f"LOG: get_variables Error: {e}")
            return PluginResult(available=False, data={})
