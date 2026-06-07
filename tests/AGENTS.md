# Tests Agent Guide

## Scope

This guide applies to files under `tests/`.

## Test Harness Contract

- Keep tests lightweight and `unittest` based.
- Tests must run locally without requiring a live Red Discord Bot instance, a
  Discord connection, or the Unraid deployment environment.
- Prefer deterministic cog logic tests: parsing, formatting, command/config
  branch behavior, fallbacks, and simple error paths.
- Use and extend `tests/support.py` for local stubs of `discord`, `redbot.core`,
  `aiohttp`, and `PIL` when needed.
- Do not turn the shared stubs into broad integration simulations. Keep them as
  small as the covered behavior allows.

## Cog-Specific Tests

- For `tests/test_implingfinder_*.py`, also follow `implingfinder/AGENTS.md`.
- For other cogs, follow the root repo guide and any nested cog guide if one is
  added later.

## Verification

Run the focused test module for the changed cog first, for example:

```bash
python3 -m unittest tests.test_remoji
```

Before completion for behavior changes, run:

```bash
python3 -m unittest discover tests
git diff --check
```
