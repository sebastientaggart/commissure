# __SOLUTION_NAME__: a Commissure example

A complete, working solution over a small fictional corpus: 23 public comments on Proposal 7, evening and Sunday hours at the Alder Creek Library. No such town or proposal exists. It reads each comment's position and topics, points every result at the exact words it rests on, sets aside what it can't read or decide, and lets a reviewer grade a random sample against the originals. Created __DATE__ by `commissure new --example`.

Everything runs offline, with no model: the extraction is deterministic, which is the right first choice whenever the input is this regular.

## Run it

```sh
uv sync
uv run commissure run --step ingest --source intake/comments --role comment
uv run commissure run --step parse
uv run commissure run --step extract
uv run commissure run --step tally
uv run commissure serve          # open the printed address; /review grades a sample
```

Then look closer:

```sh
uv run commissure inspect --only-errors      # what was set aside, and why
uv run commissure inspect --subject c21.pdf  # one comment: text, outcome, evidence (page 2)
uv run commissure review checks              # the questions a reviewer answers
uv run pytest                                # the acceptance gate in recipe/AGENTS.md
```

## What to read

- `recipe/AGENTS.md`: the contract an agent writes before any code (inputs, rules, quarantine, gate).
- `recipe/extract.py`: one item stage returning quotes as `Evidence` spans that core verifies.
- `recipe/tally.py` and `recipe/interface.py`: a run stage and the page `serve` shows.
- `AGENTS.md` and `.agents/commissure/`: what a coding agent reads in every solution.

To start your own: `uvx commissure new <dir>`.
