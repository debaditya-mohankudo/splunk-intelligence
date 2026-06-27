---
name: splunk-proj-tasks-db-schema
description: proj_tasks.db schema — open_tasks table, SQL pattern for removing stale tags
metadata:
  type: reference
  domain: splunk
  tags: splunk, tasks, sqlite, db, schema, tags, proj_tasks
---

Task DB is at `~/.claude/proj_tasks.db`. Main table is `open_tasks` (not `tasks` or `proj_tasks`).

To remove a stale tag directly (MCP update tool only appends):
```sql
sqlite3 ~/.claude/proj_tasks.db "UPDATE open_tasks SET tags = REPLACE(tags, 'old_tag,', '') WHERE id = '<task_id>';"
```

**Why:** MCP `tasks__update` appends tags only — direct SQL is the only way to remove a tag.
**How to apply:** Use this when a task has a stale `parent:` tag after being moved between epics.
