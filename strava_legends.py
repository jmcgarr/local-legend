#!/usr/bin/env python3
"""
strava-legends: find Strava segments in a zip code where you have the best
shot at becoming the Local Legend.

Discovery is hybrid:
  1. Explore Segments API — sweeps the zip code area for all segments.
     (Restricted to Extended Access tier apps starting Sept 1, 2026; the
     tool degrades gracefully to history-only after that.)
  2. Your own recent rides in the area — segments you've already ridden.

Segments are scored by: already ridden > few Local Legend efforts to beat
> shorter > flatter.
"""

import argparse
import http.server
import json
import math
import sys
import threading
import time
import urllib.parse
import webbrowser

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

console = Console()


# --------------------------------------------------------------------------
# Credentials & OAuth (one-time browser auth, tokens live in the OS keychain)
# --------------------------------------------------------------------------

def get_client_credentials(reset=False):
    if reset:
        for key in ("client_id", "client_secret", "tokens"):
            try:
                keyring.delete_password(SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass

    client_id = keyring.get_password(SERVICE, "client_id")
    client_secret = keyring.get_password(SERVICE, "client_secret")

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

    raw = keyring.get_password(SERVICE, "tokens")
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

def api_get(token, path, params=None):
    resp = requests.get(f"{API_BASE}{path}", params=params,
                        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code == 429:
        console.print("[red]Strava rate limit hit (200 req/15 min, 2000/day). "
                      "Wait ~15 minutes and try again, or lower --limit.[/red]")
        sys.exit(1)
    if resp.status_code in (401, 403):
        console.print(f"[red]Strava returned {resp.status_code} for {path} — "
                      "your app tier may not have access to this endpoint.[/red]")
        return None
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------

def zip_to_box(zip_code, radius_km, country="us"):
    """Zip code -> (lat, lng, bounding box) via the free Zippopotam.us API."""
    resp = requests.get(f"https://api.zippopotam.us/{country}/{zip_code}", timeout=15)
    if resp.status_code == 404:
        console.print(f"[red]Zip code {zip_code} not found.[/red]")
        return None
    resp.raise_for_status()
    place = resp.json()["places"][0]
    lat, lng = float(place["latitude"]), float(place["longitude"])
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.1))
    box = (lat - dlat, lng - dlng, lat + dlat, lng + dlng)
    label = f'{place["place name"]}, {place.get("state abbreviation", "")}'.strip(", ")
    return {"zip": zip_code, "label": label, "lat": lat, "lng": lng, "box": box}


def in_any_box(latlng, boxes, pad=0.0):
    if not latlng or len(latlng) != 2:
        return False
    lat, lng = latlng
    return any(b[0] - pad <= lat <= b[2] + pad and b[1] - pad <= lng <= b[3] + pad
               for b in boxes)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def explore_segments(token, box, depth=0):
    """Explore returns at most 10 segments per box; subdivide when saturated."""
    bounds = f"{box[0]},{box[1]},{box[2]},{box[3]}"
    data = api_get(token, "/segments/explore",
                   {"bounds": bounds, "activity_type": "riding"})
    if data is None:  # endpoint restricted (post Sept 1, 2026) or forbidden
        return None
    segs = data.get("segments", [])
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
            sub = explore_segments(token, quad, depth + 1)
            if sub:
                segs.extend(sub)
    return segs


def segments_from_history(token, boxes, max_rides):
    """Segments you've ridden: scan recent rides that started near the zip(s)."""
    ridden = {}
    area_rides = []
    for page in (1, 2):
        activities = api_get(token, "/athlete/activities",
                             {"per_page": 100, "page": page}) or []
        for act in activities:
            if act.get("type") not in ("Ride", "GravelRide", "MountainBikeRide"):
                continue
            if in_any_box(act.get("start_latlng"), boxes, pad=0.05):
                area_rides.append(act)
        if len(activities) < 100 or len(area_rides) >= max_rides:
            break

    mined = area_rides[:max_rides]
    for act in mined:
        detail = api_get(token, f"/activities/{act['id']}",
                         {"include_all_efforts": "true"})
        if not detail:
            continue
        for effort in detail.get("segment_efforts", []):
            seg = effort.get("segment") or {}
            if seg.get("id") and in_any_box(seg.get("start_latlng"), boxes, pad=0.01):
                ridden[seg["id"]] = seg
    return ridden, len(mined), len(area_rides)


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
        s = (3.0 * (1.0 if r["your_efforts"] > 0 else 0.0)
             + 2.5 * (1.0 - norm(r["ll_efforts"], targets))
             + 1.5 * (1.0 - norm(r["distance"], dists))
             + 1.5 * (1.0 - norm(abs(r["avg_grade"]), grades)))
        r["score"] = round(s / 8.5 * 100)


def print_table(rows, area_labels):
    table = Table(title=f"Easiest Local Legend targets — {', '.join(area_labels)}",
                  show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Segment")
    table.add_column("Dist", justify="right")
    table.add_column("Grade", justify="right")
    table.add_column("You", justify="right")
    table.add_column("To beat*", justify="right")
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Link", style="cyan")

    for i, r in enumerate(rows, 1):
        url = f"https://www.strava.com/segments/{r['id']}"
        dist = f"{r['distance'] / 1000:.1f} km" if r["distance"] >= 1000 \
            else f"{r['distance']:.0f} m"
        to_beat = "[green]unclaimed![/green]" if r["ll_efforts"] == 0 \
            else str(r["ll_efforts"])
        you = f"[green]{r['your_efforts']}[/green]" if r["your_efforts"] else "—"
        table.add_row(str(i), f"[link={url}]{r['name']}[/link]", dist,
                      f"{r['avg_grade']:.1f}%", you, to_beat, str(r["score"]), url)

    console.print(table)
    console.print("[dim]* Current Local Legend's effort count in the last 90 days "
                  "(you need more efforts than this in a rolling 90-day window). "
                  "'You' = your lifetime efforts on the segment.[/dim]")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="strava-legends",
        description="Find Strava segments in a zip code that are easiest to "
                    "become the Local Legend on (cycling).")
    parser.add_argument("zips", nargs="+", help="One or more zip codes")
    parser.add_argument("--radius-km", type=float, default=5.0,
                        help="Search radius around each zip centroid (default 5)")
    parser.add_argument("--limit", type=int, default=15,
                        help="Number of segments in the final table (default 15)")
    parser.add_argument("--max-rides", type=int, default=10,
                        help="Max past rides in the area to mine for segments (default 10)")
    parser.add_argument("--no-explore", action="store_true",
                        help="Skip the Explore API (only segments you've ridden)")
    parser.add_argument("--no-history", action="store_true",
                        help="Skip mining your ride history")
    parser.add_argument("--country", default="us",
                        help="Country code for zip lookup (default us)")
    parser.add_argument("--reset-auth", action="store_true",
                        help="Forget stored credentials and re-authenticate")
    args = parser.parse_args()

    token = get_access_token(reset=args.reset_auth)

    areas = [a for a in (zip_to_box(z, args.radius_km, args.country)
                         for z in args.zips) if a]
    if not areas:
        sys.exit(1)
    boxes = [a["box"] for a in areas]
    for a in areas:
        console.print(f"Searching around [bold]{a['label']} ({a['zip']})[/bold] "
                      f"±{args.radius_km:g} km")

    candidates = {}  # segment id -> summary dict

    if not args.no_explore:
        with console.status("Exploring segments in the area..."):
            for a in areas:
                segs = explore_segments(token, a["box"])
                if segs is None:
                    console.print(
                        "[yellow]Explore Segments is unavailable to this app "
                        "(restricted to Extended Access tier since Sept 1, 2026). "
                        "Falling back to your ride history only.[/yellow]")
                    break
                for s in segs:
                    candidates[s["id"]] = {
                        "id": s["id"], "name": s["name"],
                        "distance": s.get("distance", 0.0),
                        "avg_grade": s.get("avg_grade", 0.0),
                        "ridden_hint": False,
                    }
        console.print(f"Explore found [bold]{len(candidates)}[/bold] segments.")

    if not args.no_history:
        with console.status("Mining your recent rides in the area..."):
            ridden, n_mined, n_found = segments_from_history(
                token, boxes, args.max_rides)
        console.print(
            f"Found [bold]{len(ridden)}[/bold] segments across "
            f"[bold]{n_mined}[/bold] of your [bold]{n_found}[/bold] recent "
            "rides in the area"
            + (f" (raise --max-rides to mine more)." if n_found > n_mined else "."))
        for sid, seg in ridden.items():
            entry = candidates.setdefault(sid, {
                "id": sid, "name": seg.get("name", "?"),
                "distance": seg.get("distance", 0.0),
                "avg_grade": seg.get("average_grade", 0.0),
            })
            entry["ridden_hint"] = True

    if not candidates:
        console.print("[red]No segments found. Try a bigger --radius-km.[/red]")
        sys.exit(1)

    # Cheap pre-rank so we only spend detail API calls on promising segments.
    pre = sorted(candidates.values(),
                 key=lambda s: (not s.get("ridden_hint"),
                                abs(s["avg_grade"]), s["distance"]))
    shortlist = pre[:args.limit]

    rows = []
    with console.status(f"Fetching details for top {len(shortlist)} segments..."):
        for s in shortlist:
            detail = api_get(token, f"/segments/{s['id']}") or {}
            ll = detail.get("local_legend") or {}
            your_stats = detail.get("athlete_segment_stats") or {}
            rows.append({
                "id": s["id"],
                "name": detail.get("name", s["name"]),
                "distance": detail.get("distance", s["distance"]),
                "avg_grade": detail.get("average_grade", s["avg_grade"]),
                "ll_efforts": int(ll.get("effort_count") or 0),
                "your_efforts": int(your_stats.get("effort_count") or 0)
                                or (1 if s.get("ridden_hint") else 0),
            })

    score_segments(rows)
    rows.sort(key=lambda r: r["score"], reverse=True)
    print_table(rows, [f"{a['label']} {a['zip']}" for a in areas])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
