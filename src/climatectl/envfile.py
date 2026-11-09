"""Read and update the project's .env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, find_dotenv


def env_path() -> Path:
    """CLIMATECTL_ENV_FILE if set, else the nearest .env at or above the working directory."""
    explicit = os.environ.get("CLIMATECTL_ENV_FILE")
    if explicit:
        return Path(explicit)
    found = find_dotenv(usecwd=True)
    return Path(found) if found else Path.cwd() / ".env"


class ConfigError(RuntimeError):
    pass


def load(path: Path) -> dict[str, str]:
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def read_settings(keys: tuple[str, ...]) -> dict[str, str]:
    """Values for `keys` from .env, overridden by real environment variables (as a container
    or systemd sets them). Keys with no value are left out."""
    path = env_path()
    values = load(path) if path.exists() else {}
    values.update({k: os.environ[k] for k in keys if os.environ.get(k)})
    return {k: values[k] for k in keys if values.get(k)}


def set_value(path: Path, key: str, value: str) -> None:
    """Replace or append KEY=value, keep every other line as it was, and leave the file
    readable by its owner only (it holds secrets)."""
    lines = path.read_text().splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
