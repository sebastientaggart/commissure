"""Verdict log: seeded samples of declared checks, append-only verdicts, honest error rates.

A **check** is a question about one kind of result. A recipe declares its checks on a
stage (`Stage(checks=(Check("/quotes/*", "..."),))`) over payload fields by JSON Pointer
pattern, where `*` matches any array index or object key; core declares the parse checks
in `PARSE_CHECKS` over the page evidence in `evidence_review`. A check's id is
`<stage>:<pattern>` (`stance:/quotes/*`, `parse:unreadable_pages`).

A **sample** is a seeded random draw of a check's instances from the stage's current,
complete, fresh records; its seed, population, and instances are recorded. A **verdict**
is a reviewer's answer (`correct`, `wrong`, `unclear`) on one instance. Samples,
instances, and verdicts are append-only; the latest verdict on an instance counts.

Only sampled instances count toward a rate. Spot checks (`spot`) are recorded and
reported, but kept out of every rate, because a hand-picked instance says nothing about
the population. An earlier verdict on an identical instance (same check, subject,
pointer or page, and value hash) is shown with the instance and never counted for it.

Writes here skip the workspace lock (`AGENTS.md`, "Human-verifiable results"): they touch
only the review tables, are append-only, and rely on SQLite's own write locking, so a
reviewer can work while a long run holds the workspace.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from commissure.inspect import _item_detail, _subject_evidence, _subject_item_versions
from commissure.workspace import (
    BLOBS_DIR,
    DB_NAME,
    MIGRATIONS,
    BlobStore,
    WorkspaceError,
    connect,
    transaction,
    utc_now,
)

if TYPE_CHECKING:
    from commissure.recipes import Recipe

ANSWERS = ("correct", "wrong", "unclear")
Z95 = 1.959963984540054  # two-sided 95% normal quantile

# Core checks over parse page evidence (`evidence_review`, origin 'parse'), by kind.
PARSE_CHECKS = {
    "parse:unreadable_pages": "Is this page unreadable to a person: no text a reader could use?",
    "parse:ocr_pages": "Is the recognised text of this page faithful to what the page shows?",
}


class ReviewError(ValueError):
    """A review request that cannot be honoured as asked (a usage error)."""


@dataclass(frozen=True)
class Check:
    """A declared question about every payload value matching `pattern` in a stage's records."""

    pattern: str
    question: str

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not self.pattern.startswith("/"):
            raise ValueError(f"check pattern {self.pattern!r} must be a JSON Pointer ('/...')")
        if any(token == "" for token in self.pattern.split("/")[1:]):
            raise ValueError(f"check pattern {self.pattern!r} has an empty segment")
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError(f"check {self.pattern!r} needs a question")


@dataclass(frozen=True)
class Instance:
    outcome_id: int
    run_id: int
    stage: str
    subject_key: str
    pointer: str | None  # recipe checks
    page: int | None  # parse checks
    value_hash: str


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def match(document: Any, pattern: str) -> list[tuple[str, Any]]:
    """Every `(pointer, value)` in `document` that `pattern` selects, in document order."""
    found: list[tuple[str, Any]] = [("", document)]
    for token in (_unescape(t) for t in pattern.split("/")[1:]):
        step: list[tuple[str, Any]] = []
        for pointer, value in found:
            if token == "*":
                children = (
                    list(enumerate(value))
                    if isinstance(value, list)
                    else list(value.items())
                    if isinstance(value, dict)
                    else []
                )
            elif isinstance(value, dict) and token in value:
                children = [(token, value[token])]
            elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
                children = [(token, value[int(token)])]
            else:
                children = []
            step.extend((f"{pointer}/{_escape(str(k))}", v) for k, v in children)
        found = step
    return found


def value_at(document: Any, pointer: str) -> Any:
    value = document
    for token in (_unescape(t) for t in pointer.split("/")[1:]):
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


# --- checks and populations ---------------------------------------------------------


def checks(recipe: Recipe | None) -> dict[str, dict[str, Any]]:
    """Every check by id: the core parse checks, plus the recipe's when one is given."""
    out = {
        cid: {
            "id": cid,
            "stage": None,
            "pattern": None,
            "kind": cid.split(":", 1)[1],
            "question": q,
        }
        for cid, q in PARSE_CHECKS.items()
    }
    for short, stage in recipe.stages.items() if recipe is not None else ():
        for check in stage.checks:
            cid = f"{short}:{check.pattern}"
            out[cid] = {
                "id": cid,
                "stage": short,
                "pattern": check.pattern,
                "kind": None,
                "question": check.question,
            }
    return out


def _resolve(recipe: Recipe | None, check_id: str) -> dict[str, Any]:
    known = checks(recipe)
    if check_id not in known:
        hint = "" if recipe is not None else " (recipe checks need --recipe)"
        raise ReviewError(f"unknown check {check_id!r}{hint}; known: {', '.join(sorted(known))}")
    return known[check_id]


def population(conn: sqlite3.Connection, recipe: Recipe | None, check_id: str) -> list[Instance]:
    """The check's instances, ordered by subject and pointer or page.

    Recipe checks read the stage's current records and refuse an unfinished snapshot or
    stale records: judging outputs the current code no longer produces describes nothing.
    """
    check = _resolve(recipe, check_id)
    if check["kind"] is not None:
        rows = conn.execute(
            "SELECT outcome_id, run_id, stage, subject_key, page, bytes_hash FROM evidence_review"
            " WHERE origin = 'parse' AND kind = ? ORDER BY subject_key, stage, page",
            (check["kind"],),
        ).fetchall()
        return [
            Instance(
                r["outcome_id"],
                r["run_id"],
                r["stage"],
                r["subject_key"],
                None,
                r["page"],
                _hash({"kind": check["kind"], "page": r["page"], "bytes": r["bytes_hash"]}),
            )
            for r in rows
        ]
    assert recipe is not None
    short = check["stage"]
    full = recipe.stage_name(short)
    latest = conn.execute(
        "SELECT id, status, remaining FROM runs WHERE step = ? ORDER BY id DESC LIMIT 1", (full,)
    ).fetchone()
    if latest is None:
        raise ReviewError(f"stage {short} has never run; nothing to sample")
    if latest["status"] == "running" or (latest["remaining"] or 0) > 0:
        raise ReviewError(
            f"stage {short}'s latest run #{latest['id']} did not finish its scope;"
            f" complete it (run --step {short}) before sampling"
        )
    rows = conn.execute(
        "SELECT co.outcome_id, co.run_id, co.subject_key, co.stage_fp, co.version_stale,"
        " co.params_stale, co.input_stale, r.payload"
        " FROM current_outcomes co JOIN records r ON r.id = co.record_id"
        " WHERE co.stage = ? ORDER BY co.subject_key",
        (full,),
    ).fetchall()
    current_fp = recipe.stage_fps[short]
    stale = [
        r
        for r in rows
        if r["stage_fp"] != current_fp
        or r["version_stale"]
        or r["params_stale"]
        or r["input_stale"]
    ]
    if stale:
        raise ReviewError(
            f"{len(stale)} of {len(rows)} current records of {short} are stale (code, params,"
            f" item version, or input changed); rerun --step {short} before sampling"
        )
    out = []
    for r in rows:
        for pointer, value in match(json.loads(r["payload"]), check["pattern"]):
            out.append(
                Instance(
                    r["outcome_id"],
                    r["run_id"],
                    full,
                    r["subject_key"],
                    pointer,
                    None,
                    _hash(value),
                )
            )
    return out


# --- writes (append-only; no workspace lock) -----------------------------------------


def open_for_review(root: Path) -> sqlite3.Connection:
    """A connection for review reads and writes; never creates or migrates a workspace."""
    db_path = root / DB_NAME
    if not db_path.is_file():
        raise WorkspaceError(f"{root} has no {DB_NAME}")
    conn = connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != len(MIGRATIONS):
        conn.close()
        raise WorkspaceError(
            f"{root}: schema version {version}, this code expects {len(MIGRATIONS)};"
            " run a stage once to migrate"
        )
    return conn


def _reviewer(reviewer: str | None) -> str:
    if reviewer is None or not reviewer.strip():
        raise ReviewError("a reviewer is required: pass --reviewer or set COMMISSURE_REVIEWER")
    return reviewer.strip()


def sample(
    conn: sqlite3.Connection,
    recipe: Recipe | None,
    check_id: str,
    n: int,
    *,
    reviewer: str | None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Draw and record a seeded random sample of `n` instances (all of them when n >= size)."""
    who = _reviewer(reviewer)
    if n < 1:
        raise ReviewError("--n must be at least 1")
    check = _resolve(recipe, check_id)
    pool = population(conn, recipe, check_id)
    if not pool:
        raise ReviewError(f"check {check_id} matches no instances; nothing to sample")
    if seed is None:
        seed = random.SystemRandom().randrange(1, 2**31)
    chosen = random.Random(seed).sample(pool, min(n, len(pool)))
    now = utc_now()
    with transaction(conn):
        sample_id = conn.execute(
            "INSERT INTO review_samples (check_id, question, snapshot_run_id, seed, population,"
            " n, reviewer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                check_id,
                check["question"],
                max(i.run_id for i in pool),
                seed,
                len(pool),
                len(chosen),
                who,
                now,
            ),
        ).lastrowid
        _insert_instances(conn, sample_id, check_id, chosen, now)
    return {
        "sample_id": sample_id,
        "check_id": check_id,
        "seed": seed,
        "population": len(pool),
        "n": len(chosen),
        "census": len(chosen) == len(pool),
    }


def _insert_instances(
    conn: sqlite3.Connection, sample_id: int | None, check_id: str, chosen: list[Instance], now: str
) -> list[int]:
    return [
        conn.execute(
            "INSERT INTO review_instances (sample_id, position, check_id, outcome_id, stage,"
            " subject_key, pointer, page, value_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample_id,
                position,
                check_id,
                inst.outcome_id,
                inst.stage,
                inst.subject_key,
                inst.pointer,
                inst.page,
                inst.value_hash,
                now,
            ),
        ).lastrowid
        for position, inst in enumerate(chosen, start=1)
    ]


def spot(
    conn: sqlite3.Connection,
    recipe: Recipe | None,
    check_id: str,
    subject_keys: set[str],
    *,
    pointer: str | None,
    page: int | None,
) -> int:
    """Record an ad hoc instance; its verdicts are reported but never counted in a rate."""
    if (pointer is None) == (page is None):
        raise ReviewError("a spot check names exactly one of --pointer (recipe) or --page (parse)")
    pool = population(conn, recipe, check_id)
    hits = [
        i
        for i in pool
        if i.subject_key in subject_keys
        and i.pointer == pointer
        and (page is None or i.page == page)
    ]
    if not hits:
        raise ReviewError(f"check {check_id} has no instance at that subject and {pointer or page}")
    with transaction(conn):
        [instance_id] = _insert_instances(conn, None, check_id, hits[:1], utc_now())
    return instance_id


def record(
    conn: sqlite3.Connection,
    instance_id: int,
    answer: str,
    *,
    reviewer: str | None,
    note: str | None = None,
) -> int:
    who = _reviewer(reviewer)
    if answer not in ANSWERS:
        raise ReviewError(f"answer must be one of {', '.join(ANSWERS)}; got {answer!r}")
    if (
        conn.execute("SELECT 1 FROM review_instances WHERE id = ?", (instance_id,)).fetchone()
        is None
    ):
        raise ReviewError(f"no review instance {instance_id}")
    with transaction(conn):
        return conn.execute(
            "INSERT INTO verdicts (instance_id, answer, note, reviewer, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (instance_id, answer, note.strip() if note and note.strip() else None, who, utc_now()),
        ).lastrowid


# --- reads -------------------------------------------------------------------------


_LATEST = (
    "SELECT v.* FROM verdicts v WHERE v.id = (SELECT max(v2.id) FROM verdicts v2"
    " WHERE v2.instance_id = v.instance_id)"
)


def pending(conn: sqlite3.Connection, sample_id: int | None = None) -> int | None:
    """The next sampled instance without a verdict: oldest sample first, in drawn order."""
    if (
        sample_id is not None
        and not conn.execute("SELECT 1 FROM review_samples WHERE id = ?", (sample_id,)).fetchone()
    ):
        raise ReviewError(f"no sample {sample_id}")
    row = conn.execute(
        "SELECT i.id FROM review_instances i"
        " WHERE i.sample_id IS NOT NULL AND (? IS NULL OR i.sample_id = ?)"
        " AND NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.instance_id = i.id)"
        " ORDER BY i.sample_id, i.position LIMIT 1",
        (sample_id, sample_id),
    ).fetchone()
    return row[0] if row else None


def describe(
    conn: sqlite3.Connection, root: Path, instance_id: int, recipe: Recipe | None = None
) -> dict[str, Any]:
    """An instance with its question, the judged value, earlier verdicts, and its evidence
    (the `inspect --subject` view of its subject, narrowed to the instance)."""
    row = conn.execute(
        "SELECT i.*, s.question AS sample_question, s.n AS sample_n, r.payload"
        " FROM review_instances i LEFT JOIN review_samples s ON s.id = i.sample_id"
        " JOIN outcomes o ON o.id = i.outcome_id LEFT JOIN records r ON r.id = o.record_id"
        " WHERE i.id = ?",
        (instance_id,),
    ).fetchone()
    if row is None:
        raise ReviewError(f"no review instance {instance_id}")
    declared = checks(recipe).get(row["check_id"])
    question = row["sample_question"] or (
        declared["question"] if declared else "(question unavailable: pass --recipe)"
    )
    keys = {row["subject_key"]}
    blobs = BlobStore(root / BLOBS_DIR)
    items = [_item_detail(conn, blobs, v, texts=False) for v in _subject_item_versions(conn, keys)]
    if row["pointer"] is not None:
        value = value_at(json.loads(row["payload"]), row["pointer"])
        evidence = [
            e
            for e in _subject_evidence(conn, keys, under=row["pointer"])
            if e["stage"] == row["stage"]
        ]
    else:
        value = {"page": row["page"]}
        evidence = [
            e
            for e in _subject_evidence(conn, keys)
            if e["origin"] == "parse" and e["page"] == row["page"] and e["stage"] == row["stage"]
        ]
    earlier = [
        {k: r[k] for k in ("answer", "note", "reviewer", "created_at", "sample_id", "instance_id")}
        for r in _earlier_answers(conn, row)
    ]
    verdict = conn.execute(f"SELECT * FROM ({_LATEST}) WHERE instance_id = ?", (instance_id,))
    current = verdict.fetchone()
    return {
        "instance_id": instance_id,
        "sample_id": row["sample_id"],
        "position": row["position"],
        "sample_n": row["sample_n"],
        "check_id": row["check_id"],
        "question": question,
        "stage": row["stage"],
        "subject_key": row["subject_key"],
        "pointer": row["pointer"],
        "page": row["page"],
        "value": value,
        "verdict": dict(current) if current else None,
        "earlier_verdicts": earlier,
        "evidence": evidence,
        "originals": [
            {
                k: item[k]
                for k in (
                    "key",
                    "item_version_id",
                    "locator",
                    "original_path",
                    "original_status",
                    "blob_path",
                )  # fmt: skip
            }
            | {"pages": [p for p in item["pages"] if row["page"] in (None, p["page"])]}
            for item in items
        ],
    }


def _earlier_answers(conn: sqlite3.Connection, instance: sqlite3.Row) -> list[sqlite3.Row]:
    """Latest verdicts on other instances identical to `instance` (check, subject, value)."""
    return conn.execute(
        f"SELECT v.id AS verdict_id, v.answer, v.note, v.reviewer, v.created_at,"
        f" i.sample_id, i.id AS instance_id"
        f" FROM ({_LATEST}) v JOIN review_instances i ON i.id = v.instance_id"
        " WHERE i.check_id = ? AND i.subject_key = ? AND i.pointer IS ? AND i.page IS ?"
        " AND i.value_hash = ? AND i.id != ? ORDER BY v.id",
        (
            instance["check_id"],
            instance["subject_key"],
            instance["pointer"],
            instance["page"],
            instance["value_hash"],
            instance["id"],
        ),
    ).fetchall()


def sample_instances(conn: sqlite3.Connection, sample_id: int) -> list[dict[str, Any]]:
    """A sample's instances in drawn order, each with its latest verdict and earlier answers."""
    if not conn.execute("SELECT 1 FROM review_samples WHERE id = ?", (sample_id,)).fetchone():
        raise ReviewError(f"no sample {sample_id}")
    out = []
    for inst in conn.execute(
        f"SELECT i.*, v.answer, v.note, v.reviewer AS verdict_reviewer"
        f" FROM review_instances i LEFT JOIN ({_LATEST}) v ON v.instance_id = i.id"
        " WHERE i.sample_id = ? ORDER BY i.position",
        (sample_id,),
    ).fetchall():
        earlier = _earlier_answers(conn, inst)
        out.append(
            {
                "instance_id": inst["id"],
                "position": inst["position"],
                "subject_key": inst["subject_key"],
                "pointer": inst["pointer"],
                "page": inst["page"],
                "answer": inst["answer"],
                "note": inst["note"],
                "earlier_answers": sorted({e["answer"] for e in earlier}),
            }
        )
    return out


def carry(conn: sqlite3.Connection, sample_id: int, *, reviewer: str | None) -> dict[str, int]:
    """Record, as this reviewer's own verdicts, the earlier answers on identical instances.

    This is the reviewer confirming in bulk, so it counts; it is never done implicitly.
    Only pending instances whose earlier verdicts all agree are carried; each verdict's
    note names the verdicts it confirms. Instances with conflicting earlier answers are
    left pending and counted in `conflicting`.
    """
    who = _reviewer(reviewer)
    if not conn.execute("SELECT 1 FROM review_samples WHERE id = ?", (sample_id,)).fetchone():
        raise ReviewError(f"no sample {sample_id}")
    carried = conflicting = 0
    now = utc_now()
    with transaction(conn):
        for inst in conn.execute(
            "SELECT i.* FROM review_instances i WHERE i.sample_id = ?"
            " AND NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.instance_id = i.id)"
            " ORDER BY i.position",
            (sample_id,),
        ).fetchall():
            earlier = _earlier_answers(conn, inst)
            answers = {e["answer"] for e in earlier}
            if len(answers) > 1:
                conflicting += 1
            if len(answers) != 1:
                continue
            ids = ", ".join(str(e["verdict_id"]) for e in earlier)
            conn.execute(
                "INSERT INTO verdicts (instance_id, answer, note, reviewer, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (inst["id"], answers.pop(), f"confirmed earlier verdict(s) {ids}", who, now),
            )
            carried += 1
    return {"carried": carried, "conflicting": conflicting}


def wilson(wrong: int, judged: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for the error rate `wrong / judged`; `None` with nothing judged."""
    if judged <= 0:
        return None
    p = wrong / judged
    denom = 1 + z * z / judged
    centre = (p + z * z / (2 * judged)) / denom
    half = z * math.sqrt(p * (1 - p) / judged + z * z / (4 * judged * judged)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per sample: counts from the latest verdict per instance, rate, Wilson 95% interval.

    The rate is wrong / (correct + wrong); `unclear` and pending instances are reported and
    kept out of it. Spot checks are counted per check, separately, and never rated.
    """
    samples = []
    for s in conn.execute("SELECT * FROM review_samples ORDER BY id").fetchall():
        counts = _answer_counts(conn, "i.sample_id = ?", (s["id"],))
        judged = counts["correct"] + counts["wrong"]
        interval = wilson(counts["wrong"], judged)
        samples.append(
            {
                **dict(s),
                **counts,
                "pending": s["n"] - sum(counts.values()),
                "census": s["n"] == s["population"],
                "rate": counts["wrong"] / judged if judged else None,
                "interval_95": list(interval) if interval else None,
            }
        )
    spot_checks = []
    for (check_id,) in conn.execute(
        "SELECT DISTINCT check_id FROM review_instances WHERE sample_id IS NULL ORDER BY check_id"
    ).fetchall():
        counts = _answer_counts(conn, "i.sample_id IS NULL AND i.check_id = ?", (check_id,))
        spot_checks.append({"check_id": check_id, **counts})
    return {"samples": samples, "spot_checks": spot_checks}


def client_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """The review snapshot a deliverable may publish: report fields, reviewers, and lines.

    Frozen at plan time into a stage's input fingerprint when `reads_review=True`. Notes
    stay out; they are not published. An empty review has no lines and invents no rate.
    """
    rep = report(conn)
    samples = []
    for s in rep["samples"]:
        reviewers = [
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT v.reviewer FROM ({_LATEST}) v"
                " JOIN review_instances i ON i.id = v.instance_id"
                " WHERE i.sample_id = ? ORDER BY v.reviewer",
                (s["id"],),
            )
        ]
        reviewed_at = conn.execute(
            f"SELECT max(v.created_at) FROM ({_LATEST}) v"
            " JOIN review_instances i ON i.id = v.instance_id WHERE i.sample_id = ?",
            (s["id"],),
        ).fetchone()[0]
        samples.append({**s, "reviewers": reviewers, "reviewed_at": reviewed_at})
    spot_checks = rep["spot_checks"]
    lines = [_sample_line(s) for s in samples] + [_spot_line(s) for s in spot_checks]
    return {"samples": samples, "spot_checks": spot_checks, "lines": lines}


def _sample_line(s: dict[str, Any]) -> str:
    if s["n"] == s["population"]:
        draw = (
            f"all {s['population']} instances of {s['check_id']} from run"
            f" {s['snapshot_run_id']} (seed {s['seed']})"
        )
    else:
        draw = (
            f"{s['n']} of {s['population']} instances of {s['check_id']} sampled at"
            f" random from run {s['snapshot_run_id']} (seed {s['seed']})"
        )
    parts = [draw, s["question"], f"drawn by {s['reviewer']} on {s['created_at']}"]
    judged = s["correct"] + s["wrong"]
    if judged > 0:
        lo, hi = s["interval_95"]
        parts.append(
            f"{s['wrong']} wrong of {judged} ({s['rate']:.1%}, 95% CI {lo:.1%} to {hi:.1%})"
        )
    else:
        parts.append("none judged")
    if s["unclear"]:
        parts.append(f"{s['unclear']} unclear")
    if s["pending"]:
        parts.append(f"{s['pending']} pending")
    if s["reviewers"]:
        names = ", ".join(s["reviewers"])
        parts.append(f"verdicts by {names} on {s['reviewed_at']}")
    return "; ".join(parts)


def _spot_line(s: dict[str, Any]) -> str:
    return (
        f"spot checks of {s['check_id']}: {s['correct']} correct, {s['wrong']} wrong,"
        f" {s['unclear']} unclear (not rated)"
    )


def _answer_counts(conn: sqlite3.Connection, where: str, params: tuple) -> dict[str, int]:
    counts = dict.fromkeys(ANSWERS, 0)
    for answer, n in conn.execute(
        f"SELECT v.answer, count(*) FROM ({_LATEST}) v"
        f" JOIN review_instances i ON i.id = v.instance_id WHERE {where} GROUP BY v.answer",
        params,
    ):
        counts[answer] = n
    return counts


# --- terminal rendering ---------------------------------------------------------------


def _line(console: Console, text: str) -> None:
    # soft_wrap: a path or link broken across lines can no longer be clicked or copied.
    console.print(text, soft_wrap=True, highlight=False)


def render_checks(console: Console, rows: list[dict[str, Any]]) -> None:
    table = Table("check", "population", "question", title="Checks", title_justify="left")
    for r in rows:
        pop = "-" if r["population"] is None else str(r["population"])
        table.add_row(escape(r["id"]), escape(pop), escape(r["question"]))
    console.print(table)
    for r in rows:
        if r.get("unavailable"):
            _line(console, f"{escape(r['id'])}: {escape(r['unavailable'])}")


def render_instance(console: Console, d: dict[str, Any]) -> None:
    where = (
        f"sample {d['sample_id']}, {d['position']} of {d['sample_n']}"
        if d["sample_id"] is not None
        else "spot check, not counted in any rate"
    )
    _line(console, f"[bold]instance {d['instance_id']}[/bold] ({where})  {escape(d['check_id'])}")
    _line(console, f"  question  {escape(d['question'])}")
    target = d["pointer"] if d["pointer"] is not None else f"page {d['page']}"
    _line(console, f"  subject   {escape(d['subject_key'])}  {escape(target)}")
    _line(console, f"  value     {escape(json.dumps(d['value'], ensure_ascii=False))}")
    for e in d["earlier_verdicts"]:
        note = f": {e['note']}" if e["note"] else ""
        _line(
            console,
            f"  earlier   {escape(e['answer'])} by {escape(e['reviewer'])} on"
            f" {escape(e['created_at'])} (instance {e['instance_id']}){escape(note)}"
            + escape("  [shown only; it does not count here]"),
        )
    for e in d["evidence"]:
        page = f" p. {e['page']}" if e["page"] else ""
        trust = f"  [ocr, confidence {e['confidence']:.1f}]" if e["via_ocr"] else ""
        _line(console, f"  evidence  {escape(e['locator'])}{page}{escape(trust)}")
        if e["span_text"] is not None:
            before = " ".join((e["context_before"] or "").split())
            after = " ".join((e["context_after"] or "").split())
            span = " ".join(e["span_text"].split())
            _line(
                console, f"    ...{escape(before)} [bold]{escape(span)}[/bold] {escape(after)}..."
            )
        if e["url"]:
            _line(console, f"    {escape(e['url'])}")
    for o in d["originals"]:
        status = "" if o["original_status"] == "present" else f"  [{o['original_status']}]"
        _line(console, f"  original  {escape(o['original_path'] or o['locator'])}{escape(status)}")
        for p in o["pages"]:
            if p["url"]:
                _line(console, f"    {escape(p['url'])}")
    if d["verdict"]:
        v = d["verdict"]
        _line(console, f"  verdict   {escape(v['answer'])} by {escape(v['reviewer'])}")
    else:
        _line(
            console,
            f"  record    commissure review record {d['instance_id']} correct|wrong|unclear"
            + escape(" [--note ...]"),
        )


def render_report(console: Console, rep: dict[str, Any]) -> None:
    table = Table(
        "sample", "check", "run", "seed", "pop", "n", "correct", "wrong", "unclear", "pending",
        "rate", "95% interval", title="Samples", title_justify="left",
    )  # fmt: skip
    for s in rep["samples"]:
        rate = "-" if s["rate"] is None else f"{s['rate']:.1%}"
        ci = s["interval_95"]
        interval = "-" if ci is None else f"{ci[0]:.1%} to {ci[1]:.1%}"
        table.add_row(
            *(
                escape(str(x))
                for x in (
                    s["id"], s["check_id"], s["snapshot_run_id"], s["seed"], s["population"],
                    s["n"], s["correct"], s["wrong"], s["unclear"], s["pending"], rate, interval,
                )
            )
        )  # fmt: skip
    console.print(table)
    if rep["spot_checks"]:
        spot = Table(
            "check", "correct", "wrong", "unclear", title="Spot checks (not rated)",
            title_justify="left",
        )  # fmt: skip
        for s in rep["spot_checks"]:
            spot.add_row(*(escape(str(s[k])) for k in ("check_id", *ANSWERS)))
        console.print(spot)


def iter_checks_with_population(
    conn: sqlite3.Connection, recipe: Recipe | None
) -> Iterator[dict[str, Any]]:
    for cid, check in checks(recipe).items():
        try:
            size = len(population(conn, recipe, cid))
            yield {**check, "population": size, "unavailable": None}
        except ReviewError as exc:
            yield {**check, "population": None, "unavailable": str(exc)}
