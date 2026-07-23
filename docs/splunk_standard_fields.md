# Splunk Standard Fields Reference

Common field names Splunk populates automatically or that appear consistently across
CIM-compliant data. Useful when deciding what to pull out as first-class columns in
`splunk/parsers.py` / `splunk/db.py` vs. leaving in `extra_json`.

## Default internal fields (present on nearly every event)

| Field | Meaning |
|-------|---------|
| `_time` | Event timestamp (epoch, normalized by Splunk) |
| `_raw` | Original raw event text |
| `_indextime` | Time the event was indexed (vs. when it occurred) |
| `host` | Hostname/IP of the machine that generated the event |
| `source` | File path, script, or input the data came from |
| `sourcetype` | Format/parsing class Splunk assigned (e.g. `access_combined`, `json`) |
| `index` | Index the event is stored in |
| `linecount` | Number of lines in the raw event |
| `punct` | Punctuation pattern of the raw event (used for pattern clustering) |
| `splunk_server` | Indexer that served the search result |

## App / deployment context

| Field | Meaning |
|-------|---------|
| `app` | Splunk app context the data/search is associated with (e.g. `search`, `unix`, custom TA) |
| `sourcetype` | Often app-scoped by naming convention, e.g. `myapp:access` |
| `eventtype` | Saved Splunk classification tag matched against the event |
| `tag` | CIM tags associated via eventtypes (e.g. `authentication`, `error`) |

## CIM (Common Information Model) fields — used across correlated data models

| Field | Meaning |
|-------|---------|
| `user` | Username associated with the event |
| `src` / `src_ip` | Source address of the action |
| `dest` / `dest_ip` | Destination address of the action |
| `src_port` / `dest_port` | Network ports |
| `action` | Outcome, e.g. `success`, `failure`, `blocked`, `allowed` |
| `status` | HTTP or application status code/string |
| `severity` | Normalized severity (`critical`, `high`, `medium`, `low`, `informational`) |
| `signature` | Rule/alert name that fired (IDS/AV/correlation) |
| `process` / `process_name` | Process involved (endpoint data) |
| `url` | Requested URL (web/proxy data) |
| `http_method` | GET/POST/etc |
| `bytes` / `bytes_in` / `bytes_out` | Transfer size |
| `duration` | Time taken for the operation (ms or sec depending on source) |
| `error_code` / `error_message` | Application-level error identifiers |
| `transaction_id` / `session_id` / `request_id` | Correlation identifiers across a multi-event flow |

## This project's usage

`splunk/parsers.py` and `splunk/db.py` currently promote `host`, `sourcetype`, `source`,
`app`, `_time`, `_raw` to first-class columns (see `store_events` in `db.py`). Everything
else — including CIM fields like `user`, `src_ip`, `action`, `severity` — is preserved in
the catch-all `extra_json` column rather than duplicated into dedicated columns, since
which of these appear varies heavily by sourcetype/app. Promote a field to a real column
only once it's queried often enough across runs to justify the schema/migration cost
(see the `app` column migration in `init_db()` for the pattern to follow).
