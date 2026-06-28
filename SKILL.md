---
name: reservation-autopilot
description: |
  Check restaurant reservation availability across Resy / OpenTable /
  SevenRooms, and (with explicit user approval) book. Drop-window racer
  for hard-to-get NYC tables (Carbone, Polo Bar, 4 Charles, Don Angie,
  Torrisi, Via Carota). Use whenever Diego asks about restaurant
  reservations, asks to check availability at a specific restaurant,
  or asks about "the autopilot" / "the drop". READ-ONLY by default;
  booking requires explicit per-attempt approval.
---

# Reservation Autopilot

A cron-driven racer that checks Resy / OpenTable / SevenRooms at the
exact second new tables drop, plus an on-demand availability checker
Tina can run anytime.

## The tool

`./reservation_autopilot.py` — Python 3, requires `requests`.

```bash
# Check all enabled venues against drop windows
python3 ./reservation_autopilot.py --mode all

# Preflight (auth/sanity check before drop windows)
python3 ./reservation_autopilot.py --mode preflight

# Race a specific cohort at drop time (e.g. 9 AM ET cohort)
python3 ./reservation_autopilot.py --mode window --cohort 9am

# Nightly sweep across every enabled venue (covers slower-drop spots)
python3 ./reservation_autopilot.py --mode sweep
```

All modes emit JSON to stdout. The `run.sh` wrapper also tees to a
dated log in `logs/run-YYYYMMDD-HHMMSS.json`.

## Discover a new venue's IDs

```bash
python3 ./discover_venue.py "Restaurant Name"
python3 ./discover_venue.py "Cote" --platform opentable
```

Prints the Resy `venueId` / OpenTable `rid` to drop into
`config.json`.

## Config schema

See `config.example.json`. Per-venue keys:

| Key | Required | Notes |
|---|---|---|
| `slug` | yes | URL-safe id (e.g. `4-charles-prime-rib`) |
| `name` | yes | Display name |
| `platform` | yes | `resy` \| `opentable` \| `sevenrooms` |
| `resyVenueId` / `rid` | yes (platform-specific) | Use `discover_venue.py` to find |
| `cohort` | yes | Matches a `windows.<cohort>` key (e.g. `9am`) |
| `releaseHourET` | yes | When the venue drops (e.g. `09:00`) |
| `horizonDays` | yes | How far out to look (Carbone=30, Don Angie=7, ...) |
| `partySize` | yes | Usually 2 |
| `enabled` | yes | `false` to disable without removing |
| `apiKey` | resy only | User-specific Resy JWT (see README) |

## Hard rules (NON-NEGOTIABLE)

- **READ-ONLY by default.** All modes above only CHECK availability;
  they never book. Booking is a separate explicit step the user must
  approve per-attempt.
- **Per-attempt approval gate.** Diego must explicitly approve EACH
  (venue, date, time, party-size) tuple before any book call. Choosing
  a cohort/filter/priority ("the 9am ones") is NOT approval — it only
  decides what to *propose*. Always: list the exact slots → wait for
  explicit "yes, book that one" → submit only that one. Never
  bulk-book.
- **Never log or echo the per-user Resy JWT** (`apiKey` in config).
  Treat config.json like a credential file (chmod 600).
- **No cancellation automation.** If Diego needs to cancel, tell him
  to do it in the Resy/OpenTable app.
- **Respect drop-windows.** Hammering venues outside their release
  window wastes API budget and risks rate-limits; use `--mode sweep`
  for off-window checks and `--mode window --cohort <name>` only at
  the right minute.

## Surfacing results

When Diego asks "is there a Carbone slot Friday?", run:
```bash
python3 ./reservation_autopilot.py --mode all
```
Then filter the JSON for `slug=carbone` and surface any slots within
the next 7 days. If nothing, say so explicitly; don't fabricate.

## Failure modes seen in production

- **OT session expired** — `preflight` reports `ot.ok=false` with
  `expiresAt`. Surface to Diego with: "OpenTable session expired
  YYYY-MM-DD; cohort venues on OT are skipped until refreshed."
- **Resy auth not found in heartbeat-state.json** — the per-venue
  `apiKey` is read at runtime; if missing the venue is silently
  skipped. Check `config.json` for that venue.
- **403 / 429 from Resy** — the JWT was rejected. Likely expired
  (`exp` claim in the JWT). Diego must re-mint in his Resy app and
  paste the new value.

## Migration from earlier deployment

This skill is a portable copy. The original production deployment at
`/root/.openclaw/workspace/tools/reservation_autopilot.py` is still
active and wired into the system crontab — DO NOT duplicate the cron
jobs unless deploying to a fresh environment. See `README.md` for
full setup instructions.
