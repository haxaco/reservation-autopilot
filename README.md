# Reservation Autopilot

Drop-window racer + on-demand availability checker for restaurant
reservations across **Resy**, **OpenTable**, and **SevenRooms**.

Designed to be dropped into another agent's workspace as a standalone
skill. All paths are relative to this directory; no hardcoded
`/root/.openclaw/...` references.

> 🍝 **If this lands you a Carbone slot, star the repo.**

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | Agent operating contract — when to use, hard rules |
| `README.md` | This file (human setup) |
| `reservation_autopilot.py` | The brain — checks availability, emits JSON |
| `discover_venue.py` | Helper to find Resy `venueId` / OpenTable `rid` by name |
| `run.sh` | Wrapper that runs the autopilot + tees output to `./logs/` |
| `config.example.json` | Schema reference with placeholder values |
| `crontab.example` | Suggested cron schedule for drop-window racing |

## Quick start

1. **Install dependencies**

   ```bash
   pip3 install requests
   ```

2. **Copy + edit the config**

   ```bash
   cp config.example.json config.json
   chmod 600 config.json
   ```

   Edit `config.json` to:
   - Replace `proxyBase` / `resyProxyBase` with your CF Workers proxy
     URLs (or strip them and the code will fall back to direct API
     where available — note Resy direct calls from datacenter IPs
     are usually blocked).
   - Add venues to the `venues` list (use `discover_venue.py` to find
     IDs).
   - For each Resy venue, paste a fresh Resy JWT into `apiKey`. See
     "Getting a Resy JWT" below.

3. **Test preflight**

   ```bash
   python3 ./reservation_autopilot.py --mode preflight
   ```
   Verifies all platforms are reachable and sessions are valid.

4. **Run a sweep**

   ```bash
   ./run.sh --mode sweep
   ```
   Checks every enabled venue once. Output JSON to stdout + saved
   to `./logs/run-YYYYMMDD-HHMMSS.json`.

5. **Wire up cron** (optional — for drop-window racing)

   See `crontab.example`. Install with:
   ```bash
   crontab -l > /tmp/old.cron
   cat /tmp/old.cron crontab.example | crontab -
   ```

## Getting a Resy JWT

Resy's API requires a JWT per user account. To extract yours:

1. Sign into resy.com in a browser.
2. Open DevTools → Network → make any logged-in action (e.g. open
   your account page).
3. Find a request to `api.resy.com`.
4. Copy the `Authorization` request header. It looks like:
   `ResyAPI api_key="...", auth_token="eyJ0eXAiOiJKV..."`
5. Paste the `auth_token` value (everything between the quotes after
   `auth_token="`) into your config's `apiKey` field.

The JWT expires (check the `exp` claim — typically ~6 months out).
When it expires, Resy returns 403/429 errors and you re-mint.

## Getting an OpenTable session

OpenTable doesn't expose a clean API. The production deployment uses
a CF Workers proxy that holds an authenticated session. To replicate:

1. Set up an OpenTable account.
2. Build a small CF Worker that proxies to `https://www.opentable.com/`
   with session cookies attached. (Or use the production proxy URL
   if Diego shares it.)
3. Put the worker URL in `config.json` as `proxyBase`.

If you skip this, OpenTable venues will fail with `OT session
expired`; Resy and SevenRooms will continue to work.

## SevenRooms

No setup required. The autopilot queries the public widget API.

## Cron windows explained

The production deployment runs each cohort window 5× in 10 minutes
(at minute 55, 59, 00, 02, 05 around the drop hour). This redundancy
is intentional — if any single request hits a rate limit, the others
still get a shot at the freshly-dropped tables. Tune for your venue
mix; remove the redundancy if you're only watching slow-drop
restaurants.

## Sample output

**`--mode preflight`** — checks each platform is reachable:

```json
{
  "timestamp": "2026-06-28T04:30:02.491391-04:00",
  "mode": "preflight",
  "session": {
    "ot": {
      "ok": false,
      "error": "OT session expired",
      "expiresAt": "2026-04-01T18:03:45.503000+00:00"
    },
    "resy": { "ok": true, "error": null }
  },
  "resy":       { "ok": true,  "error": null },
  "sevenrooms": { "ok": true,  "error": null },
  "enabledVenues": 26,
  "disabledVenues": 20
}
```

**`--mode sweep`** — checks all enabled venues across their horizon
window; each result is one (venue, date) probe:

```json
{
  "timestamp": "2026-06-28T20:05:54.963175-04:00",
  "mode": "sweep",
  "results": [
    {
      "venue": "Example Bistro",
      "slug": "example-bistro",
      "platform": "resy",
      "date": "2026-07-04",
      "status": "checked",
      "slotsFound": 2,
      "slots": [
        { "time": "19:00", "type": "Dining Room",  "partySize": 2 },
        { "time": "19:30", "type": "Bar Counter",  "partySize": 2 }
      ]
    },
    {
      "venue": "Example Bistro",
      "slug": "example-bistro",
      "platform": "resy",
      "date": "2026-07-05",
      "status": "checked",
      "slotsFound": 0,
      "slots": []
    }
  ]
}
```

For hits, filter the array for `slotsFound > 0`:

```bash
./run.sh --mode sweep | jq '.results[] | select(.slotsFound > 0)'
```

## Logs + retention

`run.sh` saves each invocation to `./logs/run-<stamp>.json` and
auto-deletes logs older than 7 days. Override the log dir with:
```bash
RESERVATION_AUTOPILOT_LOGDIR=/some/other/path ./run.sh ...
```

## Operating safety

- **Read-only is safe.** All `--mode` flags above only CHECK; they
  don't book.
- **Booking** lives in a separate codepath inside
  `reservation_autopilot.py` that requires explicit per-attempt
  approval. See SKILL.md for the agent operating contract.
- **Never check config.json into git** — it holds your Resy JWT.

## Known limitations

- **No SMS / push** on hit — you currently see hits only when running
  the script (cron logs them but doesn't alert). Wire your own
  notifier reading `./logs/run-*.json` for the freshest result.
- **No automatic JWT refresh.** Resy's flow uses a refresh token but
  the autopilot doesn't currently use it; you manually re-mint when
  the JWT expires.
- **OpenTable proxy is your problem.** No bundled solution.
