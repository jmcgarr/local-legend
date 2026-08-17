#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.31",
#     "keyring>=24.0",
#     "rich>=13.0",
# ]
# ///
"""
local-legends: find Strava segments in a zip code where you have the best
shot at becoming the Local Legend.

Discovery is hybrid:
  1. Explore Segments API — sweeps the zip code area for all segments.
     (Restricted to Extended Access tier apps starting Sept 1, 2026; the
     tool degrades gracefully to history-only after that.)
  2. Your own recent rides in the area — segments you've already ridden.

Results are sorted by fewest rides needed to claim Local Legend, accounting
for efforts you've already logged in the current 90-day window. Unclaimed
segments are listed in their own table.

API responses are cached under ~/.cache/strava-legends/ (activities forever —
they're immutable; segment details and your 90-day effort counts for 24h;
explore sweeps for 7 days), so repeat runs cost only a handful of API calls.
"""

import argparse
import csv
import http.server
import json
import math
import pathlib
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone

import keyring
import requests
from rich.console import Console
from rich.table import Table

SERVICE = "strava-local-legends"
API_BASE = "https://www.strava.com/api/v3"
OAUTH_BASE = "https://www.strava.com/oauth"
REDIRECT_PORT = 8377
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = "read,activity:read_all"
EXPLORE_MAX_DEPTH = 2  # 1 + 4 + 16 = up to 21 explore calls per zip, worst case

CACHE_DIR = pathlib.Path.home() / ".cache" / "strava-legends"
SEGMENT_TTL = 24 * 3600        # LL effort counts drift slowly
EFFORTS_TTL = 24 * 3600        # your own 90-day effort counts
EXPLORE_TTL = 7 * 24 * 3600    # segments don't move
RIDE_TYPES = ("Ride", "GravelRide", "MountainBikeRide")

console = Console()
STATS = {"api": 0, "cache": 0}
LAST_USAGE = {}  # filled from Strava rate-limit response headers


class RateLimited(Exception):
    """Strava returned 429 — salvage what we have instead of dying."""


# --------------------------------------------------------------------------
# Disk cache
# --------------------------------------------------------------------------

def cache_load(name, ttl=None):
    """Load a cache file; entries past their TTL are pruned so segment data
    is flushed daily (explore weekly) rather than accumulating forever."""
    try:
        data = json.loads((CACHE_DIR / f"{name}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if ttl:
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and fresh(v, ttl)}
    return data


def flush_cache():
    files = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
    for f in files:
        f.unlink()
    return len(files)


def cache_save(name, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{name}.json").write_text(json.dumps(data))


def fresh(entry, ttl):
    return entry and (time.time() - entry.get("ts", 0)) < ttl


# --------------------------------------------------------------------------
# Credentials & OAuth (one-time browser auth, tokens live in the OS keychain)
# --------------------------------------------------------------------------

def keyring_get(key):
    try:
        return keyring.get_password(SERVICE, key)
    except keyring.errors.KeyringError as e:
        console.print(
            f"[red]Can't access your OS keychain ({e}).[/red]\n"
            "If a keychain permission dialog appeared, click "
            "[bold]Always Allow[/bold] and re-run. (A freshly downloaded or "
            "rebuilt binary counts as a new app to macOS, so it must be "
            "granted access once.)")
        sys.exit(1)


def get_client_credentials(reset=False):
    if reset:
        for key in ("client_id", "client_secret", "tokens"):
            try:
                keyring.delete_password(SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass

    client_id = keyring_get("client_id")
    client_secret = keyring_get("client_secret")

    if not client_id or not client_secret:
        console.print("\n[bold]One-time setup: connect your Strava API application[/bold]")
        console.print(
            "  1. Go to [cyan]https://www.strava.com/settings/api[/cyan] and create an app\n"
            "     (any name/website; set [bold]Authorization Callback Domain[/bold] to [bold]localhost[/bold]).\n"
            "     Note: as of June 2026 Strava requires a Strava subscription for API access.\n"
            "  2. Copy the Client ID and Client Secret shown on that page.\n")
        client_id = input("Client ID: ").strip()
        client_secret = input("Client Secret: ").strip()
        if not client_id or not client_secret:
            console.print("[red]Client ID and secret are required.[/red]")
            sys.exit(1)
        keyring.set_password(SERVICE, "client_id", client_id)
        keyring.set_password(SERVICE, "client_secret", client_secret)
        console.print("[green]Saved to your OS keychain.[/green]")

    return client_id, client_secret


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        # Browsers also request /favicon.ico etc. — only /callback matters,
        # and a captured code must never be overwritten by a later request.
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("code"):
            _CallbackHandler.code = params["code"][0]
        elif params.get("error"):
            _CallbackHandler.error = params["error"][0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorized! You can close this tab and return to the terminal." \
            if _CallbackHandler.code \
            else "Authorization failed or was denied. You can close this tab."
        self.wfile.write(f"<h2>{msg}</h2>".encode())

    def log_message(self, *args):
        pass


def browser_authorize(client_id, client_secret):
    auth_url = (
        f"{OAUTH_BASE}/authorize?client_id={client_id}&response_type=code"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&approval_prompt=auto&scope={SCOPES}"
    )
    _CallbackHandler.code = None
    _CallbackHandler.error = None
    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    console.print("\nOpening your browser to authorize with Strava (one time only)...")
    console.print(f"If it doesn't open, visit:\n  [cyan]{auth_url}[/cyan]")
    webbrowser.open(auth_url)

    deadline = time.time() + 300
    while (_CallbackHandler.code is None and _CallbackHandler.error is None
           and time.time() < deadline):
        time.sleep(0.5)
    server.shutdown()

    if _CallbackHandler.error is not None:
        console.print(f"[red]Strava authorization was denied "
                      f"({_CallbackHandler.error}).[/red]")
        sys.exit(1)
    if _CallbackHandler.code is None:
        console.print("[red]Timed out waiting for authorization (5 min).[/red]")
        sys.exit(1)

    resp = requests.post(f"{OAUTH_BASE}/token", data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _CallbackHandler.code,
        "grant_type": "authorization_code",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_access_token(reset=False):
    client_id, client_secret = get_client_credentials(reset=reset)

    raw = keyring_get("tokens")
    tokens = json.loads(raw) if raw else None

    if tokens and tokens.get("expires_at", 0) > time.time() + 60:
        return tokens["access_token"]

    if tokens and tokens.get("refresh_token"):
        resp = requests.post(f"{OAUTH_BASE}/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        }, timeout=30)
        if resp.ok:
            tokens = resp.json()
        else:
            console.print("[yellow]Token refresh failed; re-authorizing.[/yellow]")
            tokens = browser_authorize(client_id, client_secret)
    else:
        tokens = browser_authorize(client_id, client_secret)

    keyring.set_password(SERVICE, "tokens", json.dumps({
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": tokens["expires_at"],
    }))
    return tokens["access_token"]


# --------------------------------------------------------------------------
# Strava API helpers
# --------------------------------------------------------------------------

def api_get(token, path, params=None, quiet=False):
    STATS["api"] += 1
    resp = requests.get(f"{API_BASE}{path}", params=params,
                        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    usage = resp.headers.get("X-ReadRateLimit-Usage") or \
        resp.headers.get("X-RateLimit-Usage")
    limit = resp.headers.get("X-ReadRateLimit-Limit") or \
        resp.headers.get("X-RateLimit-Limit")
    if usage and limit:
        LAST_USAGE.update(zip(("used_15m", "used_day"), usage.split(",")))
        LAST_USAGE.update(zip(("limit_15m", "limit_day"), limit.split(",")))
    if resp.status_code == 429:
        raise RateLimited()
    if resp.status_code in (401, 402, 403):
        if not quiet:
            console.print(f"[yellow]Strava returned {resp.status_code} for {path} — "
                          "your app tier may not have access to this endpoint.[/yellow]")
        return None
    resp.raise_for_status()
    return resp.json()


def get_athlete_id(token):
    cached = cache_load("athlete")
    if cached.get("id"):
        STATS["cache"] += 1
        return cached["id"]
    me = api_get(token, "/athlete") or {}
    if me.get("id"):
        cache_save("athlete", {"id": me["id"]})
    return me.get("id")


def print_budget():
    parts = [f"{STATS['api']} API calls this run, {STATS['cache']} cache hits"]
    if LAST_USAGE:
        parts.append(f"Strava budget used: {LAST_USAGE['used_15m']}/"
                     f"{LAST_USAGE['limit_15m']} this 15-min window, "
                     f"{LAST_USAGE['used_day']}/{LAST_USAGE['limit_day']} today")
    console.print(f"[dim]{' · '.join(parts)}[/dim]")


# --------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------

def zip_to_area(zip_code, radius_km, country="us"):
    """Zip code -> centroid + bounding box, geocode cached forever."""
    geo = cache_load("geo")
    key = f"{country}:{zip_code}"
    if key in geo:
        STATS["cache"] += 1
        place = geo[key]
    else:
        resp = requests.get(f"https://api.zippopotam.us/{country}/{zip_code}",
                            timeout=15)
        if resp.status_code == 404:
            console.print(f"[red]Zip code {zip_code} not found.[/red]")
            return None
        resp.raise_for_status()
        p = resp.json()["places"][0]
        place = {
            "lat": float(p["latitude"]), "lng": float(p["longitude"]),
            "label": f'{p["place name"]}, {p.get("state abbreviation", "")}'.strip(", "),
        }
        geo[key] = place
        cache_save("geo", geo)

    lat, lng = place["lat"], place["lng"]
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.1))
    return {"zip": zip_code, "label": place["label"], "lat": lat, "lng": lng,
            "box": (lat - dlat, lng - dlng, lat + dlat, lng + dlng)}


def in_any_box(latlng, boxes, pad=0.0):
    if not latlng or len(latlng) != 2:
        return False
    lat, lng = latlng
    return any(b[0] - pad <= lat <= b[2] + pad and b[1] - pad <= lng <= b[3] + pad
               for b in boxes)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def explore_segments(token, box, cache, depth=0):
    """Explore returns at most 10 segments per box; subdivide when saturated.
    Each box's result is cached for EXPLORE_TTL."""
    key = ",".join(f"{c:.4f}" for c in box)
    entry = cache.get(key)
    if fresh(entry, EXPLORE_TTL):
        STATS["cache"] += 1
        segs = list(entry["segs"])
    else:
        bounds = ",".join(str(c) for c in box)
        data = api_get(token, "/segments/explore",
                       {"bounds": bounds, "activity_type": "riding"})
        if data is None:  # endpoint restricted (post Sept 1, 2026) or forbidden
            return None
        segs = [{"id": s["id"], "name": s["name"],
                 "distance": s.get("distance", 0.0),
                 "avg_grade": s.get("avg_grade", 0.0)}
                for s in data.get("segments", [])]
        cache[key] = {"ts": time.time(), "segs": segs}

    if len(segs) >= 10 and depth < EXPLORE_MAX_DEPTH:
        mid_lat = (box[0] + box[2]) / 2
        mid_lng = (box[1] + box[3]) / 2
        quadrants = [
            (box[0], box[1], mid_lat, mid_lng),
            (box[0], mid_lng, mid_lat, box[3]),
            (mid_lat, box[1], box[2], mid_lng),
            (mid_lat, mid_lng, box[2], box[3]),
        ]
        for quad in quadrants:
            sub = explore_segments(token, quad, cache, depth + 1)
            if sub:
                segs.extend(sub)
    return segs


def epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def update_ride_summaries(token, acts):
    """Keep a permanent local list of your ride summaries; after the first
    run only rides newer than the latest cached one are fetched."""
    latest = acts.get("latest", 0)
    rides = {r["id"]: r for r in acts.get("rides", [])}
    page, fetched = 1, 0
    while True:
        params = {"per_page": 100, "page": page}
        if latest:
            params["after"] = int(latest)
        batch = api_get(token, "/athlete/activities", params) or []
        for a in batch:
            if a.get("type") in RIDE_TYPES and a.get("start_latlng"):
                rides[a["id"]] = {"id": a["id"],
                                  "start_latlng": a["start_latlng"],
                                  "date": epoch(a["start_date"])}
        fetched += len(batch)
        # First-ever run is capped at 200 recent activities; incremental
        # updates page through everything new since last run.
        if len(batch) < 100 or (not latest and fetched >= 200) or page >= 5:
            break
        page += 1
    if rides:
        acts["latest"] = max(r["date"] for r in rides.values())
    acts["rides"] = sorted(rides.values(), key=lambda r: r["date"], reverse=True)


def mine_activity(token, acts, act_id):
    """Segment efforts of one activity, trimmed; cached forever (immutable)."""
    details = acts.setdefault("details", {})
    key = str(act_id)
    if key in details:
        STATS["cache"] += 1
        return details[key]
    detail = api_get(token, f"/activities/{act_id}",
                     {"include_all_efforts": "true"})
    if detail is None:
        return []
    efforts = []
    for e in detail.get("segment_efforts", []):
        seg = e.get("segment") or {}
        if seg.get("id"):
            efforts.append({"id": seg["id"], "name": seg.get("name", "?"),
                            "distance": seg.get("distance", 0.0),
                            "avg_grade": seg.get("average_grade", 0.0),
                            "start_latlng": seg.get("start_latlng"),
                            "date": epoch(e["start_date"]) if e.get("start_date") else 0})
    details[key] = efforts
    return efforts


def history_90day_counts(acts):
    """Fallback per-segment 90-day effort counts from mined activities."""
    cutoff = time.time() - 90 * 86400
    counts = {}
    for efforts in acts.get("details", {}).values():
        for e in efforts:
            if e["date"] >= cutoff:
                counts[e["id"]] = counts.get(e["id"], 0) + 1
    return counts


# --------------------------------------------------------------------------
# Segment details & your 90-day efforts
# --------------------------------------------------------------------------

def get_segment_detail(token, seg_id, cache):
    entry = cache.get(str(seg_id))
    if fresh(entry, SEGMENT_TTL):
        STATS["cache"] += 1
        return entry
    detail = api_get(token, f"/segments/{seg_id}")
    if detail is None:
        return None
    ll = detail.get("local_legend") or {}
    stats = detail.get("athlete_segment_stats") or {}
    entry = {
        "ts": time.time(),
        "name": detail.get("name", "?"),
        "distance": detail.get("distance", 0.0),
        "avg_grade": detail.get("average_grade", 0.0),
        "ll_efforts": int(ll.get("effort_count") or 0),
        "ll_athlete_id": ll.get("athlete_id"),
        "your_lifetime": int(stats.get("effort_count") or 0),
    }
    cache[str(seg_id)] = entry
    return entry


EFFORTS_ENDPOINT_OK = True  # flips off after the first 401/402/403


def get_90day_count(token, seg_id, lifetime, cache, fallback_counts):
    """Your efforts on this segment in the last 90 days."""
    global EFFORTS_ENDPOINT_OK
    if lifetime == 0:
        return 0
    entry = cache.get(str(seg_id))
    if fresh(entry, EFFORTS_TTL):
        STATS["cache"] += 1
        return entry["count"]
    if EFFORTS_ENDPOINT_OK:
        # Strava requires both dates, in Z-suffixed ISO8601.
        now_utc = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        efforts = api_get(token, "/segment_efforts", {
            "segment_id": seg_id,
            "start_date_local": (now_utc - timedelta(days=90)).strftime(fmt),
            "end_date_local": now_utc.strftime(fmt),
            "per_page": 100}, quiet=True)
        if efforts is not None:
            count = len(efforts)
            cache[str(seg_id)] = {"ts": time.time(), "count": count}
            return count
        EFFORTS_ENDPOINT_OK = False
        console.print("[yellow]Can't list your segment efforts (endpoint needs "
                      "a Strava subscription); estimating your 90-day counts "
                      "from mined ride history instead.[/yellow]")
    return fallback_counts.get(seg_id, 0)


# --------------------------------------------------------------------------
# Scoring & output
# --------------------------------------------------------------------------

def norm(value, values):
    lo, hi = min(values), max(values)
    return 0.0 if hi == lo else (value - lo) / (hi - lo)


def score_segments(rows):
    if not rows:
        return
    dists = [r["distance"] for r in rows]
    grades = [abs(r["avg_grade"]) for r in rows]
    targets = [r["ll_efforts"] for r in rows]
    for r in rows:
        s = (3.0 * (1.0 if r["your_lifetime"] > 0 else 0.0)
             + 2.5 * (1.0 - norm(r["ll_efforts"], targets))
             + 1.5 * (1.0 - norm(r["distance"], dists))
             + 1.5 * (1.0 - norm(abs(r["avg_grade"]), grades)))
        r["score"] = round(s / 8.5 * 100)


def you_cell(r):
    if r["your_lifetime"] == 0:
        return "—"
    return f"{r['your_90d']}/{r['your_lifetime']}"


def print_claimed_table(rows, area_labels):
    table = Table(title=f"Easiest Local Legend targets — {', '.join(area_labels)}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Segment", overflow="fold")
    table.add_column("Dist", justify="right")
    table.add_column("Grade", justify="right")
    table.add_column("You 90d/all", justify="right")
    table.add_column("Rides needed*", justify="right")
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Link", style="cyan", overflow="fold")
    for i, r in enumerate(rows, 1):
        url = f"https://www.strava.com/segments/{r['id']}"
        table.add_row(str(i), f"[link={url}]{r['name']}[/link]", dist_cell(r),
                      f"{r['avg_grade']:.1f}%", you_cell(r),
                      str(r["rides_needed"]), str(r["score"]), url)
    console.print(table)
    console.print("[dim]* Rides still needed within the rolling 90-day window "
                  "to claim Local Legend: current Legend's count + 1, minus "
                  "your efforts in the last 90 days.[/dim]")


def print_unclaimed_table(rows, area_labels):
    table = Table(title=f"Unclaimed segments (1 ride = Local Legend) — "
                        f"{', '.join(area_labels)}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Segment", overflow="fold")
    table.add_column("Dist", justify="right")
    table.add_column("Grade", justify="right")
    table.add_column("You 90d/all", justify="right")
    table.add_column("Link", style="cyan", overflow="fold")
    for i, r in enumerate(rows, 1):
        url = f"https://www.strava.com/segments/{r['id']}"
        table.add_row(str(i), f"[link={url}]{r['name']}[/link]", dist_cell(r),
                      f"{r['avg_grade']:.1f}%", you_cell(r), url)
    console.print(table)


METRIC = False


def dist_cell(r):
    if METRIC:
        return f"{r['distance'] / 1000:.1f} km" if r["distance"] >= 1000 \
            else f"{r['distance']:.0f} m"
    miles = r["distance"] / 1609.344
    return f"{miles:.1f} mi" if miles >= 10 else f"{miles:.2f} mi"


def export_csv(path, claimed, unclaimed, yours):
    """All scanned segments (not just the displayed rows), tagged by status."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "segment_id", "name", "distance_m",
                    "avg_grade_pct", "your_efforts_90d", "your_efforts_all",
                    "legend_efforts_90d", "rides_needed", "score", "url"])
        for status, rows in (("claimed", claimed), ("unclaimed", unclaimed),
                             ("yours", yours)):
            for r in rows:
                w.writerow([status, r["id"], r["name"],
                            round(r["distance"], 1), r["avg_grade"],
                            r.get("your_90d", ""), r["your_lifetime"],
                            r["ll_efforts"], r.get("rides_needed", ""),
                            r.get("score", ""),
                            f"https://www.strava.com/segments/{r['id']}"])


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="local-legends",
        description="Find Strava segments in a zip code that are easiest to "
                    "become the Local Legend on (cycling).")
    parser.add_argument("zips", nargs="*", help="One or more zip codes")
    parser.add_argument("--radius-km", type=float, default=5.0,
                        help="Search radius around each zip centroid (default 5)")
    parser.add_argument("--limit", type=int, default=15,
                        help="Rows in the targets table (default 15)")
    parser.add_argument("--scan", type=int, default=0, metavar="N",
                        help="Cap detail lookups to the N shortest/flattest "
                             "candidates (default 0 = scan every segment "
                             "found in the area)")
    parser.add_argument("--max-rides", type=int, default=10,
                        help="Max past rides in the area to mine for segments (default 10)")
    parser.add_argument("--ridden-only", action="store_true",
                        help="Ignore segments you've never ridden (as found in "
                             "your mined ride history; raise --max-rides for "
                             "deeper coverage). Skips the Explore sweep.")
    parser.add_argument("--no-explore", action="store_true",
                        help="Skip the Explore API (only segments you've ridden)")
    parser.add_argument("--no-history", action="store_true",
                        help="Skip mining your ride history")
    parser.add_argument("--country", default="us",
                        help="Country code for zip lookup (default us)")
    parser.add_argument("--metric", action="store_true",
                        help="Show distances in meters/kilometers instead of miles")
    parser.add_argument("--csv", metavar="PATH",
                        help="Also export all scanned segments to a CSV file")
    parser.add_argument("--flush-cache", action="store_true",
                        help="Delete all cached API responses, then run "
                             "(or just exit if no zip codes given)")
    parser.add_argument("--reset-auth", action="store_true",
                        help="Forget stored credentials and re-authenticate")
    args = parser.parse_args()

    if args.flush_cache:
        n = flush_cache()
        console.print(f"Cache flushed ({n} file(s) removed from {CACHE_DIR}).")
        if not args.zips:
            return
    if not args.zips:
        parser.error("at least one zip code is required (or use --flush-cache)")
    if args.ridden_only and args.no_history:
        parser.error("--ridden-only needs your ride history; drop --no-history")
    if args.ridden_only:
        args.no_explore = True  # explore candidates would all be filtered out
    global METRIC
    METRIC = args.metric

    token = get_access_token(reset=args.reset_auth)

    areas = [a for a in (zip_to_area(z, args.radius_km, args.country)
                         for z in args.zips) if a]
    if not areas:
        sys.exit(1)
    boxes = [a["box"] for a in areas]
    for a in areas:
        console.print(f"Searching around [bold]{a['label']} ({a['zip']})[/bold] "
                      f"±{args.radius_km:g} km")

    explore_cache = cache_load("explore", EXPLORE_TTL)
    seg_cache = cache_load("segments", SEGMENT_TTL)
    eff_cache = cache_load("efforts90", EFFORTS_TTL)
    acts = cache_load("activities")
    limited = False
    candidates = {}  # segment id -> summary dict

    try:
        my_id = get_athlete_id(token)

        if not args.no_explore:
            with console.status("Exploring segments in the area..."):
                for a in areas:
                    segs = explore_segments(token, a["box"], explore_cache)
                    if segs is None:
                        console.print(
                            "[yellow]Explore Segments is unavailable to this app "
                            "(restricted to Extended Access tier since Sept 1, 2026). "
                            "Falling back to your ride history only.[/yellow]")
                        break
                    for s in segs:
                        candidates.setdefault(s["id"], dict(s, ridden_hint=False))
            console.print(f"Explore found [bold]{len(candidates)}[/bold] segments.")

        if not args.no_history:
            with console.status("Mining your recent rides in the area..."):
                update_ride_summaries(token, acts)
                area_rides = [r for r in acts.get("rides", [])
                              if in_any_box(r["start_latlng"], boxes, pad=0.05)]
                n_ridden = 0
                for ride in area_rides[:args.max_rides]:
                    for seg in mine_activity(token, acts, ride["id"]):
                        if in_any_box(seg.get("start_latlng"), boxes, pad=0.01):
                            entry = candidates.setdefault(
                                seg["id"], {k: seg[k] for k in
                                            ("id", "name", "distance", "avg_grade")})
                            if not entry.get("ridden_hint"):
                                entry["ridden_hint"] = True
                                n_ridden += 1
            n_mined = min(len(area_rides), args.max_rides)
            console.print(
                f"Found [bold]{n_ridden}[/bold] ridden segments across "
                f"[bold]{n_mined}[/bold] of your [bold]{len(area_rides)}[/bold] "
                "recent rides in the area"
                + (" (raise --max-rides to mine more)."
                   if len(area_rides) > n_mined else "."))

        if args.ridden_only:
            candidates = {k: v for k, v in candidates.items()
                          if v.get("ridden_hint")}

        if not candidates:
            console.print("[red]No segments found. Try a bigger --radius-km"
                          + (" or --max-rides" if args.ridden_only else "")
                          + ".[/red]")
            print_budget()
            sys.exit(1)

        # Every candidate gets a detail lookup by default, easiest-looking
        # first (short/flat), so a rate-limited partial run still covers the
        # most promising segments. Ridden-ness only matters for tie-breaking.
        pre = sorted(candidates.values(),
                     key=lambda s: (abs(s["avg_grade"]), s["distance"]))
        shortlist = pre[:args.scan] if args.scan else pre
        stale = sum(1 for s in shortlist
                    if not fresh(seg_cache.get(str(s["id"])), SEGMENT_TTL))
        if stale:
            console.print(f"[dim]{len(shortlist)} segments to check: "
                          f"{len(shortlist) - stale} cached, ~{stale} API "
                          "calls needed"
                          + (" — may span several 15-min rate windows; "
                             "re-run to resume if limited."
                             if stale > 150 else ".") + "[/dim]")

        fallback_counts = history_90day_counts(acts)
        rows, yours = [], []
        skipped = 0
        try:
            with console.status(f"Fetching details for {len(shortlist)} segments..."):
                for s in shortlist:
                    d = get_segment_detail(token, s["id"], seg_cache)
                    if d is None:
                        skipped += 1
                        continue
                    row = {
                        "id": s["id"], "name": d["name"],
                        "distance": d["distance"], "avg_grade": d["avg_grade"],
                        "ll_efforts": d["ll_efforts"],
                        "your_lifetime": d["your_lifetime"]
                                         or (1 if s.get("ridden_hint") else 0),
                    }
                    if d["ll_athlete_id"] and d["ll_athlete_id"] == my_id:
                        yours.append(row)
                        continue
                    row["your_90d"] = get_90day_count(
                        token, s["id"], row["your_lifetime"],
                        eff_cache, fallback_counts)
                    row["rides_needed"] = max(
                        1, d["ll_efforts"] + 1 - row["your_90d"])
                    rows.append(row)
        except RateLimited:
            limited = True
            skipped = len(shortlist) - len(rows) - len(yours)

    except RateLimited:
        limited = True
        rows, yours, skipped = [], [], 0
    finally:
        cache_save("explore", explore_cache)
        cache_save("segments", seg_cache)
        cache_save("efforts90", eff_cache)
        cache_save("activities", acts)

    if limited:
        console.print("[yellow]Hit Strava's rate limit — showing what was "
                      "gathered. Everything fetched so far is cached; re-run "
                      "in ~15 minutes to pick up where this left off.[/yellow]")

    score_segments(rows)
    area_labels = [f"{a['label']} {a['zip']}" for a in areas]

    claimed = [r for r in rows if r["ll_efforts"] > 0]
    # Fewest rides to claim Local Legend first; score breaks ties.
    claimed.sort(key=lambda r: (r["rides_needed"], -r["score"]))
    unclaimed = [r for r in rows if r["ll_efforts"] == 0]
    unclaimed.sort(key=lambda r: -r["score"])

    if claimed:
        print_claimed_table(claimed[:args.limit], area_labels)
    if unclaimed:
        print_unclaimed_table(unclaimed[:args.limit], area_labels)
        if len(unclaimed) > args.limit:
            console.print(f"[dim]...plus {len(unclaimed) - args.limit} more "
                          "unclaimed segments (raise --limit to see them).[/dim]")
    if yours:
        console.print(f"[green]👑 You're already the Local Legend on "
                      f"{len(yours)} segment(s): "
                      f"{', '.join(r['name'] for r in yours)}[/green]")
    if args.csv:
        export_csv(args.csv, claimed, unclaimed, yours)
        console.print(f"Exported {len(claimed) + len(unclaimed) + len(yours)} "
                      f"segments to [bold]{args.csv}[/bold]")
    if skipped and not limited:
        console.print(f"[dim]{skipped} segment(s) skipped (detail fetch failed).[/dim]")
    print_budget()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
