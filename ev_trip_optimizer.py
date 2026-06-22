"""
EV Trip Optimizer
==================

An AppDaemon app for Home Assistant that looks ahead across your Google
Calendar(s), works out how much energy each upcoming trip will actually
need (via the Google Routes API), and decides two things:

  1. How much charge you actually need before your next trip
     (writes a target SoC to a helper)
  2. Whether charging needs to start RIGHT NOW regardless of your normal
     cheap-price threshold, because waiting any longer risks missing the
     trip (writes an override flag to a helper)

This app is intentionally charger-agnostic. It never calls a
charger-specific service — no switch.turn_on, no number.set_value on an
inverter, nothing hardware-specific. It only writes to two generic Home
Assistant helpers:

  - input_number.ev_charge_target_soc   the SoC ceiling to charge to
  - input_boolean.ev_charge_override    "on" = charge now regardless of
                                          your normal price rules

Wire your own charging automation (a DC charger integration, a Tesla
Wall Connector, anything that exposes a number/switch in Home
Assistant) to read those two helpers. See the README for a worked
example of both.

Configuration is via apps.yaml — see apps.yaml.example in this repo.
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import urllib.request
import json


class EVTripOptimizer(hass.Hass):

    def initialize(self):
        self.log("EV Trip Optimizer starting...")

        # --- Required config (see apps.yaml.example) ---
        self.google_api_key       = self.args["google_api_key"]
        self.home_lat             = float(self.args["home_lat"])
        self.home_lng             = float(self.args["home_lng"])
        self.calendars            = self.args["calendars"]                # list of calendar.* entity_ids
        self.soc_sensor           = self.args["soc_sensor"]               # entity_id, 0-100
        self.price_forecast_sensor = self.args["price_forecast_sensor"]   # sensor with a 'forecasts' attribute

        # --- Optional config (sensible defaults) ---
        self.min_soc              = float(self.args.get("min_soc", 30.0))
        self.baseline_target_soc  = float(self.args.get("baseline_target_soc", 80.0))
        self.battery_cap          = float(self.args.get("battery_capacity_kwh", 75.0))
        self.efficiency           = float(self.args.get("efficiency_km_per_kwh", 6.5))
        self.charge_rate_kw       = float(self.args.get("charge_rate_kw", 25.0))
        self.horizon_hours        = float(self.args.get("horizon_hours", 72))

        self.target_soc_helper = self.args.get("target_soc_helper", "input_number.ev_charge_target_soc")
        self.override_helper   = self.args.get("override_helper", "input_boolean.ev_charge_override")
        self.status_sensor     = self.args.get("status_sensor", "sensor.ev_trip_optimizer")

        self.run_hourly(self.optimize, datetime.now().replace(minute=0, second=0, microsecond=0))
        self.run_in(self.optimize, 10)

    # ------------------------------------------------------------------
    # Calendar + routing helpers
    # ------------------------------------------------------------------

    def get_calendar_events(self, cal, start, end):
        result = self.call_service(
            "calendar/get_events",
            entity_id=cal,
            start_date_time=start.isoformat(),
            end_date_time=end.isoformat(),
            return_response=True
        )
        try:
            return result["result"]["response"][cal]["events"]
        except Exception as e:
            self.log(f"Failed to parse calendar response for {cal}: {e}", level="ERROR")
            return []

    def get_trip_distance_km(self, destination_address):
        """Call Google Routes API to get round trip distance in km."""
        try:
            url = "https://routes.googleapis.com/directions/v2:computeRoutes"
            payload = {
                "origin": {
                    "location": {
                        "latLng": {"latitude": self.home_lat, "longitude": self.home_lng}
                    }
                },
                "destination": {
                    "address": destination_address
                },
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE"
            }
            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.google_api_key,
                "X-Goog-FieldMask": "routes.distanceMeters"
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            metres = data["routes"][0]["distanceMeters"]
            one_way_km = metres / 1000
            round_trip_km = round(one_way_km * 2, 1)
            self.log(f"Distance to {destination_address[:40]}: {one_way_km:.1f}km one way, {round_trip_km}km round trip")
            return round_trip_km
        except Exception as e:
            self.log(f"Routes API error for {destination_address[:40]}: {e}", level="WARNING")
            return None

    # ------------------------------------------------------------------
    # Main optimization cycle
    # ------------------------------------------------------------------

    def optimize(self, kwargs):
        self.log("Running EV trip optimization...")

        target_soc = self.baseline_target_soc  # may be raised below if a trip needs more than this

        raw_soc = self.get_state(self.soc_sensor)
        if raw_soc in (None, "unknown", "unavailable"):
            self.log("SoC unavailable, skipping")
            self.set_state(self.status_sensor, state="SoC unavailable", attributes={})
            return
        current_soc = float(raw_soc)

        now      = datetime.now().astimezone()
        end_time = now + timedelta(hours=self.horizon_hours)

        trips = []
        seen_locations = {}  # cache distances to avoid duplicate API calls
        for cal in self.calendars:
            events = self.get_calendar_events(cal, now, end_time)
            self.log(f"{cal}: got {len(events)} events")
            for event in events:
                location = (event.get("location") or "").strip()
                if not location:
                    continue
                start_str = event.get("start")
                if not start_str:
                    continue
                try:
                    depart = datetime.fromisoformat(start_str).astimezone()
                except Exception:
                    continue
                if depart < now:
                    continue

                # Get distance — use cache if same location already looked up
                if location not in seen_locations:
                    dist = self.get_trip_distance_km(location)
                    seen_locations[location] = dist if dist is not None else 50.0
                distance_km = seen_locations[location]

                trips.append({
                    "summary":     event.get("summary", "Unknown"),
                    "location":    location,
                    "depart":      depart,
                    "distance_km": distance_km,
                    "soc_before":  None,
                    "soc_after":   None,
                    "breach":      False,
                })

        # Deduplicate — same summary + depart time from both calendars
        seen = set()
        unique_trips = []
        for t in trips:
            key = (t["summary"], t["depart"].isoformat())
            if key not in seen:
                seen.add(key)
                unique_trips.append(t)
        trips = unique_trips

        self.log(f"Total trips with locations: {len(trips)}")

        if not trips:
            self._write_outputs(target_soc, override_active=False)
            self.set_state(self.status_sensor,
                state="No upcoming trips",
                attributes={
                    "current_soc":   current_soc,
                    "min_soc":       self.min_soc,
                    "target_soc":    target_soc,
                    "horizon_hours": self.horizon_hours,
                    "last_updated":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
            return

        trips.sort(key=lambda x: x["depart"])

        # --- Walk trip timeline projecting SoC ---
        soc = current_soc
        charge_needed_before = None
        required_soc = None

        for trip in trips:
            trip["soc_before"] = round(soc, 1)
            soc_used  = (trip["distance_km"] / self.efficiency / self.battery_cap) * 100
            soc_after = max(soc - soc_used, 0)
            trip["soc_after"] = round(soc_after, 1)
            if soc_after < self.min_soc and charge_needed_before is None:
                charge_needed_before = trip["depart"]
                # Minimum SoC we'd need before this trip to still land at
                # min_soc afterwards — i.e. the real requirement, not just
                # the baseline preference.
                required_soc = min(self.min_soc + soc_used, 100.0)
                trip["breach"] = True
            soc = soc_after

        # Raise the target above baseline only if this trip genuinely needs more.
        if required_soc is not None:
            target_soc = max(self.baseline_target_soc, required_soc)

        # --- Find cheapest price window, and the hard deadline to start charging ---
        charge_window_start = None
        charge_window_price = None
        kwh_to_charge       = 0.0
        charge_duration_hrs = 0.0
        override_active     = False

        if charge_needed_before:
            kwh_to_charge       = max(((target_soc - current_soc) / 100) * self.battery_cap, 0)
            charge_duration_hrs = kwh_to_charge / self.charge_rate_kw if self.charge_rate_kw else 0
            latest_safe_start   = charge_needed_before - timedelta(hours=charge_duration_hrs)

            forecast = self.get_state(self.price_forecast_sensor, attribute="forecasts")
            if forecast and kwh_to_charge > 0:
                valid = []
                for slot in forecast:
                    try:
                        ss = datetime.fromisoformat(slot["start_time"]).astimezone()
                        se = datetime.fromisoformat(slot["end_time"]).astimezone()
                        if ss >= now and se <= charge_needed_before:
                            valid.append({"start": ss, "price": slot["per_kwh"] * 100})
                    except Exception:
                        continue
                if valid:
                    valid.sort(key=lambda x: x["price"])
                    charge_window_start = valid[0]["start"]
                    charge_window_price = round(valid[0]["price"], 1)

            # Trigger the override at whichever comes first: the cheapest
            # window we found, or the hard deadline by which charging must
            # start to finish in time. The second branch covers cases where
            # no price forecast is available, or the cheap window leaves
            # too little time to actually finish charging before departure.
            trigger_time = min(charge_window_start, latest_safe_start) if charge_window_start else latest_safe_start

            if kwh_to_charge > 0 and trigger_time <= now < charge_needed_before:
                override_active = True

        self._write_outputs(target_soc, override_active)

        trip_list = [{
            "summary":     t["summary"],
            "location":    t["location"],
            "depart":      t["depart"].strftime("%a %d %b %H:%M"),
            "distance_km": t["distance_km"],
            "soc_before":  t["soc_before"],
            "soc_after":   t["soc_after"],
            "breach":      t["breach"],
        } for t in trips]

        if charge_needed_before:
            if override_active:
                status = f"Charging now (override) — target {target_soc:.0f}%, needed by {charge_needed_before.strftime('%a %H:%M')}"
            elif charge_window_start:
                status = f"Charge needed — best window {charge_window_start.strftime('%a %H:%M')} at {charge_window_price}c/kWh"
            else:
                status = f"Charge needed before {charge_needed_before.strftime('%a %H:%M')} — no price window found"
        else:
            status = "No charge needed"

        self.set_state(self.status_sensor, state=status[:255], attributes={
            "current_soc":          current_soc,
            "min_soc":              self.min_soc,
            "target_soc":           target_soc,
            "trips":                trip_list,
            "charge_needed":        charge_needed_before is not None,
            "charge_needed_before": charge_needed_before.strftime("%a %d %b %H:%M") if charge_needed_before else None,
            "charge_window_start":  charge_window_start.strftime("%a %d %b %H:%M") if charge_window_start else None,
            "charge_window_price":  charge_window_price,
            "override_active":      override_active,
            "kwh_to_charge":        round(kwh_to_charge, 1),
            "charge_duration_hrs":  round(charge_duration_hrs, 2),
            "last_updated":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.log(f"EV Optimizer: {status}")

    # ------------------------------------------------------------------
    # Output helpers — the only two entities this app ever drives
    # ------------------------------------------------------------------

    def _write_outputs(self, target_soc, override_active):
        self.call_service(
            "input_number/set_value",
            entity_id=self.target_soc_helper,
            value=round(target_soc, 1)
        )
        self.call_service(
            "input_boolean/turn_on" if override_active else "input_boolean/turn_off",
            entity_id=self.override_helper
        )
