# sleepyhauru-cogs Agent Guide

## Scope

This guide applies to the whole repository. This is a Red Discord Bot cog
collection, and each top-level cog directory should be treated as its own
product area.

## Repo Rules

- Inspect `git status --short --branch` before edits.
- Keep changes scoped to the requested cog or shared test/docs surface.
- Do not touch unrelated cogs unless the user explicitly expands the task.
- Add or update focused tests before changing behavior.
- Update the relevant README section when commands, behavior, dependencies, data
  sources, or operational expectations change.
- Prefer repo-local deterministic tests over live Discord, Redbot, or network
  checks for normal development.
- Work with existing user changes. Do not revert unrelated local edits.

## Cog-Specific Guides

- For ImplingFinder work, also follow `implingfinder/AGENTS.md`.
- ImplingFinder work includes `implingfinder/`, `tests/test_implingfinder_*.py`,
  ImplingFinder sections in `README.md`, and ImplingFinder plans or docs under
  `docs/`.
- For other cogs, use this repo-wide guide unless that cog has its own
  `AGENTS.md`.

## Shared Test Harness

- For changes under `tests/`, also follow `tests/AGENTS.md`.
- Keep tests lightweight and runnable without a live Red Discord Bot instance,
  Discord connection, or deployment environment.

## Verification

Run the smallest focused test command that covers the change. Before claiming
completion for behavior changes, normally run:

```bash
python3 -m unittest discover tests
git diff --check
git status --short --branch
```

For changed cog metadata, validate the changed `info.json` with
`python3 -m json.tool`.
