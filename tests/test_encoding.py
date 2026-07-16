"""Regression test for the UTF-8 read/write encoding pin (fast-follow to
storage.py::_atomic_write).

_atomic_write always writes UTF-8 explicitly. On Windows dev hosts the
platform's default locale encoding is cp1252, so a bare `.read_text()` (no
encoding=) silently mis-decodes non-ASCII content instead of raising —
producing mojibake (e.g. an arrow "→" written as UTF-8 comes back as
"â†’" when read with cp1252) rather than a loud failure. Pinning
encoding="utf-8" on both the write and the read closes that gap.

See also: notes_store.py, which has pinned both sides of every read/write
pair from the start; app.py's ~50 read_text()/write_text() call sites were
swept to match as part of this fast-follow.
"""

from storage import _atomic_write


def test_atomic_write_read_roundtrip_non_ascii(tmp_path):
    p = tmp_path / "x.md"
    _atomic_write(p, "arrow → check — em-dash …")
    assert p.read_text(encoding="utf-8") == "arrow → check — em-dash …"
