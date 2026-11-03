---
name: release-target
description: >-
  Ship solution changes to a client: find what changed for which target, propose version
  bumps and client-facing CHANGELOG entries, build the release with commissure release, review
  the release diff for leaks, publish the tarball, and push to the client only on explicit
  go. Use when asked to release, ship, deliver, publish, or send a solution or its changes
  to a client.
---

# Release a target

The mechanics live in `commissure/tools/release.py` (`commissure release`); this skill is the
judgment around them. Root `AGENTS.md` section 6 (Delivery) is the rule set and wins.

## 1. What changed, for whom

1. Find the solution's targets: `ls solutions/<solution>/releases/*.toml`.
2. For each target, list shipped changes since its last release:
   `git -C solutions/<solution> log --oneline release/<target>/v<version>..HEAD -- recipe CHANGELOG.md release.toml`
   (no tag yet: this is the first release). Read the diffs, not only the messages.
3. Inspect the exact file list that will ship (`include` minus `exclude` in `release.toml`) for
   leftover files, stale docstrings, or links to excluded/unshipped docs (`AGENTS.md`,
   solution `README.md`) before committing and building.
4. A core change ships too: compare `version` in the root `pyproject.toml` with the
   `commissure` version in `build/releases/<target>/release.json`.

## 2. Propose, then wait for the operator

- **Version** in `solutions/<solution>/release.toml`: on a solution's first release (no prior
  release on this target), keep `0.1.0` in `release.toml` unless the operator asks otherwise
  and replace the scaffold stub in `CHANGELOG.md`. On later releases, propose a bump: patch
  for a fix with the same outputs; minor for new fields, flags, or outputs; major for a
  changed output format, a new required input or step, or removed output. Say which rule
  applies.
- **CHANGELOG entry** under `## <version> (<date>)`, written for the client: what ships (on
  a first release) or what they will see differently and what they must do (on later
  releases). No internal names, rule numbers, file paths, other solutions or offices, or
  analyst questions.
- **`release.toml`**: update `summary`, `usage`, `include`, `exclude`, or `extras` if
  commands, shipped files, or optional dependencies changed.

Show the proposal and wait for approval. Then edit `release.toml` and `CHANGELOG.md`, and
have the operator commit (or get explicit confirmation before running `git commit`), since
`build` requires a clean solution working tree and tags `release/<target>/v<version>`.

## 3. Build

```sh
uv run commissure release build <target>
```

It runs the tests, builds the wheel, scans for forbidden text, installs the release from its
wheel and loads the recipe, then commits and tags (`v<version>` in `build/releases/<target>/`
and `release/<target>/v<version>` in `solutions/<solution>/`). A refusal is a finding: fix the
cause (bump, CHANGELOG, a leak in code or docs), never pass `--allow-dirty` or `--skip-tests`
for a real release, and never weaken a `forbid` pattern to get past it without the operator's
say.

## 4. Review the release diff

On a subsequent release:

```sh
git -C build/releases/<target> diff --stat v<prev> v<version>
git -C build/releases/<target> diff v<prev> v<version> -- . ':!uv.lock' ':!vendor'
```

On the first release:

```sh
git -C build/releases/<target> show --stat v<version>
git -C build/releases/<target> show v<version> -- . ':!uv.lock' ':!vendor'
```

Read it as the client will. Check: only the intended solution shipped; nothing names
another office's work, a person, an internal system, or a machine path; no notes, TODOs,
debug prints, or commented-out experiments; the generated README's commands match the
change. Report what you checked and anything doubtful.

## 5. Publish

1. `uv run commissure release publish <target> --tarball` when the target takes tarballs.
2. `uv run commissure release publish <target> --push` only after the operator says to
   push this release. A push is outward-facing and cannot be taken back.
3. Have the operator commit the tarball (`solutions/<solution>/releases/<target>-v<version>.tar.gz`)
   and push with tags (`git push && git push --tags`), or run those git commands only when
   explicitly asked: the `release/<target>/v<version>` tags keep release history on every
   machine.

Report: target, solution version, commissure version, what was published where, and the
review findings.
