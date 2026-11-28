# __SOLUTION_NAME__: intake

Input documents for this solution. They stay on this machine: `intake/*` is gitignored except this README. A solution that must move its corpus through git says so in its `.gitignore`.

- One subfolder per source you will register, for example `intake/<batch>/`, so that `commissure run --step ingest --source intake/<batch> --role <role>` scopes one cycle.
- Originals are read-only: commissure copies bytes into `workspace/blobs/` and never writes here.
- Run `uv run commissure probe intake/<batch> --json` before writing extraction code and record what it finds in `recipe/AGENTS.md` (Inputs).
