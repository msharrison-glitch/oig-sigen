# The cloud side

One watchdog, many sites. It observes and alarms; it cannot command anything.

## Why it exists

The agent on each site handles its own exceptions, signals, hard kills and
expired leases. It cannot handle its own host dying, because systemd, cron and
the signal handlers die with it — and the SigenStor has no Modbus watchdog, so
whatever mode was latched stays latched. Off-box observation is the only thing
that catches that.

It has no control path on purpose. A compromised or broken watchdog must never
be able to make a plant *less* safe than no watchdog at all, so releasing a
stuck plant stays a human decision or the site's own cron deadman.

## Running it

```sh
python3 server.py --add-site home        # mint a token; shown once
python3 server.py --serve --port 8080    # loopback by default
python3 server.py --status               # what it currently sees
```

Dashboard at `/` — HTTP Basic, any username, password is the admin token.
JSON at `/v1/sites` — bearer auth. `deploy/oig-watchdog.service` runs it under
systemd. Put a TLS terminator in front rather than binding `0.0.0.0`.

**Do not run it on a host that runs an agent.** Noticing that host die is the
entire point.

## Severities

| | Meaning |
|---|---|
| `OK` | seen within 15 min |
| `WARN` | silent, but held nothing — availability problem only |
| `CRITICAL` | **silent while holding a lease** — the plant may be importing at peak with nobody watching |
| `UNKNOWN` | registered, never reported |

Only `CRITICAL` means money is burning.

## Onboarding another site

```sh
python3 server.py --add-site <name>            # on the watchdog host
```

Then on their machine, per `deploy/README.md`, with the agent configured to
report in:

```sh
python3 reconcile.py --heartbeat-url https://<watchdog>/v1/heartbeat \
                     --site-token <their token>
```

The token authenticates that site and nothing else. Only its SHA-256 is
stored, so a stolen database does not yield working tokens, and revoking a
site is a row delete.

## What is NOT solved, and must be before this is offered to anyone

**Octopus credential custody.** Every site needs its own `OCTOPUS_API_KEY` to
read its dispatch schedule, and today that key sits in plaintext `.env` on the
customer's own machine. That is fine while the customer is you. It is a
different proposition once you hold, or your software handles, other people's
account credentials — those keys are not scoped to dispatch data, and Octopus
has no per-application revocation.

Options, none of them implemented:

- Keep the key strictly on the customer's host, never transiting the cloud.
  The agent stays autonomous and the watchdog only ever sees health. This is
  the current architecture and the one that avoids the problem entirely.
- Move schedule fetching server-side, which is more convenient and means
  holding customer keys. Do not do this without deciding deliberately what
  that obliges you to.

**No per-site alerting.** `CRITICAL` is visible on the dashboard and the API;
nothing pushes it anywhere. A watchdog nobody looks at is decoration.

**No agent auto-update.** Sites would run whatever version they installed.
