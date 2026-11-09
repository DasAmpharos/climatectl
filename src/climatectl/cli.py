"""`climatectl`: one command for every tool, e.g. `climatectl logger --once`. Each command
has its own --help."""

from __future__ import annotations

import argparse
import importlib
from importlib.metadata import PackageNotFoundError, version

COMMANDS = {
    "logger": "climatectl.logger_cli",
    "status": "climatectl.status_cli",
    "nest-auth": "climatectl.providers.nest.auth_cli",
}


def _version() -> str:
    try:
        return version("climatectl")
    except PackageNotFoundError:
        return "unknown"


def _summary(module: str) -> str:
    """A command module's first docstring line, without its "`climatectl x`: " prefix."""
    first = importlib.import_module(module).__doc__.splitlines()[0]
    return first.split("`: ", 1)[-1]


def main(argv: list[str] | None = None) -> None:
    summaries = "\n".join(f"  {name:10} {_summary(module)}" for name, module in COMMANDS.items())
    parser = argparse.ArgumentParser(
        prog="climatectl",
        description="Thermostat, weather and HVAC logging with fault alerts.",
        epilog=f"commands:\n{summaries}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"climatectl {_version()}")
    parser.add_argument("command", choices=COMMANDS, metavar="command")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="passed to the command")
    args = parser.parse_args(argv)
    importlib.import_module(COMMANDS[args.command]).main(args.args)


if __name__ == "__main__":
    main()
