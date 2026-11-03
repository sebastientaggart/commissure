import stat

import pytest

from commissure.workspace import hash_file


def test_put_file_stores_read_only_blob_at_sharded_path(ws, tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"some bytes")
    digest = hash_file(src)
    dest = ws.blobs.put_file(src, digest)
    assert dest == ws.blobs.root / digest[:2] / digest[2:4] / digest
    assert dest.read_bytes() == b"some bytes"
    assert not dest.stat().st_mode & stat.S_IWUSR


def test_put_file_refuses_bytes_that_do_not_match_digest(ws, tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"original")
    digest = hash_file(src)
    src.write_bytes(b"changed after hashing")
    with pytest.raises(ValueError, match="changed while being stored"):
        ws.blobs.put_file(src, digest)
    assert not ws.blobs.has(digest)
    assert list(ws.blobs.root.rglob(".tmp-*")) == []
