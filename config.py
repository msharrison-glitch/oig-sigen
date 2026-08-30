"""
Shared .env loading and host resolution.

Lives apart from octopus.py so that probe.py and control.py -- which have
nothing to do with Octopus -- can pick up SIGEN_HOST without importing the
GraphQL client.

Stdlib only, like everything else here.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).with_name(".env")


class ConfigError(RuntimeError):
    """Missing or unusable .env configuration."""


# Keys we will take from the process environment. Anything else has to come
# from .env, so a stray shell variable can never silently become config.
ENV_KEYS = ("OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT_NUMBER", "SIGEN_HOST",
            "IOG_OFF_PEAK_P", "IOG_PEAK_P")


def state_path(name: str) -> Path:
    """Where mutable state belongs.

    Beside the scripts normally, which is what the cron deadman and the
    systemd unit expect. In a container that directory is inside the image
    and vanishes on restart -- taking .lease.json, the record of what we
    commanded, with it -- so OIG_STATE_DIR redirects it to a volume.
    """
    directory = os.environ.get("OIG_STATE_DIR")
    if directory:
        return Path(directory) / name
    return Path(__file__).with_name(name)


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Config from .env, overridden by the process environment.

    The file is the normal case. The environment matters for containers,
    where bind-mounting a dotfile to inject two secrets is awkward and
    `docker run -e` is the idiom. Environment wins, so a compose file can
    override a baked-in .env without editing it.

    Credentials are read here and never leave the machine.
    """
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")

    for key in ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value

    if not env:
        raise ConfigError(
            f"No configuration found. Either create {path.name} beside this "
            f"script or set the variables in the environment:\n"
            f"    OCTOPUS_API_KEY=sk_live_...\n"
            f"    OCTOPUS_ACCOUNT_NUMBER=A-1234ABCD\n"
            f"    SIGEN_HOST=192.168.2.53"
        )
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
