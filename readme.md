# Dynamic ESS (venus-dynamicess)

A service for Victron Venus OS (GX devices) that runs Dynamic ESS locally on the GX. It follows a charge/discharge schedule, usually delivered by VRM, and steers the ESS system to match it. It runs as its own D-Bus service, `com.victronenergy.dynamicess`.

The schedule consists of up to 48 windows. Each window has a start time, a duration, a target SoC and a strategy, plus optional restrictions, flags and EV charger instructions. The service finds the active window, calculates the charge rate needed to reach the target SoC by the end of the window, and sets the ESS setpoints accordingly. It supports VE.Bus and Multi RS systems.

If something is wrong (no matching window, invalid target, unexpected state), it falls back to self-consumption and reports the reason via `/ErrorCode` and `/ReactiveStrategy`.

## Strategies

Each strategy has two coping mechanisms. They define how to deal with **missing** or **excess** energy compared to the linear charge rate towards the target SoC.

| Strategy | Value | Missing/Excess coping | Description | Target SoC implication |
|---|---|---|---|---|
| `TARGETSOC` | 0 | grid/grid | All deviations from the ideal charge rate are taken from / sent to the grid. | Target SoC is guaranteed to be reached. |
| `SELFCONSUME` | 1 | bat/bat | All deviations from the ideal charge rate are taken from / sent to the battery. | There is no target SoC evaluation (plain ESS). |
| `PROBATTERY` | 2 | grid/bat | Missing energy is taken from the grid. Excess energy is sent to the battery. | SoC will be >= target SoC by the end of the window. |
| `PROGRID` | 3 | bat/grid | Missing energy is taken from the battery. Excess energy is sent to the grid. | SoC will be <= target SoC by the end of the window. |

## Requirements

- A GX device with a supported ESS (VE.Bus or Multi RS) and ESS mode set up.
- **`BatteryCapacity` must be set.** While it is 0, the service reports `BATTERY_CAPACITY_UNSET` and does nothing.
- A schedule source, such as VRM, that writes the schedule settings.

## Where to look

- **Settings and overrides:** `CONFIGURABLES` in [globals.py](globals.py) lists every setting with its path, default and range. The 48 per-window settings (`/Settings/DynamicEss/Schedule/{i}/...`) are registered in `_init_settings` in [dynamicess.py](dynamicess.py).
- **Enums:** error codes, capabilities, strategies, restrictions, flags and `ReactiveStrategy` are in [globals.py](globals.py). `ReactiveStrategy` explains what the controller is doing right now: 1-23 are normal operation (charge, discharge, self-consume and idle variants), 90-99 are errors and fallbacks. Its numbering is stable, because external applications rely on it.
- **D-Bus paths:** the service's own paths are created in `_init_dbus_service` in [dynamicess.py](dynamicess.py).

## Development

Run the tests:

```bash
python3 -m unittest discover
```

`aiovelib` is included as a git submodule under `ext/aiovelib`. Clone with `--recurse-submodules`, or run `git submodule update --init`.
