from __future__ import annotations

import json

from splunk.parsers import parse_splunk_json


# ---------------------------------------------------------------------------
# Schema cache — shape mismatch fallback
#
# No mocking here, exercises real splunk.db (same convention as
# TestActiveRunsDB in test_connector.py). Reproduces the bug found while
# testing the --live path against a real Splunk instance (local_splunk/):
# a plain search caches a scalar dtype for a field, then a later query
# against the same sourcetype (e.g. piped through `transaction`) returns
# that same field as a list -- construction with the stale cached schema
# must fall back to inference rather than crash, and the cache must be
# reset (not merged) so a third query shape doesn't collide with the first.
# ---------------------------------------------------------------------------

class TestSchemaCacheShapeMismatch:
    SOURCETYPE = "test-schema-mismatch-sourcetype"

    def setup_method(self):
        from splunk.db import init_db, reset_schema
        init_db()
        reset_schema(self.SOURCETYPE, {})

    def teardown_method(self):
        from splunk.db import reset_schema
        reset_schema(self.SOURCETYPE, {})

    def _raw(self, records: list[dict]) -> str:
        return json.dumps({"results": records})

    def test_cached_schema_reused_for_stable_shape(self):
        first = self._raw([{"sourcetype": self.SOURCETYPE, "host": "a", "value": "x"}])
        parse_splunk_json(first)

        from splunk.db import load_schema
        assert load_schema(self.SOURCETYPE) is not None

        second = self._raw([{"sourcetype": self.SOURCETYPE, "host": "b", "value": "y"}])
        df = parse_splunk_json(second)
        assert df.height == 1
        assert df["value"][0] == "y"

    def test_shape_mismatch_falls_back_to_inference_instead_of_raising(self):
        # First shape: 'value' is a scalar string -> cached as String.
        scalar = self._raw([{"sourcetype": self.SOURCETYPE, "host": "a", "value": "scalar-value"}])
        parse_splunk_json(scalar)

        # Second shape: same sourcetype, same field name, but 'value' is now
        # a list -- mirrors transaction/stats producing multivalue fields
        # where a plain search returned a scalar for the same field.
        listy = self._raw([{"sourcetype": self.SOURCETYPE, "host": "a", "value": ["multi", "valued"]}])
        df = parse_splunk_json(listy)  # must not raise

        assert df.height == 1
        assert list(df["value"][0]) == ["multi", "valued"]

    def test_cache_is_reset_not_merged_after_mismatch(self):
        scalar = self._raw([{"sourcetype": self.SOURCETYPE, "host": "a", "value": "scalar-value"}])
        parse_splunk_json(scalar)

        listy = self._raw([{"sourcetype": self.SOURCETYPE, "host": "a", "value": ["multi", "valued"]}])
        parse_splunk_json(listy)

        from splunk.db import load_schema
        schema = load_schema(self.SOURCETYPE)
        assert schema is not None
        # Stale 'String' dtype from the first (scalar) shape must be gone --
        # a plain upsert-merge would have left both dtypes fighting over the
        # same field_name row; reset_schema deletes-then-inserts instead.
        assert schema["value"] != "String"

        # A THIRD query in the original (scalar) shape must also succeed --
        # this is what an upsert-merge (rather than reset) would still break,
        # since it would leave list-shaped rows behind that a scalar can't fill.
        third = self._raw([{"sourcetype": self.SOURCETYPE, "host": "a", "value": "scalar-again"}])
        df = parse_splunk_json(third)
        assert df.height == 1
