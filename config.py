"""
Shared .env loading and host resolution.

Lives apart from octopus.py so that probe.py and control.py -- which have
nothing to do with Octopus -- can pick up SIGEN_HOST without importing the
GraphQL client.

Stdlib only, like everything else here.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

ENV_FILE = Path(__file__).with_name(".env")


# IOG import unit rates, pence per kWh, AS CHARGED -- whatever VAT is in
# force at the time is already in these numbers.
#
# These two are the BASELINE: the rate before any change in IOG_RATE_CHANGES.
# They still read 4.49/29.757 because that is what this account paid until
# 2026-10-01, when VAT on domestic electricity went to zero.
DEFAULT_OFF_PEAK_P = 4.49
DEFAULT_PEAK_P = 29.757


def rate_schedule(env=None) -> list:
    """Import rates with the date each took effect, oldest first.

    A SINGLE pair of rates cannot price a range that spans a change, and
    this project's ranges routinely do -- "this month" and "last 7 days"
    both straddled 2026-10-01, when VAT on domestic electricity went to
    zero, and 2026-10-31, when this account's tariff was re-priced. Editing
    one scalar would have silently re-priced September at November's rates:
    no error, no empty result, just a wrong number in a column of money.

    Export never needed this because Agile Outgoing is already fetched per
    half hour; import is the side that was a constant. This gives it the
    same shape.

    IOG_RATE_CHANGES is a comma or semicolon separated list of
    `YYYY-MM-DD:off/peak`, each taking effect at LOCAL MIDNIGHT on that date:

        IOG_RATE_CHANGES=2026-10-01:4.279/28.3429, 2026-10-31:7.6/34.955

    A malformed entry is skipped rather than raised on, because the
    dashboard must degrade rather than die -- but costs.py prints every
    period it is using, so a dropped entry shows up as a missing row rather
    than as a quietly wrong total.
    """
    if env is None:
        try:
            env = load_env()
        except ConfigError:
            env = {}

    def number(key, default):
        try:
            return float(env.get(key, "") or default)
        except ValueError:
            return default

    schedule = [(date.min,
                 number("IOG_OFF_PEAK_P", DEFAULT_OFF_PEAK_P),
                 number("IOG_PEAK_P", DEFAULT_PEAK_P))]

    raw = (env.get("IOG_RATE_CHANGES") or "").replace(";", ",")
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            when, pair = item.split(":", 1)
            off, peak = pair.split("/", 1)
            schedule.append((date.fromisoformat(when.strip()),
                             float(off), float(peak)))
        except ValueError:
            continue              # skipped, and visible in costs.py's output
    schedule.sort(key=lambda row: row[0])
    return schedule


def rates_at(schedule, when) -> tuple:
    """The (off-peak, peak) rates in force on that LOCAL date.

    Boundaries are local midnight, so this compares dates and never does
    timezone arithmetic -- which is also why it cannot be caught out by a
    host whose zone is Europe/Dublin rather than Europe/London.
    """
    if hasattr(when, "date"):
        when = when.date()
    chosen = schedule[0]
    for row in schedule:
        if row[0] <= when:
            chosen = row
        else:
            break
    return chosen[1], chosen[2]


# Floor for any configured poll interval. Below roughly this, most of the
# delay is the plant's own actuation latency (5-25 s to release), so faster
# polling buys nothing and only adds load.
MIN_POLL_SECONDS = 15.0


class ConfigError(RuntimeError):
    """Missing or unusable .env configuration."""


# Keys we will take from the process environment. Anything else has to come
# from .env, so a stray shell variable can never silently become config.
ENV_KEYS = ("OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT_NUMBER", "SIGEN_HOST",
            "IOG_OFF_PEAK_P", "IOG_PEAK_P", "IOG_RATE_CHANGES",
            "IOG_POLL_IDLE_SECONDS", "IOG_POLL_CHARGING_SECONDS",
            "IOG_RESUME_BAND_PCT", "IOG_RESTORE_MODE",
            "IOG_RESTORE_PROFILE", "IOG_STATE_DIR",
            "IOG_HEARTBEAT_URL", "IOG_SITE_TOKEN",
            "MYENERGI_SERIAL", "MYENERGI_API_KEY", "MYENERGI_USER_AGENT",
            "SIGEN_CLOUD_USERNAME", "SIGEN_CLOUD_PASSWORD",
            "SIGEN_CLOUD_REGION",
            "DAIKIN_CLIENT_ID", "DAIKIN_CLIENT_SECRET",
            "DAIKIN_REDIRECT_URI")


def state_path(name: str) -> Path:
    """Where mutable state belongs.

    Beside the scripts normally, which is what the cron deadman and the
    systemd unit expect. In a container that directory is inside the image
    and vanishes on restart -- taking .lease.json, the record of what we
    commanded, with it -- so IOG_STATE_DIR redirects it to a volume.
    """
    directory = os.environ.get("IOG_STATE_DIR") or os.environ.get(
        "OIG_STATE_DIR")   # OIG_ was the old spelling; accepted, undocumented
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
            f"    SIGEN_HOST=192.168.1.100"
        )
    return env


def poll_seconds(key: str, default: float) -> float:
    """A poll interval from .env, floored so a typo cannot hammer Octopus.

    These ship to other people, all of them on the same tariff, so their
    slots start at the same moments. A shared minimum keeps one careless
    value from turning a dozen installations into a synchronised burst.
    """
    try:
        value = float(load_env().get(key, "") or default)
    except (ValueError, ConfigError):
        return default
    if value < MIN_POLL_SECONDS:
        print(f"warning: {key}={value:g}s is below the {MIN_POLL_SECONDS:g}s "
              f"minimum; using {MIN_POLL_SECONDS:g}s. Below this you are "
              f"waiting on actuation latency, not detection.")
        return MIN_POLL_SECONDS
    return value


def env_setting(key: str) -> str | None:
    """A plain string setting from .env, or None if unset or unreadable.

    Deliberately quiet: a missing .env is not an error for an optional
    setting, and an agent that refused to start because nobody had configured
    a watchdog would be trading a real capability for a hypothetical one.
    """
    try:
        value = (load_env().get(key) or "").strip()
    except ConfigError:
        return None
    return value or None


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
            "192.168.1.100"
        )
    return host


def log_files(path) -> list:
    """The agent log plus its rotated archives, OLDEST FIRST.

    `deploy/synology/rotate-log.sh` copies observe.log to
    observe-YYYY-MM-DD.log and truncates the original in place, so after the
    first rotation the current file holds only this month and everything
    before it lives in archives. Anything that reads history -- costs.py,
    heatreport.py, the dashboard -- has to read both or it silently loses
    every slot older than the last rotation.

    The archive suffix is the DATE OF ROTATION, not a claim about the months
    inside: the first rotation of an existing log carries whatever was
    already there. Names sort chronologically, which is why they sort here.
    """
    p = Path(path)
    pattern = f"{p.stem}-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]{p.suffix}"
    files = sorted(str(a) for a in p.parent.glob(pattern))
    if p.exists():
        files.append(str(p))
    return files
