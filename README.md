# strava-local-legends

A CLI that finds cycling segments in a zip code where you have the best shot at
becoming the Strava **Local Legend** (most efforts in a rolling 90-day window).

**Every segment found in the area is checked by default** — ridden or not —
and results are sorted by **fewest rides needed to claim Local Legend**: the
current Legend's 90-day effort count plus one, **minus the efforts you've
already logged in the current 90-day window**. Ties are broken by a score that
favors segments you've **already ridden**, then **shorter**, then **flatter**.
Unclaimed segments (one ride = instant Legend) get their own second table, and
segments where you're *already* the Legend are reported separately.

A full first scan of a dense area can exceed one 15-minute rate window; the
tool shows partial results, caches everything, and a re-run resumes where it
left off. Use `--scan N` to cap the sweep instead.

Output is a table with a clickable Strava link for each segment.

## How it finds segments (hybrid discovery)

- **Explore API**: sweeps the zip-code area for all segments, subdividing the
  area when Strava's 10-segments-per-box cap is hit.
  ⚠️ Strava restricts this endpoint to *Extended Access tier* apps on
  **Sept 1, 2026**. When that happens the tool automatically falls back to:
- **Your ride history**: recent rides that started near the zip code are mined
  for the segments you rode through.

## Setup (one time)

1. **Strava API app** — go to <https://www.strava.com/settings/api> and create
   an application. Any name/category/website is fine; set
   **Authorization Callback Domain** to `localhost`.
   Note: since June 2026, Strava requires an active Strava subscription on
   your account for Standard-tier API access.

2. **Install**:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

3. **First run** — you'll be prompted for the Client ID and Client Secret from
   step 1, then a browser window opens for a one-time Strava authorization.
   Everything (client secret, access token, refresh token) is stored in the
   **macOS Keychain** — nothing sensitive is written to disk, and tokens
   auto-refresh, so you never need to log in again.

## Usage

```sh
.venv/bin/python strava_legends.py 94952
.venv/bin/python strava_legends.py 94952 94954 --radius-km 8 --limit 20
```

| Flag | Default | Meaning |
|---|---|---|
| `--radius-km` | 5 | Search radius around each zip's centroid |
| `--limit` | 15 | Rows shown per table |
| `--scan` | 0 (= all) | Cap segment-detail lookups to the N shortest/flattest candidates; by default every segment found in the area is checked |
| `--max-rides` | 10 | How many of your past rides in the area to mine |
| `--no-explore` / `--no-history` | off | Use only one discovery method |
| `--csv PATH` | off | Also export all scanned segments (claimed, unclaimed, and ones you already hold) to a CSV file |
| `--country` | us | Country code for zip lookup |
| `--no-cache` | off | Ignore cached API responses this run |
| `--reset-auth` | off | Forget stored credentials and re-authenticate |

### Example output

```
                Easiest Local Legend targets — Petaluma, CA 94952
 #  Segment              Dist    Grade  You 90d/all  Rides needed*  Score  Link
 1  D St Sprint          0.4 km   0.2%         3/70              2     96  https://www.strava.com/segments/…
 2  Western Ave Roll     2.1 km   0.9%         0/12             15     64  https://www.strava.com/segments/…

               Unclaimed segments (1 ride = Local Legend) — Petaluma, CA 94952
 #  Segment              Dist    Grade  You 90d/all  Link
 1  Bodega Ave Rollers   1.1 km   1.0%            —  https://www.strava.com/segments/…
```

`Rides needed*` = current Legend's 90-day effort count + 1, minus the efforts
you've logged in the last 90 days. Your 90-day counts come from the List
Segment Efforts endpoint (needs a Strava subscription); without one they're
estimated from your mined ride history.

## Rate limits & caching

Standard-tier apps get ~200 requests / 15 min and 2,000 / day. Every run
prints how many API calls it made, how many came from cache, and your current
Strava budget (read from Strava's rate-limit headers).

API responses are cached in `~/.cache/strava-legends/`:

- **your activities** — forever (they're immutable); the activity list is
  fetched incrementally (only rides newer than the last run)
- **segment details & your 90-day effort counts** — 24 hours
- **explore sweeps** — 7 days
- **zip geocodes** — forever

The first run of a new area costs ~50–80 calls; repeat runs the same day cost
a handful. If you do hit the limit mid-run, the tool prints what it gathered
(everything fetched is cached) and a re-run 15 minutes later resumes cheaply.

## Notes

- Zip → coordinates uses the free, keyless [Zippopotam.us](https://api.zippopotam.us)
  service; the search area is a box around the zip centroid, not the exact
  zip-code polygon.
- Cycling only (`Ride`, `GravelRide`, `MountainBikeRide`); virtual rides are ignored.
