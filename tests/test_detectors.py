from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from splunk.detectors import detect_numeric_anomalies, detect_slow_queries


def test_detect_slow_queries_finds_events_over_threshold():
    df = pl.DataFrame({
        "host": ["a", "b", "c"],
        "duration_ms": [200, 1500, 3000],
        "query": ["fast search", "slow search", "slowest search"],
    })
    result = detect_slow_queries(df, threshold_ms=1000)
    assert len(result) == 2
    assert result[0]["duration_ms"] == 3000
    assert result[0]["host"] == "c"
    assert result[1]["duration_ms"] == 1500


def test_detect_slow_queries_no_duration_field_returns_empty():
    df = pl.DataFrame({"host": ["a"], "message": ["no timing info"]})
    assert detect_slow_queries(df) == []


def test_detect_slow_queries_empty_df_returns_empty():
    assert detect_slow_queries(pl.DataFrame()) == []


def test_detect_slow_queries_uses_alternate_field_names():
    df = pl.DataFrame({"host": ["a"], "elapsed": [5000]})
    result = detect_slow_queries(df, threshold_ms=1000)
    assert len(result) == 1
    assert result[0]["field"] == "elapsed"


def _timed_df(values: list[float], host: str = "a") -> pl.DataFrame:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = [base + timedelta(seconds=i) for i in range(len(values))]
    return pl.DataFrame({
        "time": times,
        "host": [host] * len(values),
        "duration_ms": values,
    })


def test_detect_numeric_anomalies_flags_outlier():
    values = [100.0] * 25 + [5000.0] + [100.0] * 5
    df = _timed_df(values)
    result = detect_numeric_anomalies(df, window=20, z_threshold=3.0)
    assert len(result) >= 1
    assert any(r["value"] == 5000.0 for r in result)
    assert result[0]["host"] == "a"


def test_detect_numeric_anomalies_no_outliers_returns_empty():
    values = [100.0 + (i % 3) for i in range(30)]
    df = _timed_df(values)
    assert detect_numeric_anomalies(df, window=20, z_threshold=3.0) == []


def test_detect_numeric_anomalies_not_enough_events_returns_empty():
    df = _timed_df([100.0, 200.0, 300.0])
    assert detect_numeric_anomalies(df, window=20) == []


def test_detect_numeric_anomalies_no_numeric_field_returns_empty():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    df = pl.DataFrame({
        "time": [base + timedelta(seconds=i) for i in range(30)],
        "message": ["x"] * 30,
    })
    assert detect_numeric_anomalies(df) == []


def test_detect_numeric_anomalies_tags_window_contamination(caplog):
    # Three consecutive elevated points: once the first is flagged, the
    # rolling window feeding the next two still contains it, inflating mean
    # and std for their own z-scores too. All three cross the threshold, but
    # only the strongest should be treated as the genuine anomaly — the
    # other two are tagged window_contaminated and logged as an artifact.
    baseline = [100.0 + (i % 5) for i in range(25)]
    values = baseline + [5000.0, 4900.0, 4950.0] + [100.0] * 5
    df = _timed_df(values)
    with caplog.at_level("WARNING"):
        result = detect_numeric_anomalies(df, window=20, z_threshold=2.0)

    assert len(result) >= 2
    leader = max(result, key=lambda r: abs(r["z_score"]))
    assert leader["window_contaminated"] is False
    followers = [r for r in result if r is not leader]
    assert followers and all(r["window_contaminated"] for r in followers)
    assert any("window contamination" in rec.message for rec in caplog.records)
