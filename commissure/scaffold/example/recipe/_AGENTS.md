# Recipe: `__SOLUTION_NAME__` (example)

A worked example of a finished contract: what `investigate-solution` writes for a real solution, and what the operator approves before stage code exists. This file adds to core's rules (`../.agents/commissure/AGENTS.md`) and never overrides them.

## Purpose

Read the public comments on Proposal 7 (evening and Sunday hours at the Alder Creek Library, a fictional proposal) and report how many support or oppose it and which topics they raise. Every count rests on quotes a person can check against the original comment. Outputs: the `tally` record, shown by the interface (`uv run commissure serve`).

## Inputs

One source, `intake/comments/`, role `comment`: 23 files, every one on the comment form (its first line is the form's header).

- 18 web-form submissions (`.html`, one `<main>` each), 2 letters typed in Word (`.docx`), 2 PDFs with a text layer (`c21.pdf` has two pages), and 1 scanned letter with no text layer (`c23-scan.pdf`).
- `commissure probe intake/comments` reports one unreadable page: the scan. OCR (`--parser ocr`, needs Tesseract) is not used, so the scan is set aside.

## Domain rules

- The form's header names "evening" and "Sunday": nothing is read from it, only from the text after it (motivated by every input).
- **Position:** the first of these phrases after the header, case-insensitive. Support: "I support", "I am in favor", "Please approve". Oppose: "I oppose", "I am against", "Please reject". The phrase itself is the evidence (`kind="position_phrase"`).
- **Topics:** for each topic, the first word after the header that names it, as evidence (`kind="topic"`): evenings ("evening", "night"), sundays ("Sunday", "weekend"), cost ("levy", "tax", "cost"), staffing ("staff"), students ("student", "homework"), parking ("parking").
- A conditional ("I support longer hours, but only if parking ... is fixed", `c05`) counts as its stated phrase: support. A reviewer who disagrees records a verdict; the rule changes only on the operator's decision.

## Quarantine rules

| Condition | Outcome | Reason code | Test |
| --- | --- | --- | --- |
| Not on the comment form (header missing) | quarantined | `missing_form_header` | none in the corpus; add one with the first such input |
| Both a support and an oppose phrase (`c10`) | quarantined | `conflicting_position` | `tests/test_recipe.py::test_outcomes_match_the_acceptance_gate` |
| No position phrase (`c09`) | warning, `position` is null | `no_position_phrase` | same |
| No text layer (the scan) | quarantined at parse, skipped here | core's readability gate | same |

## Acceptance gate

`parse`: 22 valid, 1 quarantined. `extract`: 20 valid, 1 warning, 1 quarantined, 1 skipped. `tally`: 21 comments, 12 support, 8 oppose, 1 without a position, and the two set aside named. Core's defaults apply (a run with no successes fails).

## Definition of done

`uv run pytest` passes; the interface shows the tally and every comment's quotes; a seeded sample of `/position_evidence` reviewed at `/review` finds no `wrong` verdicts.
