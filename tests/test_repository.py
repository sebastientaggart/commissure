"""Core's tracked files name no machine path and hold no credential (AGENTS.md Section 1).

Solutions are separate repositories, so this guards only what core itself publishes.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBID = re.compile(
    r"/Users/[A-Za-z]|/home/[a-z]"  # a path from a developer machine
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:ghp|gho|github_pat|glpat)_[A-Za-z0-9_]{20,}"  # GitHub / GitLab tokens
    r"|\bsk-[A-Za-z0-9]{20,}"  # API keys in the sk- style
)


def leaks(root: Path, paths: list[str]) -> list[str]:
    found = []
    for rel in paths:
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary fixtures and images
        found += [f"{rel}: {m.group(0)}" for m in FORBID.finditer(text)]
    return found


def test_tracked_files_name_no_machine_path_or_credential():
    out = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True
    )
    assert leaks(ROOT, out.splitlines()) == []


def test_the_scan_can_fail(tmp_path):
    (tmp_path / "a.py").write_text('HOME = "/Users' + '/alice/notes"\n', encoding="utf-8")
    (tmp_path / "b.md").write_text("token ghp_" + "x" * 24 + "\n", encoding="utf-8")
    assert len(leaks(tmp_path, ["a.py", "b.md"])) == 2


def test_a_solution_checked_out_inside_core_is_ignored(tmp_path):
    """The check can fail: without the `/solutions/` line, a private solution's files under
    a core checkout would show as untracked and could be committed to the public repository."""
    repo = tmp_path / "core"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    ignore = (ROOT / ".gitignore").read_text()
    for text, ignored in ((ignore, True), (ignore.replace("/solutions/\n", ""), False)):
        (repo / ".gitignore").write_text(text)
        probe = subprocess.run(
            ["git", "check-ignore", "-q", "solutions/acme/recipe/recipe.py"], cwd=repo
        )
        assert (probe.returncode == 0) is ignored
