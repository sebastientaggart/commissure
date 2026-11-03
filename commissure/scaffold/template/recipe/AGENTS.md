# Recipe: `__SOLUTION_NAME__`

Initialized __DATE__ by `commissure init`. Read the core playbook (`../../AGENTS.md`), API
surface (`../../commissure/api.py`), and workflow skills (`../../.agents/skills/`) first; this
file adds to them and never overrides core invariants. Keep solution changes inside
`solutions/__SOLUTION_NAME__/` (never leave uncommitted local edits in `../../commissure/`); if a
domain-neutral core change is needed, stop and propose a standalone pull request to `commissure`.

Fill in every section before writing extraction code. Until then `recipe.py` declares no
stages and fails at startup on purpose.

## Purpose

What operation this recipe performs, for whom, and what outputs (and interface, if any) it produces. Note any optional extras this recipe requires (for example `commissure[docx]` for `docxtpl` template rendering).

## Inputs

Sources, locators, roles (`corpus`, `reference`, ...), file types, and what reconnaissance (`commissure probe`) found: scanned pages, glyph anomalies, schema edge cases.

## Domain rules

Client-specific rules and edge cases, each with the input that motivated it.

## Quarantine rules

Every condition that yields `warning` or `quarantined`, with its reason code and the test that proves it fires.

## Acceptance gate

Thresholds on `discovered`, `attempted`, `valid`, `warning`, `quarantined`, `skipped` that make a run pass or fail, and any core fail-closed default this recipe tightens or relaxes.

## Definition of done

What the operator checks before outputs ship.
