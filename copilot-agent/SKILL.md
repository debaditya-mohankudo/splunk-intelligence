---
name: copilot-agent
description: Build, debug, and refine a GitHub Copilot agent workflow in this workspace. Use when the user wants a Copilot agent skill, prompt, or instruction file that tells Copilot how to inspect the repo, reason over the codebase, and make targeted changes without guessing.
user-invocable: true
cwd: /Users/debaditya/workspace/splunk_analysis
---

# Copilot Agent

Repository-specific instructions for a Copilot agent working in this workspace. The goal is to keep the agent local, precise, and grounded in the actual files in the repo.

## Repo

`/Users/debaditya/workspace/splunk_analysis`

## What this skill is for

Use this skill when the user wants Copilot to:
- understand the project structure before editing
- inspect the nearest owning files instead of searching broadly
- make focused changes and validate them
- explain what changed in plain language
- avoid inventing APIs, file names, or behavior that is not in the repo

## Embedded MCP Tools

This repo exposes the following MCP tools through `splunk/mcp_server.py` and the VS Code MCP config in `.vscode/mcp.json`.

- `splunk__investigate_start` — load a file or live SPL query, normalize the data, run deterministic detectors, and return structured findings plus a `run_id`.
- `splunk__submit_report` — submit a markdown report and follow-up SPL queries, persist the result, and advance the investigation loop.
- `splunk__get_findings` — read the current findings for an active run.
- `splunk__pause` — pause the investigation after the current iteration.
- `splunk__hint` — inject an analyst hint that will be used on the next iteration.

Recommended usage rules:

- Use the investigation tools only when the user is working on the Splunk analysis workflow in this repo.
- Prefer `splunk__investigate_start` first, then reason over the findings before calling `splunk__submit_report`.
- Keep follow-up SPL queries grounded in fields and values present in the findings.
- Use `splunk__get_findings` for mid-loop inspection instead of guessing the current state.
- Use `splunk__pause` and `splunk__hint` only when the user explicitly wants to steer an active run.

## Looping behavior

This skill should behave like an iterative investigation agent, not a single-pass summarizer.

Recommended loop:

1. Start with `splunk__investigate_start` on the user-provided file or live SPL.
2. Read the returned findings and form one falsifiable hypothesis about the root cause.
3. Draft a short report plus a small set of follow-up SPL queries that can disprove or refine that hypothesis.
4. Call `splunk__submit_report` with the report and queries.
5. Use the next findings returned by the hook to decide whether to tighten the hypothesis, broaden the search, or stop.
6. Repeat until the evidence converges, the confidence is high enough, or the user asks to pause.

Looping rules:

- Treat each iteration as a refinement step, not a rewrite of the whole analysis.
- Prefer the cheapest discriminating query first, then add more focused queries only if needed.
- Stop when the same root-cause hypothesis is supported by multiple detectors and the follow-up queries stop producing new signal.
- If the findings are sparse, cap confidence and keep the loop narrow rather than inventing broader theories.

## Working rules

- Start from the concrete anchor: a file, symbol, test, error, or user-visible behavior.
- Gather only the minimum local context needed before the first edit.
- Prefer the owning abstraction over wiring or registration files.
- Make the smallest change that can test the hypothesis.
- Validate after edits with the cheapest relevant check.
- Do not widen scope unless the first check disproves the current hypothesis.
- Keep any generated instructions aligned with the actual codebase conventions.

## Recommended flow

1. Read the relevant file or nearby test.
2. Form one falsifiable hypothesis about the behavior.
3. Make a small edit that tests that hypothesis.
4. Run a focused validation step.
5. Summarize the result with file links and the behavioral impact.

## Good outputs

A good Copilot agent response should:
- state the current understanding clearly
- mention the exact file or symbol being changed
- call out the validation performed
- avoid overexplaining unrelated parts of the repo

## When to ask for clarification

Ask only when:
- the target file or location is genuinely ambiguous
- multiple behaviors could be changed safely but differently
- the request requires a deployment or secret that cannot be inferred

## Key constraints

- Do not hallucinate code paths or tool behavior.
- Do not rewrite unrelated files.
- Do not add broad abstractions when a small local patch will do.
- Keep responses concise and grounded in the repository.
