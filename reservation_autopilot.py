#!/usr/bin/env python3
"""Reservation Autopilot — checks availability across Resy, OpenTable, and SevenRooms.

Usage:
    python3 tools/reservation_autopilot.py --mode preflight
    python3 tools/reservation_autopilot.py --mode window --cohort 9am
    python3 tools/reservation_autopilot.py --mode sweep
    python3 tools/reservation_autopilot.py --mode all
"""

import argparse
import json
import os
import sys
import threading
import time as time_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Resy's platform-public client API key (the static value Resy embeds
# in their own browser code at resy.com). NOT user-specific auth —
# per-user JWT goes in config.json per venue. Safe to commit.
RESY_API_KEY = 'VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5'
RESY_HEADERS = {
    'Authorization': f'ResyAPI api_key="{RESY_API_KEY}"',
    'User-Agent': 'Mozilla/5.0',
}
# NYC coordinates (required by Resy API — 0,0 returns 500)
NYC_LAT = 40.7128
NYC_LONG = -74.0060

SEVENROOMS_URL = 'https://www.sevenrooms.com/api-yoa/availability/widget/range'

WORKSPACE = Path(__file__).resolve().parent.parent
CONFIG_PATH = WORKSPACE / 'config' / 'reservation-autopilot.json'
OT_SESSION_PATH = WORKSPACE / 'memory' / 'ot-session.json'
HEARTBEAT_STATE_PATH = WORKSPACE / 'memory' / 'heartbeat-state.json'

REQUEST_TIMEOUT = 15
PLATFORM_CONCURRENCY = 3
THREAD_POOL_WORKERS = 10
RESY_REQUEST_DELAY = 0.6  # seconds between Resy API calls (avoid 429s)

# ---------------------------------------------------------------------------
# Config loading & validation
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    """Structural validation — no hardcoded venue lists."""
    required_venue_fields = {'slug', 'name', 'platform', 'cohort', 'releaseHourET',
                             'horizonDays'}
    windows = cfg.get('windows', {})
    if not windows:
        raise ValueError('Config missing "windows"')

    venues = cfg.get('venues', [])
    if not venues:
        raise ValueError('Config missing "venues"')

    cohorts_seen = set()
    for i, v in enumerate(venues):
        missing = required_venue_fields - set(v.keys())
        if missing:
            raise ValueError(f'Venue #{i} ({v.get("slug", "?")}) missing fields: {missing}')

        if v['platform'] == 'resy' and not isinstance(v.get('resyVenueId'), int):
            raise ValueError(f'Venue {v["slug"]}: resy venue requires int "resyVenueId"')
        if v['platform'] == 'opentable' and not isinstance(v.get('rid'), int):
            raise ValueError(f'Venue {v["slug"]}: opentable venue requires int "rid"')
        if v['platform'] == 'sevenrooms' and not isinstance(v.get('sevenroomsSlug'), str):
            raise ValueError(f'Venue {v["slug"]}: sevenrooms venue requires str "sevenroomsSlug"')

        cohort = v['cohort']
        if cohort not in windows:
            raise ValueError(f'Venue {v["slug"]}: cohort "{cohort}" has no matching window')
        cohorts_seen.add(cohort)

    # Warn (don't fail) if a window has no enabled venues
    for w in windows:
        enabled = [v for v in venues if v['cohort'] == w and v.get('enabled', True)]
        if not enabled:
            print(f'[warn] Window "{w}" has no enabled venues', file=sys.stderr)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

RETRIABLE_STATUS_CODES = {429, 500, 502, 503}


def retry_request(fn, max_retries=2, backoff=0.5):
    """Retry a request function on transient errors (500/502/503/429).

    Args:
        fn: callable that returns a requests.Response
        max_retries: number of retries after first attempt
        backoff: base backoff in seconds (doubles each retry)

    Returns:
        requests.Response from the last attempt
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = fn()
            if resp.status_code not in RETRIABLE_STATUS_CODES or attempt == max_retries:
                return resp
            time_mod.sleep(backoff * (2 ** attempt))
        except requests.RequestException as e:
            last_exc = e
            if attempt == max_retries:
                raise
            time_mod.sleep(backoff * (2 ** attempt))
    raise last_exc


# ---------------------------------------------------------------------------
# Resy auth management
# ---------------------------------------------------------------------------

def load_resy_auth() -> Optional[dict]:
    """Load Resy auth token from heartbeat-state.json.

    Returns dict with 'token' and 'expires' (epoch), or None if unavailable.
    """
    if not HEARTBEAT_STATE_PATH.exists():
        return None
    try:
        state = json.loads(HEARTBEAT_STATE_PATH.read_text())
        resy_auth = state.get('resyAuth')
        if not resy_auth or not resy_auth.get('token'):
            return None
        return {
            'token': resy_auth['token'],
            'expires': resy_auth.get('expires', 0),
        }
    except (json.JSONDecodeError, OSError):
        return None


def check_resy_auth() -> dict:
    """Check Resy auth token validity. Returns status dict."""
    auth = load_resy_auth()
    if not auth:
        return {'ok': False, 'error': 'Resy auth not found in heartbeat-state.json'}

    expires_epoch = auth['expires']
    now_epoch = int(datetime.now(timezone.utc).timestamp())

    if now_epoch > expires_epoch:
        return {'ok': False, 'error': 'Resy auth token expired',
                'expiresAt': datetime.fromtimestamp(expires_epoch, tz=timezone.utc).isoformat()}

    remaining_days = (expires_epoch - now_epoch) / 86400
    return {
        'ok': True,
        'remainingDays': int(remaining_days),
        'expiresAt': datetime.fromtimestamp(expires_epoch, tz=timezone.utc).isoformat(),
        'warn': remaining_days < 3,
    }


# ---------------------------------------------------------------------------
# OpenTable session management
# ---------------------------------------------------------------------------

def check_ot_session() -> dict:
    """Check OT session validity. Returns status dict."""
    if not OT_SESSION_PATH.exists():
        return {'ok': False, 'error': 'OT session file not found'}

    try:
        session = json.loads(OT_SESSION_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {'ok': False, 'error': f'Cannot read session: {e}'}

    expires_str = session.get('expiresAt')
    if not expires_str:
        # Calculate from extractedAt + 14 days
        extracted = session.get('extractedAt')
        if not extracted:
            return {'ok': False, 'error': 'No expiresAt or extractedAt in session'}
        expires_dt = datetime.fromisoformat(extracted.replace('Z', '+00:00')) + timedelta(days=14)
    else:
        expires_dt = datetime.fromisoformat(expires_str.replace('Z', '+00:00'))

    now = datetime.now(timezone.utc)
    if now > expires_dt:
        return {'ok': False, 'error': 'OT session expired', 'expiresAt': expires_dt.isoformat()}

    remaining = (expires_dt - now).days
    return {
        'ok': True,
        'remainingDays': remaining,
        'expiresAt': expires_dt.isoformat(),
        'warn': remaining < 3,
    }


def load_ot_session() -> Optional[dict]:
    """Load OT session credentials. Returns None if invalid."""
    if not OT_SESSION_PATH.exists():
        return None
    try:
        return json.loads(OT_SESSION_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Resy checker (API-based)
# ---------------------------------------------------------------------------

def check_resy(venue: dict, date: str, party_size: int,
               resy_proxy_base: str = '',
               resy_auth_token: str = '') -> dict:
    """Check Resy availability via proxy (or direct). Returns result dict."""
    venue_id = venue['resyVenueId']
    if resy_proxy_base:
        find_url = f'{resy_proxy_base}/4/find'
    else:
        find_url = 'https://api.resy.com/4/find'

    # Build headers with user auth token
    headers = dict(RESY_HEADERS)
    if resy_auth_token:
        headers['X-Resy-Auth-Token'] = resy_auth_token
        headers['X-Resy-Universal-Auth'] = resy_auth_token

    params = {
        'lat': NYC_LAT,
        'long': NYC_LONG,
        'day': date,
        'party_size': party_size,
        'venue_id': venue_id,
    }

    try:
        resp = retry_request(
            lambda: requests.get(find_url, params=params, headers=headers,
                                 timeout=REQUEST_TIMEOUT)
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return make_result(venue, date, 'error', error=str(e))

    # Parse slots
    results = data.get('results', {})
    venue_results = results.get('venues', [])
    if not venue_results:
        return make_result(venue, date, 'checked', slots=[])

    raw_slots = venue_results[0].get('slots', [])
    slots = []
    for s in raw_slots:
        config = s.get('config', {})
        # time_slot field is empty in /4/find response; actual time is in date.start
        start_str = s.get('date', {}).get('start', '')  # "2026-03-12 14:30:00"
        slot_time = start_str.split(' ')[1][:5] if ' ' in start_str else ''
        slot_type = config.get('type', '')  # "Dining Room"
        token = config.get('token', '')

        slots.append({
            'time': slot_time,
            'type': slot_type,
            'token': token,
        })

    return make_result(venue, date, 'checked', slots=slots)


# ---------------------------------------------------------------------------
# OpenTable checker (via proxy)
# ---------------------------------------------------------------------------

def check_opentable(venue: dict, date: str, party_size: int,
                    session: dict, proxy_base: str) -> dict:
    """Check OpenTable availability via proxy. Queries each time preference."""
    import re
    rid = venue['rid']
    time_prefs = venue.get('timePreferences', ['19:00'])
    bearer = session.get('bearerToken', '')
    session_id = session.get('sessionId', '')

    TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9]):([0-5][0-9])\s?(AM|PM)\b', re.IGNORECASE)
    NO_INVENTORY = ['no availability', 'nextavailable', 'noinventory']

    all_slots = []
    seen_times = set()
    last_error = None

    for time_pref in time_prefs:
        datetime_str = f'{date}T{time_pref}'
        url = f'{proxy_base}/api/v3/restaurant/availability'
        headers = {
            'Authorization': f'Bearer {bearer}',
            'x-ot-sessionid': session_id,
            'Content-Type': 'application/json',
        }
        body = {
            'partySize': party_size,
            'dateTime': datetime_str,
            'rids': [str(rid)],
            'includeNextAvailable': False,
            'forceNextAvailable': 'false',
            'attribution': {'partnerId': '84'},
            'includeOffers': True,
        }

        try:
            resp = retry_request(
                lambda: requests.put(url, json=body, headers=headers, timeout=20)
            )
            if resp.status_code == 401:
                return make_result(venue, date, 'error',
                                   error='OT proxy unauthorized (401): refresh session')
            if resp.status_code >= 400:
                last_error = f'OT proxy status {resp.status_code}'
                continue
            payload = resp.json() if resp.text else {}
        except requests.RequestException as e:
            last_error = str(e)
            continue

        # Parse time slots from response text using regex
        payload_str = json.dumps(payload)
        for m in TIME_RE.finditer(payload_str):
            h, mi, ampm = int(m.group(1)), m.group(2), m.group(3).upper()
            if ampm == 'PM' and h != 12:
                h += 12
            elif ampm == 'AM' and h == 12:
                h = 0
            slot_time = f'{h:02d}:{mi}'
            if slot_time not in seen_times:
                seen_times.add(slot_time)
                all_slots.append({'time': slot_time, 'type': 'Standard'})

        # If no slots and response indicates no inventory, that's a valid check
        if not all_slots:
            low = payload_str.lower()
            if any(m in low for m in NO_INVENTORY):
                return make_result(venue, date, 'checked', slots=[])

    if not all_slots and last_error:
        return make_result(venue, date, 'error', error=last_error)

    return make_result(venue, date, 'checked', slots=sorted(all_slots, key=lambda s: s['time']))


# ---------------------------------------------------------------------------
# SevenRooms checker (public API, no auth)
# ---------------------------------------------------------------------------

def check_sevenrooms(venue: dict, date: str, party_size: int) -> dict:
    """Check SevenRooms availability via public widget API."""
    slug = venue['sevenroomsSlug']
    # Convert YYYY-MM-DD to MM-DD-YYYY for SevenRooms
    parts = date.split('-')
    sr_date = f'{parts[1]}-{parts[2]}-{parts[0]}'

    try:
        resp = retry_request(
            lambda: requests.get(
                SEVENROOMS_URL,
                params={
                    'venue': slug,
                    'time_slot': '19:00',
                    'party_size': party_size,
                    'start_date': sr_date,
                    'num_days': 1,
                    'halo_size_interval': 16,
                    'channel': 'SEVENROOMS_WIDGET',
                },
                timeout=REQUEST_TIMEOUT,
            )
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return make_result(venue, date, 'error', error=str(e))

    # Parse: data.availability[date][shift].times[]
    availability = data.get('data', {}).get('availability', {})
    day_data = availability.get(date, {})

    slots = []
    # day_data is a list of shift objects, each with a 'times' array
    if isinstance(day_data, list):
        shifts = day_data
    elif isinstance(day_data, dict):
        shifts = list(day_data.values()) if day_data else []
    else:
        shifts = []

    for shift in shifts:
        if isinstance(shift, dict):
            times = shift.get('times', [])
        elif isinstance(shift, list):
            times = shift
        else:
            continue

        for t in times:
            if not isinstance(t, dict):
                continue
            slot_type = t.get('type', '')
            # Only bookable slots (not "request")
            if slot_type != 'book':
                continue
            if not t.get('access_persistent_id'):
                continue

            time_iso = t.get('time_iso', '')
            # Extract HH:MM from ISO time (may use 'T' or space separator)
            if 'T' in time_iso:
                slot_time = time_iso.split('T')[1][:5]
            elif ' ' in time_iso:
                slot_time = time_iso.split(' ')[1][:5]
            else:
                slot_time = t.get('time', '')

            slots.append({
                'time': slot_time,
                'type': 'SevenRooms',
                'access_persistent_id': t.get('access_persistent_id', ''),
            })

    return make_result(venue, date, 'checked', slots=slots)


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def make_result(venue: dict, date: str, status: str,
                slots: list = None, error: str = None) -> dict:
    slots = slots or []
    result = {
        'venue': venue['name'],
        'slug': venue['slug'],
        'platform': venue['platform'],
        'date': date,
        'status': status,
        'slotsFound': len(slots),
        'slots': slots,
    }
    if error:
        result['error'] = error
    return result


# ---------------------------------------------------------------------------
# Date computation
# ---------------------------------------------------------------------------

def get_et_now():
    """Get current time in ET."""
    import zoneinfo
    et = zoneinfo.ZoneInfo('America/New_York')
    return datetime.now(et)


def compute_release_date(venue: dict) -> str:
    """Compute the date that would be released today for this venue."""
    et_now = get_et_now()
    target = et_now + timedelta(days=venue['horizonDays'])
    return target.strftime('%Y-%m-%d')


def compute_window_dates(venue: dict) -> list[str]:
    """For window mode: today, tomorrow, and release date (3 dates max)."""
    et_now = get_et_now()
    dates = set()
    dates.add(et_now.strftime('%Y-%m-%d'))
    dates.add((et_now + timedelta(days=1)).strftime('%Y-%m-%d'))
    dates.add((et_now + timedelta(days=venue['horizonDays'])).strftime('%Y-%m-%d'))
    return sorted(dates)


def compute_sweep_dates(venue: dict) -> list[str]:
    """Compute all dates within the venue's horizon for a sweep."""
    et_now = get_et_now()
    dates = []
    for d in range(1, venue['horizonDays'] + 1):
        target = et_now + timedelta(days=d)
        dates.append(target.strftime('%Y-%m-%d'))
    return dates


def is_in_window(cohort: str, windows: dict) -> bool:
    """Check if current ET time is within the cohort's window."""
    if cohort not in windows:
        return False
    w = windows[cohort]
    et_now = get_et_now()
    now_hm = et_now.strftime('%H:%M')
    return w['start'] <= now_hm <= w['end']


# ---------------------------------------------------------------------------
# Platform dispatcher
# ---------------------------------------------------------------------------

def check_venue(venue: dict, date: str, party_size: int,
                resy_proxy_base: str = '', ot_session: dict = None,
                ot_proxy_base: str = '',
                resy_auth_token: str = '') -> dict:
    """Route a venue check to the correct platform checker."""
    platform = venue['platform']
    if platform == 'resy':
        return check_resy(venue, date, party_size, resy_proxy_base,
                          resy_auth_token=resy_auth_token)
    elif platform == 'opentable':
        if not ot_session:
            return make_result(venue, date, 'skipped',
                               error='OT session unavailable')
        return check_opentable(venue, date, party_size, ot_session, ot_proxy_base)
    elif platform == 'sevenrooms':
        return check_sevenrooms(venue, date, party_size)
    else:
        return make_result(venue, date, 'skipped',
                           error=f'Unknown platform: {platform}')


# ---------------------------------------------------------------------------
# Parallel execution helpers
# ---------------------------------------------------------------------------

def _make_platform_semaphores():
    """Create per-platform semaphores to limit concurrent requests."""
    return {
        'resy': threading.Semaphore(1),  # sequential to avoid Resy 429s
        'opentable': threading.Semaphore(PLATFORM_CONCURRENCY),
        'sevenrooms': threading.Semaphore(PLATFORM_CONCURRENCY),
    }


def _check_with_semaphore(semas, venue, date, party_size,
                           resy_proxy_base, ot_session, ot_proxy_base,
                           resy_auth_token):
    """Run a venue check under its platform's semaphore."""
    sema = semas.get(venue['platform'])
    if sema:
        with sema:
            if venue['platform'] == 'resy':
                time_mod.sleep(RESY_REQUEST_DELAY)
            return check_venue(venue, date, party_size, resy_proxy_base,
                               ot_session, ot_proxy_base, resy_auth_token)
    return check_venue(venue, date, party_size, resy_proxy_base,
                       ot_session, ot_proxy_base, resy_auth_token)


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def get_venues_for_cohort(cfg: dict, cohort: str) -> list[dict]:
    """Get enabled venues for a given cohort."""
    return [v for v in cfg['venues']
            if v['cohort'] == cohort and v.get('enabled', True)]


def run_preflight(cfg: dict) -> dict:
    """Run pre-flight checks and return status."""
    ot_status = check_ot_session()
    resy_status = check_resy_auth()

    # Load Resy auth token for probe
    resy_auth = load_resy_auth()
    resy_auth_token = resy_auth['token'] if resy_auth else ''

    # Quick Resy API probe via proxy
    resy_proxy_base = cfg.get('resyProxyBase', '')
    resy_venues = [v for v in cfg['venues']
                   if v['platform'] == 'resy' and v.get('enabled', True)]
    resy_ok = False
    resy_error = None
    if resy_venues:
        test_venue = resy_venues[0]
        find_url = f'{resy_proxy_base}/4/find' if resy_proxy_base else 'https://api.resy.com/4/find'
        headers = dict(RESY_HEADERS)
        if resy_auth_token:
            headers['X-Resy-Auth-Token'] = resy_auth_token
            headers['X-Resy-Universal-Auth'] = resy_auth_token
        try:
            resp = retry_request(
                lambda: requests.get(
                    find_url,
                    params={'lat': NYC_LAT, 'long': NYC_LONG,
                            'day': get_et_now().strftime('%Y-%m-%d'),
                            'party_size': 2, 'venue_id': test_venue['resyVenueId']},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
            )
            resp.raise_for_status()
            resy_ok = True
        except requests.RequestException as e:
            resy_error = str(e)

    # Quick SevenRooms API probe
    sr_venues = [v for v in cfg['venues']
                 if v['platform'] == 'sevenrooms' and v.get('enabled', True)]
    sr_ok = False
    sr_error = None
    if sr_venues:
        test_venue = sr_venues[0]
        today = get_et_now().strftime('%Y-%m-%d')
        parts = today.split('-')
        sr_date = f'{parts[1]}-{parts[2]}-{parts[0]}'
        try:
            resp = retry_request(
                lambda: requests.get(
                    SEVENROOMS_URL,
                    params={
                        'venue': test_venue['sevenroomsSlug'],
                        'time_slot': '19:00',
                        'party_size': 2,
                        'start_date': sr_date,
                        'num_days': 1,
                        'halo_size_interval': 16,
                        'channel': 'SEVENROOMS_WIDGET',
                    },
                    timeout=REQUEST_TIMEOUT,
                )
            )
            resp.raise_for_status()
            sr_ok = True
        except requests.RequestException as e:
            sr_error = str(e)
    else:
        sr_ok = True  # No SR venues, nothing to check

    return {
        'timestamp': get_et_now().isoformat(),
        'mode': 'preflight',
        'session': {
            'ot': ot_status,
            'resy': resy_status,
        },
        'resy': {'ok': resy_ok, 'error': resy_error},
        'sevenrooms': {'ok': sr_ok, 'error': sr_error},
        'enabledVenues': len([v for v in cfg['venues'] if v.get('enabled', True)]),
        'disabledVenues': len([v for v in cfg['venues'] if not v.get('enabled', True)]),
    }


def run_window(cfg: dict, cohort: str) -> dict:
    """Check release date + today + tomorrow for a cohort window.

    Uses date scoping (3 dates per venue) and parallel execution.
    """
    venues = get_venues_for_cohort(cfg, cohort)
    if not venues:
        return make_run_output('window', cohort, [], [],
                               f'No enabled venues for cohort "{cohort}"')

    ot_status = check_ot_session()
    ot_session = load_ot_session() if ot_status['ok'] else None
    resy_auth = load_resy_auth()
    resy_auth_token = resy_auth['token'] if resy_auth else ''
    proxy_base = cfg.get('proxyBase', '')
    resy_proxy_base = cfg.get('resyProxyBase', '')
    global_party = cfg.get('partySize', 2)

    # Build task list: (venue, date, party_size)
    tasks = []
    for v in venues:
        dates = compute_window_dates(v)
        party = v.get('partySize', global_party)
        for date in dates:
            tasks.append((v, date, party))

    # Execute in parallel with per-platform semaphores
    semas = _make_platform_semaphores()
    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=THREAD_POOL_WORKERS) as executor:
        futures = {
            executor.submit(
                _check_with_semaphore, semas, v, date, party,
                resy_proxy_base, ot_session, proxy_base, resy_auth_token
            ): (v, date)
            for v, date, party in tasks
        }
        for future in as_completed(futures):
            v, date = futures[future]
            try:
                r = future.result()
            except Exception as e:
                r = make_result(v, date, 'error', error=str(e))
            results.append(r)
            if r.get('error'):
                errors.append({'venue': v['name'], 'date': date, 'error': r['error']})

    return make_run_output('window', cohort, results, errors,
                           session_ot=ot_status)


def run_sweep(cfg: dict) -> dict:
    """Check all dates within horizon for all enabled venues (parallel)."""
    venues = [v for v in cfg['venues'] if v.get('enabled', True)]

    ot_status = check_ot_session()
    ot_session = load_ot_session() if ot_status['ok'] else None
    resy_auth = load_resy_auth()
    resy_auth_token = resy_auth['token'] if resy_auth else ''
    proxy_base = cfg.get('proxyBase', '')
    resy_proxy_base = cfg.get('resyProxyBase', '')
    global_party = cfg.get('partySize', 2)

    # Build task list
    tasks = []
    for v in venues:
        dates = compute_sweep_dates(v)
        party = v.get('partySize', global_party)
        for date in dates:
            tasks.append((v, date, party))

    # Execute in parallel with per-platform semaphores
    semas = _make_platform_semaphores()
    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=THREAD_POOL_WORKERS) as executor:
        futures = {
            executor.submit(
                _check_with_semaphore, semas, v, date, party,
                resy_proxy_base, ot_session, proxy_base, resy_auth_token
            ): (v, date)
            for v, date, party in tasks
        }
        for future in as_completed(futures):
            v, date = futures[future]
            try:
                r = future.result()
            except Exception as e:
                r = make_result(v, date, 'error', error=str(e))
            results.append(r)
            if r.get('error'):
                errors.append({'venue': v['name'], 'date': date, 'error': r['error']})

    return make_run_output('sweep', None, results, errors,
                           session_ot=ot_status)


def make_run_output(mode: str, cohort: Optional[str], results: list,
                    errors: list, note: str = None,
                    session_ot: dict = None) -> dict:
    checked = sum(1 for r in results if r['status'] == 'checked')
    with_avail = sum(1 for r in results if r['slotsFound'] > 0)
    total = len(results)

    out = {
        'timestamp': get_et_now().isoformat(),
        'mode': mode,
    }
    if cohort:
        out['cohort'] = cohort
    if session_ot:
        out['session'] = {'ot': session_ot}
    out['results'] = results
    out['errors'] = errors
    out['summary'] = f'{checked}/{total} checked, {with_avail} with availability'
    if note:
        out['note'] = note
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Reservation Autopilot')
    parser.add_argument('--mode', required=True,
                        choices=['preflight', 'window', 'sweep', 'all'],
                        help='Run mode')
    parser.add_argument('--cohort', help='Cohort to check (for window mode)')
    args = parser.parse_args()

    # Change to workspace root
    os.chdir(WORKSPACE)

    cfg = load_config()

    if args.mode == 'preflight':
        output = run_preflight(cfg)

    elif args.mode == 'window':
        if not args.cohort:
            parser.error('--cohort is required for window mode')
        output = run_window(cfg, args.cohort)

    elif args.mode == 'sweep':
        output = run_sweep(cfg)

    elif args.mode == 'all':
        # Run all cohorts in window mode
        all_results = []
        all_errors = []
        ot_status = check_ot_session()
        cohorts = list(cfg.get('windows', {}).keys())
        for cohort in cohorts:
            result = run_window(cfg, cohort)
            all_results.extend(result.get('results', []))
            all_errors.extend(result.get('errors', []))
        output = make_run_output('all', None, all_results, all_errors,
                                 session_ot=ot_status)

    # Save artifact
    artifact_dir = WORKSPACE / 'artifacts' / 'reservation-runs'
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = get_et_now().strftime('%Y%m%d-%H%M%S')
    artifact_path = artifact_dir / f'run-{stamp}.json'
    artifact_path.write_text(json.dumps(output, indent=2))

    # Print to stdout (for shell wrapper / cron capture)
    print(json.dumps(output, indent=2))

    # Exit non-zero if any errors
    if output.get('errors'):
        sys.exit(1)


if __name__ == '__main__':
    main()
