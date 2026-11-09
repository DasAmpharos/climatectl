"""`climatectl status`: read-only snapshot of every thermostat. Changes nothing."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import providers
from .envfile import ConfigError
from .providers import ProviderError
from .thermostat import COOL, COOLING, HEAT, HEAT_COOL, HEATING, IDLE, Thermostat

HVAC_NOW = {HEATING: "heating now", COOLING: "cooling now", IDLE: "idle"}


def fmt_temp(celsius: float | None) -> str:
    if celsius is None:
        return "?"
    return f"{celsius * 9 / 5 + 32:.0f}°F ({celsius:.1f}°C)"


def render(t: Thermostat) -> str:
    header = t.name if t.online is not False else f"{t.name} (OFFLINE: readings may be stale)"
    indoor = fmt_temp(t.ambient_c)
    if t.humidity_pct is not None:
        indoor += f", {t.humidity_pct:.0f}% humidity"
    system = f"{t.mode or '?'} mode, {HVAC_NOW.get(t.hvac or '', t.hvac or '?')}"

    if t.eco:
        target = (
            f"Eco: heat {fmt_temp(t.eco_heat_c)}, cool {fmt_temp(t.eco_cool_c)} "
            "(normal setpoints paused)"
        )
    elif t.mode == HEAT:
        target = f"heat to {fmt_temp(t.heat_c)}"
    elif t.mode == COOL:
        target = f"cool to {fmt_temp(t.cool_c)}"
    elif t.mode == HEAT_COOL:
        target = f"heat to {fmt_temp(t.heat_c)}, cool to {fmt_temp(t.cool_c)}"
    else:
        target = "none (system off)"

    return "\n".join(
        [header, f"  Indoor   {indoor}", f"  System   {system}", f"  Target   {target}"]
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="climatectl status", description=__doc__)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        provider = providers.from_settings()
        found = provider.thermostats()
    except (ConfigError, ProviderError) as e:
        sys.exit(str(e))
    if not found:
        hint = provider.hints.get("no_thermostats", "")
        sys.exit(f"{provider.name} returned no thermostats. {hint}".rstrip())
    if args.json:
        print(json.dumps([asdict(t) for t in found], indent=2))
    else:
        print("\n\n".join(render(t) for t in found))


if __name__ == "__main__":
    main()
