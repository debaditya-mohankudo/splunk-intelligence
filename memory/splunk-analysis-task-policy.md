---
name: splunk-analysis-task-policy
description: Task creation policy for splunk_analysis repo — all tasks under epic d142e45a, no new epics
metadata:
  type: feedback
  domain: splunk
  tags: splunk, tasks, epic, repo, policy, task-create, subtask, parent
---

All tasks for `/Users/debaditya/workspace/splunk_analysis` must live under epic `d142e45a` (Local LLM Splunk Intelligence). No new epics for this repo. Before creating a task, check if a relevant one already exists under this epic. If the new work is small and related to an existing task, parent it to that task as a subtask instead of directly to the epic.

**Why:** User wants a single umbrella epic with clean hierarchy — subtasks under existing tasks when scope is narrow.

**How to apply:** New task → check existing tasks under `d142e45a` → if closely related and small, parent to that task; otherwise parent directly to `d142e45a`. Never create a new epic for this repo.
