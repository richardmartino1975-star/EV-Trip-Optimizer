# EV Trip Optimizer

A Home Assistant / AppDaemon app that looks at your Google Calendar,
works out how much energy your upcoming trips will actually need, and
tells your charging system how much SoC to aim for and when it needs
to charge regardless of price — instead of charging to a flat
percentage on a fixed schedule.

## Why this exists

Most "smart EV charging" setups for Home Assistant do one of two things well, but not both:

- **Price/solar-optimized charging** (e.g. [EV Smart Charging](https://github.com/jonasbkarlsson/ev_smart_charging)) — charges to a fixed target whenever electricity is cheap or there's solar excess. It has no concept of *why* you need charge or *when* you need it by.
- **Calendar-aware notifications** — a few people have wired up "check my calendar, warn me if I won't have enough range," but nothing that actually closes the loop into a charging decision.

As of writing, [this exact gap was raised on the Home Assistant community forum](https://community.home-assistant.io/t/smart-ev-charging-based-on-calendar-events-solar-and-dynamic-pricing/943357) — "charge enough for the trips I actually have, not a flat percentage, and don't let a cheap-price threshold stop you from charging when a deadline is close" — and it's sat unanswered. This project is one answer to it.

## What it does

Every hour (and once at startup):

1. Pulls upcoming events with a location from your configured Google Calendar(s)
2. Calls the Google Routes API to get the real round-trip distance for each one
3. Walks your projected SoC forward across all upcoming trips using your car's efficiency and battery capacity
4. If any trip would drop you below your safety floor, works out the *actual* SoC you need before that trip — not just a flat default
5. Finds the cheapest available price window before that trip's deadline (using whatever price-forecast sensor you point it at — Amber Electric, or anything that exposes a compatible `forecasts` attribute)
6. Decides whether charging needs to start now, regardless of your normal "only charge when it's cheap" rule, because the deadline is close enough that waiting any longer risks missing the trip

## What it writes — the integration contract

This app deliberately **never controls a charger directly**. It doesn't know or care whether you have a Sigenergy DC charger, a Tesla Wall Connector, or anything else. It only writes to two generic Home Assistant helpers:

| Helper | Type | Meaning |
|---|---|---|
| `input_number.ev_charge_target_soc` | input_number | The SoC to charge to. Raised above your configured baseline only when an upcoming trip genuinely needs more. |
| `input_boolean.ev_charge_override` | input_boolean | `on` = charge now even if your normal price/solar conditions aren't met, because the deadline requires it. |

**Your own charging automation needs to read these two values.** A minimal example, assuming you already have *some* automation that turns a charger on/off based on price:

```yaml
conditions:
  - condition: or
    conditions:
      - condition: numeric_state
        entity_id: sensor.your_price_sensor
        below: input_number.your_cheap_threshold
      - condition: state
        entity_id: input_boolean.ev_charge_override
        state: "on"
actions:
  - action: number.set_value
    target:
      entity_id: number.your_charger_power_limit   # or whatever your charger exposes
    data:
      value: "{{ states('input_number.ev_charge_target_soc') }}"
```

The exact action depends entirely on your hardware — a Tesla Wall Connector setup might just set `number.<car>_charge_limit` directly via the Tesla Fleet integration and let the car manage the rest, with no separate power-limit logic needed at all.

## Requirements

- **[AppDaemon](https://appdaemon.readthedocs.io/)** add-on, installed and running
- **Google Calendar integration** configured in Home Assistant, with calendar entities exposing a `location` on relevant events
- A **Google Cloud project** with the **Routes API** enabled, and an API key
  - The Routes API has a free monthly tier but isn't unlimited — this app caches distance lookups per unique location per run to keep calls down, but be aware of it if you have a lot of calendar churn
- A **price-forecast sensor** exposing a `forecasts` attribute as a list of `{start_time, end_time, per_kwh}` — this matches the Amber Electric integration's format; if you're on a different provider, a template sensor reshaping your data into this format will work fine
- A **SoC sensor** for your vehicle (0–100), from whatever integration you're already using (Tesla Fleet, Tesla BLE, etc.)
- Two Home Assistant helpers, created via **Settings → Devices & Services → Helpers**:
  - `input_number.ev_charge_target_soc` (min 0, max 100)
  - `input_boolean.ev_charge_override`

## Installation

1. Copy `ev_trip_optimizer.py` into your AppDaemon `apps/` directory
2. Copy `apps.yaml.example` into your `apps.yaml` (or merge the relevant block in), and fill in your own values — see the comments in the file
3. Create the two helpers listed above if they don't already exist
4. Restart AppDaemon
5. Check the logs for `EV Trip Optimizer starting...` and confirm `sensor.ev_trip_optimizer` appears in Developer Tools → States
6. Wire your own charging automation to respect `input_number.ev_charge_target_soc` and `input_boolean.ev_charge_override`

## Optional: dashboard card

`dashboard-card.yaml` is a Lovelace card showing current SoC, projected SoC after each upcoming trip, and the charge window if one's needed. It requires the [HTML Jinja2 Template card](https://github.com/PiotrMachowski/Home-Assistant-Lovelace-HTML-Jinja2-Template-card) (PiotrMachowski), available via HACS. Add it as a manual card and paste in the YAML — no entity references need editing, it reads entirely from `sensor.ev_trip_optimizer`.

## Known limitations

- Round-trip distance is calculated as one-way distance × 2. It won't be accurate for multi-stop days or one-way trips with a different return route.
- Only the first trip that would breach your safety floor drives the target-SoC calculation. If you have several trips in a row, later ones aren't separately accounted for once the first is covered.
- Relies on your calendar events having a `location` field Google's Routes API can resolve to an address.
- This is a personal homelab project shared as-is — not actively maintained as a polished product. Issues and PRs are welcome but responses may be slow.

## License

MIT — see [LICENSE](LICENSE).
