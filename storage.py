"""Pure file/format helpers extracted from app.py (Phase 4 §8.4).

These have no dependency on app.py's monkeypatched globals (MEETINGS_DIR,
SETTINGS_PATH, the `meetings` dict) — they operate purely on their arguments —
so app.py re-imports them with no import cycle. The stateful index/settings
helpers (_meeting_dir, _save_index, _load_index, load_settings, save_settings)
deliberately STAY in app.py because they read those monkeypatched globals.

_validate_artifact_id is covered by tests/test_security.py (via app._validate_artifact_id).
"""

import re
from pathlib import Path

from fastapi import HTTPException

_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_artifact_id(value: str, kind: str = "id") -> None:
    """Reject ids that could escape a directory (path traversal) or are malformed."""
    if not isinstance(value, str) or not _ARTIFACT_ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {kind}")


def _atomic_write(path: Path, content: str):
    """Write content to a file atomically via a temp file + rename."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _srt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def _generate_srt(segments: list[dict]) -> str:
    """Generate SRT subtitle content from segments."""
    lines = []
    for i, seg in enumerate(segments, 1):
        start = _srt_ts(seg["start"])
        end = _srt_ts(seg["end"])
        speaker = seg.get("speaker", "")
        prefix = f"[{speaker}] " if speaker and speaker != "UNKNOWN" else ""
        lines.append(f"{i}\n{start} --> {end}\n{prefix}{seg['text']}\n")
    return "\n".join(lines)
