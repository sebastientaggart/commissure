import shutil
from pathlib import Path

import pytest

from commissure.testing import Cli
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


@pytest.fixture
def cli() -> Cli:
    return Cli()
