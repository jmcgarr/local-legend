"""Offline unit tests — no Strava credentials or network access required."""

import csv
import http.server
import threading
import time
import urllib.error
import urllib.request

import pytest
from rich.console import Console

import strava_legends as sl


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Keep tests away from the real cache and terminal."""
    monkeypatch.setattr(sl, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(sl, "console", Console(width=100, force_terminal=False))


# --- units ------------------------------------------------------------------

def test_dist_cell_imperial(monkeypatch):
    monkeypatch.setattr(sl, "METRIC", False)
    assert sl.dist_cell({"distance": 184}) == "0.11 mi"
    assert sl.dist_cell({"distance": 1100}) == "0.68 mi"
    assert sl.dist_cell({"distance": 26100}) == "16.2 mi"


def test_dist_cell_metric(monkeypatch):
    monkeypatch.setattr(sl, "METRIC", True)
    assert sl.dist_cell({"distance": 184}) == "184 m"
    assert sl.dist_cell({"distance": 26100}) == "26.1 km"


# --- geometry ---------------------------------------------------------------

def test_in_any_box():
    box = (38.0, -123.0, 39.0, -122.0)
    assert sl.in_any_box([38.5, -122.5], [box])
    assert not sl.in_any_box([40.0, -122.5], [box])
    assert sl.in_any_box([39.02, -122.5], [box], pad=0.05)
    assert not sl.in_any_box(None, [box])
    assert not sl.in_any_box([], [box])


def test_zip_to_area_geocode_cached(monkeypatch):
    calls = []

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"places": [{"latitude": "38.24", "longitude": "-122.68",
                                "place name": "Petaluma",
                                "state abbreviation": "CA"}]}

    monkeypatch.setattr(sl.requests, "get",
                        lambda *a, **k: calls.append(a) or FakeResp())
    a1 = sl.zip_to_area("94952", 5.0)
    a2 = sl.zip_to_area("94952", 5.0)  # second call must come from cache
    assert len(calls) == 1
    assert a1["label"] == "Petaluma, CA"
    assert a1["box"] == a2["box"]
    lo_lat, lo_lng, hi_lat, hi_lng = a1["box"]
    assert lo_lat < 38.24 < hi_lat and lo_lng < -122.68 < hi_lng


# --- scoring & ranking ------------------------------------------------------

def test_norm():
    assert sl.norm(5, [0, 10]) == 0.5
    assert sl.norm(7, [7, 7]) == 0.0


def make_row(**kw):
    row = {"id": 1, "name": "seg", "distance": 500, "avg_grade": 0.5,
           "ll_efforts": 10, "your_lifetime": 0, "your_90d": 0,
           "rides_needed": 11}
    row.update(kw)
    return row


def test_score_favors_ridden_short_flat():
    rows = [make_row(id=1, your_lifetime=5, distance=400, avg_grade=0.1),
            make_row(id=2, your_lifetime=0, distance=5000, avg_grade=6.0)]
    sl.score_segments(rows)
    assert rows[0]["score"] > rows[1]["score"]


def test_rides_needed_sort_with_score_tiebreak():
    rows = [make_row(id=1, rides_needed=5, score=40),
            make_row(id=2, rides_needed=2, score=10),
            make_row(id=3, rides_needed=5, score=90)]
    rows.sort(key=lambda r: (r["rides_needed"], -r["score"]))
    assert [r["id"] for r in rows] == [2, 3, 1]


def test_rides_needed_formula():
    # Legend has 11 efforts, you have 3 in the window -> 9 more rides
    assert max(1, 11 + 1 - 3) == 9
    # You already exceed the Legend -> floor at 1
    assert max(1, 4 + 1 - 8) == 1


# --- cache ------------------------------------------------------------------

def test_cache_roundtrip_and_ttl_prune():
    now = time.time()
    sl.cache_save("segments", {"1": {"ts": now, "name": "fresh"},
                               "2": {"ts": now - 25 * 3600, "name": "stale"}})
    assert set(sl.cache_load("segments", sl.SEGMENT_TTL)) == {"1"}
    assert set(sl.cache_load("segments")) == {"1", "2"}  # no TTL, no prune


def test_cache_load_missing_or_corrupt(tmp_path):
    assert sl.cache_load("nonexistent") == {}
    sl.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (sl.CACHE_DIR / "bad.json").write_text("{not json")
    assert sl.cache_load("bad") == {}


def test_flush_cache():
    sl.cache_save("a", {})
    sl.cache_save("b", {})
    assert sl.flush_cache() == 2
    assert sl.flush_cache() == 0  # empty/missing dir is safe


def test_history_90day_counts():
    now = time.time()
    acts = {"details": {
        "10": [{"id": 5, "date": now - 10 * 86400},
               {"id": 5, "date": now - 200 * 86400}],
        "11": [{"id": 5, "date": now - 3 * 86400},
               {"id": 7, "date": now - 89 * 86400}],
    }}
    assert sl.history_90day_counts(acts) == {5: 2, 7: 1}


# --- output -----------------------------------------------------------------

def test_tables_never_truncate_urls(monkeypatch):
    rows = [make_row(id=1234567890,
                     name="A very long segment name to squeeze the columns",
                     score=88)]
    for width in (50, 80, 120):
        monkeypatch.setattr(sl, "console",
                            Console(width=width, force_terminal=False))
        for fn in (sl.print_claimed_table, sl.print_unclaimed_table):
            with sl.console.capture() as cap:
                fn(rows, ["T"])
            frags = [line.rsplit("│", 2)[1].strip()
                     for line in cap.get().splitlines() if line.count("│") >= 2]
            link_col = "".join(frags)
            assert "…" not in link_col
            assert "https://www.strava.com/segments/1234567890" in link_col


def test_export_csv(tmp_path):
    claimed = [make_row(id=1, name='With, "quotes"', your_90d=3,
                        rides_needed=2, score=88)]
    unclaimed = [make_row(id=2, ll_efforts=0, rides_needed=1, score=40)]
    yours = [{"id": 3, "name": "My crown", "distance": 1200, "avg_grade": 1.2,
              "ll_efforts": 9, "your_lifetime": 60}]
    path = tmp_path / "out.csv"
    sl.export_csv(path, claimed, unclaimed, yours)
    rows = list(csv.DictReader(open(path)))
    assert [r["status"] for r in rows] == ["claimed", "unclaimed", "yours"]
    assert rows[0]["name"] == 'With, "quotes"'
    assert rows[1]["url"] == "https://www.strava.com/segments/2"
    assert rows[2]["rides_needed"] == "" and rows[2]["score"] == ""


# --- OAuth callback server ---------------------------------------------------

def test_callback_survives_favicon_request():
    sl._CallbackHandler.code = None
    sl._CallbackHandler.error = None
    server = http.server.HTTPServer(("localhost", 0), sl._CallbackHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        urllib.request.urlopen(f"http://localhost:{port}/callback?code=abc123")
        with pytest.raises(urllib.error.HTTPError):  # 404, must not clobber
            urllib.request.urlopen(f"http://localhost:{port}/favicon.ico")
        assert sl._CallbackHandler.code == "abc123"

        sl._CallbackHandler.code = None
        urllib.request.urlopen(f"http://localhost:{port}/callback?error=access_denied")
        assert sl._CallbackHandler.error == "access_denied"
    finally:
        server.shutdown()
