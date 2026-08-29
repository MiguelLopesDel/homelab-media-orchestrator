# Homelab media-language orchestration

Portable reference implementation for auditing an anime library per episode,
resolving PT-BR dub availability, acquiring a verified dubbed torrent,
aligning it conservatively, and importing it by hardlink without
changing a seeding video.

## What is deliberately excluded

This is a clean public snapshot, not a backup of a running server. It excludes
application databases, inventories, disk layout, hostnames, media lists,
container state, private automation history, credentials, API keys, webhook
URLs and complete application configuration.

Copy `.env.example` to `.env`, set `HOMELAB_ROOT`, `CONFIG`, and `DATA`, and
place a local override at `config/dub-availability/dublagem.json` using the
provided example. Do not commit that override if it records personal media
history or private provider data.

## Safety properties

- Original library videos are read-only inputs.
- A verified dubbed torrent at 720p or above replaces the library filename by
  hardlink; its torrent member remains untouched and seedable.
- PT-BR audio is published as a validated external sidecar only for an
  out-of-band source or a dubbed source below 720p.
- A completed mapped season can inherit a PT-BR dub; airing seasons remain
  episode-specific, and specials/films do not inherit a main-season status.
- Ambiguous audio, episode mappings and timeline alignment stop for review.

See `docs/ARCHITECTURE.md` and `docs/DUB-AUTOMATION.md` for the model.
