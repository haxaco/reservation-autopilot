#!/usr/bin/env python3
"""Discover Resy venue IDs and OpenTable RIDs by restaurant name.

Usage:
    python3 tools/discover_venue.py "Restaurant Name"
    python3 tools/discover_venue.py "Restaurant Name" --platform resy
    python3 tools/discover_venue.py "Restaurant Name" --platform opentable
    python3 tools/discover_venue.py "Restaurant Name" --city "New York"
"""

import argparse
import os
import json
import sys
from pathlib import Path

import requests

# Resy's platform-public client API key (the static value Resy embeds
# in their own browser code at resy.com). NOT user-specific auth —
# per-user JWT goes in config.json per venue. Safe to commit.
RESY_API_KEY = 'VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5'

WORKSPACE = Path(__file__).resolve().parent.parent
CONFIG_PATH = WORKSPACE / 'config' / 'reservation-autopilot.json'
# Optional OpenTable proxy URL. Set via env var or config.json; the
# autopilot itself reads `proxyBase` from config.json. This script
# only needs OT lookups during venue discovery — set the env var if
# you want to use a proxy, otherwise OT lookups will fail.
OT_PROXY_BASE = os.environ.get('OT_PROXY_BASE', '')


def get_resy_proxy_base() -> str:
    """Read resyProxyBase from config, fallback to direct API."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        return cfg.get('resyProxyBase', '')
    except (FileNotFoundError, json.JSONDecodeError):
        return ''


def search_resy(query: str) -> list[dict]:
    """Search Resy for venues matching query."""
    resy_proxy_base = get_resy_proxy_base()
    if resy_proxy_base:
        search_url = f'{resy_proxy_base}/3/venuesearch/search'
    else:
        search_url = 'https://api.resy.com/3/venuesearch/search'

    try:
        resp = requests.post(
            search_url,
            json={
                'geo': {'latitude': 40.7128, 'longitude': -74.0060},
                'query': query,
                'types': ['venue'],
                'per_page': 5,
            },
            headers={
                'Authorization': f'ResyAPI api_key="{RESY_API_KEY}"',
                'Content-Type': 'application/json',
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f'[Resy] Search error: {e}', file=sys.stderr)
        return []

    results = []
    hits = data.get('search', {}).get('hits', [])
    for hit in hits:
        venue = hit.get('_source', hit)
        results.append({
            'name': venue.get('name', ''),
            'resyVenueId': venue.get('id', venue.get('venue_id')),
            'location': venue.get('location', {}).get('name', ''),
            'neighborhood': venue.get('neighborhood', ''),
        })
    return results


def search_opentable(query: str) -> list[dict]:
    """Search OpenTable for restaurants matching query."""
    try:
        resp = requests.get(
            f'{OT_PROXY_BASE}/api/restaurant/search',
            params={'term': query, 'latitude': 40.7128, 'longitude': -74.0060},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f'[OpenTable] Search error: {e}', file=sys.stderr)
        return []

    results = []
    restaurants = data.get('restaurants', data.get('items', []))
    for r in restaurants:
        results.append({
            'name': r.get('name', ''),
            'rid': r.get('rid', r.get('restaurantId', r.get('id'))),
            'neighborhood': r.get('neighborhood', r.get('location', '')),
        })
    return results


def make_config_snippet(name: str, platform: str, venue_id, slug: str = None) -> dict:
    """Generate a ready-to-paste config JSON object."""
    if not slug:
        import re
        slug = name.lower().replace(' ', '-').replace("'", '')
        slug = re.sub(r'[^a-z0-9-]', '', slug)

    snippet = {
        'slug': slug,
        'name': name,
        'platform': platform,
        'cohort': '10am',
        'releaseHourET': '10:00',
        'horizonDays': 30,
        'timePreferences': ['19:00', '19:30', '20:00', '20:30'],
        'partySize': 2,
        'enabled': True,
    }

    if platform == 'resy':
        snippet['resyVenueId'] = venue_id
    elif platform == 'opentable':
        snippet['rid'] = venue_id

    return snippet


def main():
    parser = argparse.ArgumentParser(description='Discover venue IDs for Resy & OpenTable')
    parser.add_argument('name', help='Restaurant name to search for')
    parser.add_argument('--platform', choices=['resy', 'opentable'],
                        help='Search only this platform (default: both)')
    parser.add_argument('--city', default='New York',
                        help='City for search context (default: New York)')
    args = parser.parse_args()

    query = args.name

    if args.platform in (None, 'resy'):
        print(f'\n=== Resy Results for "{query}" ===')
        resy_results = search_resy(query)
        if resy_results:
            for i, r in enumerate(resy_results, 1):
                print(f'\n  {i}. {r["name"]}')
                print(f'     Venue ID: {r["resyVenueId"]}')
                if r.get('location'):
                    print(f'     Location: {r["location"]}')
                if r.get('neighborhood'):
                    print(f'     Neighborhood: {r["neighborhood"]}')
            # Print config snippet for top result
            top = resy_results[0]
            snippet = make_config_snippet(top['name'], 'resy', top['resyVenueId'])
            print(f'\n  Config snippet (top result):')
            print(f'  {json.dumps(snippet, indent=2)}')
        else:
            print('  No results found.')

    if args.platform in (None, 'opentable'):
        print(f'\n=== OpenTable Results for "{query}" ===')
        ot_results = search_opentable(query)
        if ot_results:
            for i, r in enumerate(ot_results, 1):
                print(f'\n  {i}. {r["name"]}')
                print(f'     RID: {r["rid"]}')
                if r.get('neighborhood'):
                    print(f'     Neighborhood: {r["neighborhood"]}')
            top = ot_results[0]
            snippet = make_config_snippet(top['name'], 'opentable', top['rid'])
            print(f'\n  Config snippet (top result):')
            print(f'  {json.dumps(snippet, indent=2)}')
        else:
            print('  No results found.')

    print()


if __name__ == '__main__':
    main()
