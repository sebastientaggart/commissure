The source of truth for skills is in this folder, but claude code doesn't yet support .agents/, so:

- Create the real skill at `.agents/skills/<name>/SKILL.md`.
- Add one relative link: `.claude/skills/<name> → ../../.agents/skills/<name>`.
- Stage `.claude/skills/<name>`. Staging `.claude/skills/<name>/SKILL.md` fails with `fatal: pathspec '...' is beyond a symbolic link`, because that path walks through the link.