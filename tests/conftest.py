import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commissure.cli import app
from commissure.workspace import Workspace, open_workspace

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A writable copy of the fixture folder; the checked-in fixture is never touched."""
    dest = tmp_path / "source"
    shutil.copytree(FIXTURES / "folder_source", dest)
    return dest


@pytest.fixture
def ws_root(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def ws(ws_root: Path) -> Workspace:
    workspace = open_workspace(ws_root)
    yield workspace
    workspace.close()


class Cli:
    def __init__(self) -> None:
        self.runner = CliRunner()

    def __call__(self, *args: str, env: dict[str, str] | None = None):
        return self.runner.invoke(app, list(args), env=env)

    def ingest(self, ws_root: Path, source: Path, *extra: str) -> tuple[int, dict]:
        result = self(
            "run", "--step", "ingest", "--workspace", str(ws_root),
            "--source", str(source), "--json", *extra,
        )  # fmt: skip
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        return result.exit_code, payload


@pytest.fixture
def cli() -> Cli:
    return Cli()
