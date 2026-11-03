"""The workspace lockfile refuses a concurrent ingest from another process."""

import subprocess
import sys
import textwrap

import pytest

from commissure.workspace import WorkspaceLocked, lock, open_workspace

HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from commissure.workspace import lock

    with lock(Path(sys.argv[1])):
        print("locked", flush=True)
        sys.stdin.readline()
    """
)


@pytest.fixture
def held_lock(ws_root):
    """Another OS process holding the workspace lock until the test ends."""
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(ws_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        yield holder
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)


def test_concurrent_ingest_is_refused(cli, ws_root, source, held_lock):
    result = cli("run", "--step", "ingest", "--workspace", str(ws_root), "--source", str(source))
    assert result.exit_code == 3
    assert "locked by another process" in result.stderr
    assert f"pid={held_lock.pid}" in result.stderr


def test_refused_ingest_writes_nothing(cli, ws_root, source, held_lock):
    cli.ingest(ws_root, source)
    held_lock.stdin.close()
    held_lock.wait(timeout=10)
    ws = open_workspace(ws_root)
    assert ws.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    ws.close()


def test_lock_is_released_after_use(ws_root):
    with lock(ws_root):
        pass
    with lock(ws_root), pytest.raises(WorkspaceLocked), lock(ws_root):
        pass
