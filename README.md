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

2. **Get the tool**, either way:
   - **With [uv](https://docs.astral.sh/uv/)** (once: `brew install uv` or
     `curl -LsSf https://astral.sh/uv/install.sh | sh`). No venv, no pip; the
     script declares its own dependencies ([PEP 723]) and uv resolves them
     transparently on first run.
   - **Prebuilt binary** — download the one for your platform from
     [GitHub Releases](../../releases), `chmod +x` it, and run. No Python
     needed. macOS notes: the binary is unsigned, so the first launch needs
     right-click → Open (or `xattr -d com.apple.quarantine <file>`), and
     macOS will ask once for keychain permission — click **Always Allow**.

3. **First run** — you'll be prompted for the Client ID and Client Secret from
   step 1, then a browser window opens for a one-time Strava authorization.
   Everything (client secret, access token, refresh token) is stored in the
   **macOS Keychain** — nothing sensitive is written to disk, and tokens
   auto-refresh, so you never need to log in again.

[PEP 723]: https://peps.python.org/pep-0723/

## Usage

```sh
uv run strava_legends.py 94952
uv run strava_legends.py 94952 94954 --radius-km 8 --limit 20
./strava_legends.py 94952          # the shebang also invokes uv directly
./strava-legends-macos-arm64 94952 # or the downloaded binary
```

| Flag | Default | Meaning |
|---|---|---|
| `--radius-km` | 5 | Search radius around each zip's centroid |
| `--limit` | 15 | Rows shown per table |
| `--scan` | 0 (= all) | Cap segment-detail lookups to the N shortest/flattest candidates; by default every segment found in the area is checked |
| `--ridden-only` | off | Ignore segments you've never ridden (skips the Explore sweep — much cheaper; ridden = found in your mined history, so raise `--max-rides` for deeper coverage) |
| `--max-rides` | 10 | How many of your past rides in the area to mine |
| `--no-explore` / `--no-history` | off | Use only one discovery method |
| `--csv PATH` | off | Also export all scanned segments (claimed, unclaimed, and ones you already hold) to a CSV file |
| `--metric` | off | Show distances in meters/km (default is miles) |
| `--country` | us | Country code for zip lookup |
| `--flush-cache` | off | Delete all cached API responses; alone it just flushes and exits, with zip codes it flushes then runs fresh |
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
- **segment details & your 90-day effort counts** — 24 hours (expired
  entries are pruned on every run, so segment data flushes itself daily)
- **explore sweeps** — 7 days
- **zip geocodes** — forever

`--flush-cache` wipes everything on demand.

The first run of a new area costs ~50–80 calls; repeat runs the same day cost
a handful. If you do hit the limit mid-run, the tool prints what it gathered
(everything fetched is cached) and a re-run 15 minutes later resumes cheaply.

## Development

```sh
uv run --with pytest -m pytest tests/
```

(Or the classic way: `python3 -m venv .venv`,
`.venv/bin/pip install -r requirements.txt pytest`, then
`.venv/bin/python -m pytest tests/`.)

The test suite is fully offline (no Strava credentials or network needed).
GitHub Actions runs it on Linux and macOS across Python 3.11–3.14 (matching
the script's `requires-python`) on every push and pull request.

**Releases:** pushing a version tag (`git tag v0.1.0 && git push --tags`)
triggers a workflow that runs the tests, builds single-file binaries with
PyInstaller for Linux (x86_64), macOS (Apple Silicon), and Windows,
smoke-tests each, and attaches them to a GitHub Release for that tag.
(No Intel-mac binary: GitHub retired the `macos-13` Intel runners, and the
arm64 binary won't run on Intel Macs — Intel-mac users should use the uv
path instead.)

## TODO

- **Publish to PyPI** so `uvx strava-legends` / `pipx install strava-legends`
  work: add a `pyproject.toml` with a `strava-legends` console entry point,
  register the repo for [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
  on PyPI (OIDC — no API token secrets), and add a publish job to the release
  workflow (`uv build` + `pypa/gh-action-pypi-publish`).

## License

MIT — see [LICENSE](LICENSE).

This project is not affiliated with, endorsed by, or sponsored by Strava.
You bring your own Strava API application (created under your own account),
and your use of the Strava API is governed by the
[Strava API Agreement](https://www.strava.com/legal/api). Never commit or
share your Client ID/Secret. Note that `--csv` exports contain your personal
Strava data (they're gitignored for that reason).

## Notes

- Zip → coordinates uses the free, keyless [Zippopotam.us](https://api.zippopotam.us)
  service; the search area is a box around the zip centroid, not the exact
  zip-code polygon.
- Cycling only (`Ride`, `GravelRide`, `MountainBikeRide`); virtual rides are ignored.
