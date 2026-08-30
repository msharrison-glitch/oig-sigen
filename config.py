"""
Shared .env loading and host resolution.

Lives apart from octopus.py so that probe.py and control.py -- which have
nothing to do with Octopus -- can pick up SIGEN_HOST without importing the
GraphQL client.

Stdlib only, like everything else here.
"""

from __future__ import annotations

from pathlib import Path

ENV_FILE = Path(__file__).with_name(".env")


class ConfigError(RuntimeError):
    """Missing or unusable .env configuration."""


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Minimal .env reader. Credentials never leave this machine."""
    if not path.exists():
        raise ConfigError(
            f"No {path.name} found. Copy .env.example beside this script:\n"
            f"    OCTOPUS_API_KEY=sk_live_...\n"
            f"    OCTOPUS_ACCOUNT_NUMBER=A-1234ABCD\n"
            f"    SIGEN_HOST=192.168.2.53"
        )
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def resolve_host(explicit: str | None) -> str:
    """Command-line host wins; otherwise fall back to SIGEN_HOST in .env.

    Kept strict on purpose: guessing a plant address is not a thing we want
    to do, so an unresolvable host is an error rather than a default.
    """
    if explicit:
        return explicit
    try:
        host = load_env().get("SIGEN_HOST", "").strip()
    except ConfigError:
        host = ""
    if not host:
        raise ConfigError(
            "No host given and SIGEN_HOST is not set in .env. "
            "Pass the plant address on the command line, e.g. "
            "192.168.2.53"
        )
    return host
