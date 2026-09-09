"""Frame-level manifest handling for Type-A sequences (framework doc §3.3).

A manifest is a single JSON file listing every frame filename + its SHA256. It
is stored:

* in the app data dir  (``$DATA_DIR/manifests/...``) — referenced by
  ``sequence_containers.manifest_ref`` and used for integrity / CSV export;
* on the tape itself, but under a **parallel** ``_manifests/`` path on the LTFS
  volume — never inside the real shot folder, so a restore that reproduces the
  shot directory exactly cannot pick a manifest up by accident (§3.3, §3.5).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.services.checksums import sha256_text


@dataclass
class FrameChecksum:
    filename: str
    frame: int | None
    size: int
    sha256: str


def build_manifest_document(
    *,
    project_name: str,
    sequence_name: str | None,
    shot_name: str | None,
    source_path: str,
    ltfs_dir: str,
    frames: list[FrameChecksum],
) -> dict:
    return {
        "manifest_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "project_name": project_name,
        "sequence_name": sequence_name,
        "shot_name": shot_name,
        "source_path": source_path,
        "ltfs_dir": ltfs_dir,
        "frame_count": len(frames),
        "frames": [
            {"filename": f.filename, "frame": f.frame, "size": f.size, "sha256": f.sha256}
            for f in frames
        ],
    }


def manifest_relpath(project_name: str, sequence_name: str | None, shot_name: str | None) -> str:
    """LTFS-relative path for the sidecar manifest — always under ``_manifests/``."""
    parts = ["_manifests", _safe(project_name)]
    if sequence_name:
        parts.append(_safe(sequence_name))
    parts.append(f"{_safe(shot_name or 'shot')}.json")
    return "/".join(parts)


def write_manifest(dest: Path, document: dict) -> str:
    """Write ``document`` to ``dest`` (creating parents) and return its SHA256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, sort_keys=True)
    dest.write_text(text)
    return sha256_text(text)


def load_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def merge_part_manifests(part_paths: list[Path], *, base: dict) -> tuple[dict, str]:
    """Combine per-tape part manifests into one whole-folder manifest.

    Returns ``(document, sha256)``. Frames are de-duplicated by filename and
    ordered by frame number so the merged manifest matches the original folder.
    """
    frames: dict[str, dict] = {}
    for p in part_paths:
        for fr in load_manifest(p).get("frames", []):
            frames[fr["filename"]] = fr
    ordered = sorted(
        frames.values(),
        key=lambda f: (f.get("frame") if f.get("frame") is not None else -1, f["filename"]),
    )
    doc = {
        "manifest_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "project_name": base.get("project_name"),
        "sequence_name": base.get("sequence_name"),
        "shot_name": base.get("shot_name"),
        "source_path": base.get("source_path"),
        "ltfs_dir": base.get("ltfs_dir"),
        "frame_count": len(ordered),
        "spanned_parts": len(part_paths),
        "frames": ordered,
    }
    return doc, sha256_text(json.dumps(doc, indent=2, sort_keys=True))


def _safe(name: str) -> str:
    keep = "-_.() "
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in (name or "")).strip()
    return cleaned or "unnamed"
