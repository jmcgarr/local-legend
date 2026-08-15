# strava-local-legends

A CLI that finds cycling segments in a zip code where you have the best shot at
becoming the Strava **Local Legend** (most efforts in a rolling 90-day window).

Segments are scored by, in priority order:

1. **Already ridden** — segments you've done before get a big boost
2. **Few efforts to beat** — the current Local Legend's 90-day effort count (unclaimed segments rank best)
3. **Shorter** distance
4. **Flatter** average grade

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
| `--limit` | 15 | Rows in the final table (also caps detail API calls) |
| `--max-rides` | 10 | How many of your past rides in the area to mine |
| `--no-explore` / `--no-history` | off | Use only one discovery method |
| `--country` | us | Country code for zip lookup |
| `--reset-auth` | off | Forget stored credentials and re-authenticate |

### Example output

```
                Easiest Local Legend targets — Petaluma, CA 94952
 #  Segment              Dist    Grade  You  To beat*  Score  Link
 1  D St Sprint          0.4 km   0.2%    7         4     96  https://www.strava.com/segments/…
 2  Bodega Ave Rollers   1.1 km   1.0%    —  unclaimed!    71  https://www.strava.com/segments/…
```

`To beat*` is the current Local Legend's effort count over the last 90 days —
you become the Legend by logging **more** efforts than that within any 90-day
window. `You` is your lifetime effort count on the segment.

## Rate limits

Standard-tier apps get ~200 requests / 15 min and 2,000 / day. A default run
uses roughly `21 × zips` (explore, worst case) + `2` (activity list) +
`max-rides` + `limit` calls ≈ 50. If you hit a 429, wait 15 minutes.

## Notes

- Zip → coordinates uses the free, keyless [Zippopotam.us](https://api.zippopotam.us)
  service; the search area is a box around the zip centroid, not the exact
  zip-code polygon.
- Cycling only (`Ride`, `GravelRide`, `MountainBikeRide`); virtual rides are ignored.
