# Multi-arch by construction: pure stdlib Python, no wheels to compile, so
# the same Dockerfile builds for amd64 (mini-PC, Synology Plus, Unraid) and
# arm64 (Pi, ARM NAS) with no per-arch special cases.
#
#   docker buildx build --platform linux/amd64,linux/arm64 -t oig-sigen .
FROM python:3.12-alpine

# zoneinfo has no database of its own on Alpine, and everything here turns on
# Europe/London -- the 23:30-05:30 window, BST/GMT transitions, slot display.
# Without tzdata, ZoneInfo("Europe/London") raises at import and the agent
# never starts. This is the single most common way this image gets broken.
RUN apk add --no-cache tzdata
ENV TZ=Europe/London

# Not root. It talks to the internet and to a device that controls a battery.
RUN adduser -D -h /app oig
WORKDIR /app

COPY --chown=oig:oig *.py /app/
COPY --chown=oig:oig cloud/ /app/cloud/
COPY --chown=oig:oig docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

# State lives here and MUST be a volume. .lease.json is the record of what we
# commanded; if it is lost on restart the deadman finds nothing and silently
# protects nothing. .octopus-token.json is a cache and merely nice to keep.
RUN mkdir -p /state && chown oig:oig /state
VOLUME /state
ENV IOG_STATE_DIR=/state

USER oig

# Unbuffered, so `docker logs` shows a tick as it happens rather than when a
# buffer fills. On a loop that ticks twice an hour that is the difference
# between logs and silence.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["--kw", "5", "--bonus-only"]
