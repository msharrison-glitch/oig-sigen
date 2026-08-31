# Running on dedicated hardware

Docker, multi-arch. It is the only packaging that runs unchanged on a
Synology, a QNAP, Unraid, a mini-PC and a Pi, and this project is pure
stdlib Python so there are no wheels to compile and no per-architecture
special cases.

```sh
docker buildx build --platform linux/amd64,linux/arm64 -t oig-sigen .
docker compose up -d
```

## Four things that specifically bite this project

**tzdata.** Alpine ships no timezone database. `ZoneInfo("Europe/London")`
raises at import, so the agent never starts — and everything here turns on
local time: the 23:30–05:30 window, BST/GMT transitions, slot display. The
image installs `tzdata` and sets `TZ`. If you swap the base image, keep both.

**The state volume is not optional.** `.lease.json` is the record of what we
commanded. Inside the image it dies with the container, and a deadman that
finds no lease file silently protects nothing — the worst failure mode this
project has, because it looks fine. `OIG_STATE_DIR=/state` redirects it to a
volume; a host install leaves it beside the scripts, where the cron deadman
and the systemd unit expect it.

**Restart must release first.** A container restarting after a crash may be
inheriting a lease its dead predecessor took out — the plant has no watchdog,
so that command is still latched. The entrypoint runs `control.py --deadman`
before starting the loop, which makes a restart self-healing rather than a
way to lose track of the plant.

**Do not co-locate the watchdog.** It exists to notice this host dying. The
compose file runs both so it is demonstrable, with a warning; in earnest put
the watchdog on a VPS or another box.

## Configuration

Environment beats `.env`, so a compose file overrides without editing
anything:

| Variable | Purpose |
|---|---|
| `SIGEN_HOST` | plant address on the LAN |
| `OCTOPUS_API_KEY` | your key — stays on your machine |
| `OCTOPUS_ACCOUNT_NUMBER` | `A-XXXXXXXX` |
| `IOG_OFF_PEAK_P` / `IOG_PEAK_P` | your region's rates, for the cost summary |
| `TZ` | `Europe/London` |
| `OIG_STATE_DIR` | `/state` in the image; unset on a host install |

Networking is plain bridge — it needs the plant on your LAN and
`api.octopus.energy`. No host networking, no privileges, and it runs as a
non-root user.

## NAS specifics

Synology and QNAP both ship Docker (Container Manager / Container Station)
and will run the compose file directly. Give the agent a fixed IP or make
sure it can route to the plant's subnet. Unraid: Community Applications will
take the compose file, or add it as a custom container with the volume set.

## What this does not solve

If the host dies holding a lease, nothing here releases it — the deadman and
the entrypoint both die with it. That is what the ESS charge cut-off SOC
backstop is for; see the SOC cut-off registers in `registers.py`, which are
deliberately read-only until their ordering is confirmed. Do not run this
unattended on dedicated hardware until that exists.
