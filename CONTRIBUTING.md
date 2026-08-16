# Contributing

Thanks for your interest! This is a small single-file tool — the bar for
contributing is low.

## Setup

You need [uv](https://docs.astral.sh/uv/) (or a classic venv; see the README's
Development section). To run the tool from your checkout:

```sh
uv run strava_legends.py <zip>
```

You'll need your own Strava API application (see the README) to run it for
real; the test suite needs neither credentials nor network.

## Tests

```sh
uv run --with pytest -m pytest
```

All tests are offline. If you fix a bug, add a test that would have caught
it; if you add a flag or feature, cover its logic. CI runs the suite on
Linux and macOS across Python 3.11–3.14 and must be green before merge.

## Pull requests

- Keep changes focused; one topic per PR.
- Match the existing style (plain Python, no new dependencies without
  discussion — the single-file, three-dependency design is deliberate).
- Be mindful of Strava API costs: anything that adds API calls should use
  the existing cache layer and respect the rate-limit salvage path.
- Never include real Strava data, API responses with personal information,
  or your Client ID/Secret in code, tests, fixtures, or screenshots.
