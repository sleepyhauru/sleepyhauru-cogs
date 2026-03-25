# Test Notes

These tests are lightweight `unittest`-based regression tests for the cogs in this repo.

They are designed to run locally without requiring a live Red Discord Bot instance, a Discord connection, or the Unraid deployment environment.

## How They Work

Most tests exercise deterministic cog logic such as:

- parsing
- formatting
- command/config branch behavior
- fallback handling
- simple error paths

The shared helper in `tests/support.py` provides small local stubs for parts of:

- `discord`
- `redbot.core`
- `aiohttp`
- `PIL`

This keeps the suite runnable on a normal local machine even if those packages are not installed.

## What These Tests Do Not Cover

These tests are not full integration tests.

They do not validate:

- live Discord API behavior
- real Redbot runtime behavior
- live network calls to external services
- deployment-specific behavior on the Unraid server

Because of that, they should be treated as a fast regression safety check, not as proof that every production interaction will work.

## Running The Suite

From the repo root:

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

To run a single test module:

```bash
./.venv/bin/python -m unittest tests.test_seventv -v
```

## When To Run Them

Run these tests after making cog changes, especially before committing or pushing.

If you change code that depends on new Redbot, Discord, aiohttp, or Pillow behavior, you may also need to update the stubs in `tests/support.py`.
