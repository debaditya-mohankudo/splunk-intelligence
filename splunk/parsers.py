"""
Deterministic parsers for Splunk log exports.
No LLM calls, no network — pure Polars DataFrame transformations.

Public contract:
  parse_splunk_json / parse_splunk_csv  → pl.DataFrame  (entry points)
  extract_cert_fields                   → pl.DataFrame
  extract_timestamps                    → pl.DataFrame
  build_timeline                        → pl.DataFrame
  group_by_host                         → dict[str, pl.DataFrame]
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone

import polars as pl

from splunk.config import CERT_FIELDS as _CERT_FIELDS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Loaders — return pl.DataFrame
# ---------------------------------------------------------------------------

def parse_splunk_json(raw: str) -> pl.DataFrame:
    """Parse Splunk REST API JSON export (/results?output_mode=json)."""
    logger.debug("Parsing JSON input (%d bytes)", len(raw))
    data = json.loads(raw)
    if isinstance(data, dict) and "results" in data:
        records = data["results"]
    elif isinstance(data, list):
        records = data
    else:
        logger.warning("Unexpected JSON structure — no 'results' key and not a list")
        return pl.DataFrame()
    if not records:
        logger.warning("JSON parsed but contained 0 records")
        return pl.DataFrame()

    # Check schema cache for the sourcetype of the first record
    sourcetype = str(records[0].get("sourcetype", ""))
    cached = _load_schema_cache(sourcetype) if sourcetype else None
    if cached:
        df = pl.DataFrame(records, schema=cached)
        logger.debug("JSON parse used cached schema for sourcetype=%s (%d fields)", sourcetype, len(cached))
    else:
        df = pl.DataFrame(records)
        if sourcetype:
            _save_schema_cache(sourcetype, df)

    logger.info("Parsed JSON — %d events, %d columns", df.height, df.width)
    return df


def parse_splunk_csv(raw: str) -> pl.DataFrame:
    """Parse Splunk CSV export."""
    logger.debug("Parsing CSV input (%d bytes)", len(raw))

    # Peek at sourcetype from header row if present
    first_line = raw.split("\n", 1)[0]
    headers = [h.strip() for h in first_line.split(",")]
    sourcetype = None  # CSV headers don't tell us sourcetype until we parse a row

    df = pl.read_csv(io.StringIO(raw), infer_schema=False)

    # Try to get sourcetype from data and register schema
    if "sourcetype" in df.columns and df.height > 0:
        sourcetype = str(df["sourcetype"][0])
        _save_schema_cache(sourcetype, df)

    logger.info("Parsed CSV — %d events, %d columns", df.height, df.width)
    return df


# ---------------------------------------------------------------------------
# Schema cache helpers
# ---------------------------------------------------------------------------

def _save_schema_cache(sourcetype: str, df: pl.DataFrame) -> None:
    """Persist column→dtype mapping for this sourcetype to splunk.db."""
    try:
        from splunk.db import init_db, save_schema
        init_db()
        schema = {col: str(dtype) for col, dtype in zip(df.columns, df.dtypes)}
        save_schema(sourcetype, schema)
        logger.debug("Schema cached for sourcetype=%s (%d fields)", sourcetype, len(schema))
    except Exception as exc:
        logger.warning("Could not save schema cache: %s", exc)


def _load_schema_cache(sourcetype: str) -> dict[str, type] | None:
    """Load cached schema from splunk.db and convert dtype strings to Polars types."""
    try:
        from splunk.db import init_db, load_schema
        init_db()
        raw_schema = load_schema(sourcetype)
        if not raw_schema:
            return None
        return {field: _str_to_polars_dtype(dtype) for field, dtype in raw_schema.items()}
    except Exception as exc:
        logger.warning("Could not load schema cache: %s", exc)
        return None


def _str_to_polars_dtype(dtype_str: str) -> pl.DataType:
    """Map stored dtype string back to a Polars dtype. Defaults to String."""
    _map = {
        "String": pl.String,
        "Int64": pl.Int64,
        "Int32": pl.Int32,
        "Float64": pl.Float64,
        "Boolean": pl.Boolean,
        "Datetime(time_unit='us', time_zone='UTC')": pl.Datetime("us", "UTC"),
    }
    return _map.get(dtype_str, pl.String)


# ---------------------------------------------------------------------------
# Transformations — DataFrame in, DataFrame out
# ---------------------------------------------------------------------------

def extract_cert_fields(df: pl.DataFrame) -> pl.DataFrame:
    """Add a 'cert' struct column containing any PKI fields present."""
    present = [c for c in _CERT_FIELDS if c in df.columns]
    if present:
        logger.info("Cert fields found: %s", present)
        return df.with_columns(pl.struct(present).alias("cert"))
    logger.debug("No cert fields found in DataFrame columns")
    return df.with_columns(pl.lit(None).alias("cert"))


def extract_timestamps(df: pl.DataFrame) -> pl.DataFrame:  # noqa: C901
    """
    Normalise _time to a UTC datetime added as 'time' column.
    Handles: Unix epoch float/int string, ISO-8601, Splunk '%m/%d/%Y %H:%M:%S'.
    """
    if "_time" not in df.columns:
        logger.warning("No '_time' column found — 'time' will be null for all events")
        return df.with_columns(pl.lit(None).cast(pl.Datetime("us", "UTC")).alias("time"))

    times = [_parse_time(str(v)) for v in df["_time"].to_list()]
    null_count = sum(1 for t in times if t is None)
    if null_count:
        logger.warning("Could not parse _time for %d/%d events", null_count, len(times))
    else:
        logger.debug("Timestamps parsed successfully for all %d events", len(times))
    return df.with_columns(pl.Series("time", times, dtype=pl.Datetime("us", "UTC")))


def build_timeline(df: pl.DataFrame) -> pl.DataFrame:
    """Sort by time ascending; add relative_offset_s from first event."""
    if "time" not in df.columns or df.is_empty():
        return df.with_columns(pl.lit(0.0).alias("relative_offset_s"))

    df = df.sort("time", nulls_last=True)
    anchor = df["time"][0]
    if anchor is None:
        return df.with_columns(pl.lit(0.0).alias("relative_offset_s"))

    offsets = [
        (t - anchor).total_seconds() if t is not None else 0.0
        for t in df["time"].to_list()
    ]
    return df.with_columns(pl.Series("relative_offset_s", offsets))


def group_by_host(df: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Partition DataFrame by effective host (host → src → hostname → 'unknown')."""
    host_expr = pl.lit("unknown").cast(pl.String)

    for col in ("hostname", "src", "host"):
        if col in df.columns:
            host_expr = (
                pl.when(pl.col(col).is_not_null() & (pl.col(col) != ""))
                .then(pl.col(col))
                .otherwise(host_expr)
            )

    df = df.with_columns(host_expr.alias("_eff_host"))
    return {
        str(host): group.drop("_eff_host")
        for host, group in df.partition_by("_eff_host", as_dict=True).items()
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_time(value: str) -> datetime | None:
    if not value or value in ("None", "null", ""):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, OSError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%m/%d/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None
