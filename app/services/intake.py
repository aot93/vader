"""Intake classification — turn a source directory tree into a list of
write units, one per framework-doc content type (§3.3).

The classifier is deliberately rule-driven (:class:`IntakeRules`) rather than
magic, so the annual operator can see and adjust exactly how files are being
bucketed. Nothing is ever silently dropped: anything that matches no specific
rule is catalogued as a Type-C file with ``source_system_tag="unclassified"``.

Type A (EXR sequences)
    A directory holding files that match the frame pattern becomes one
    :class:`SequenceUnit` per distinct frame *base name*. Individual frames are
    never emitted as their own units — that is the two-tier model from §3.3.

Type B (video chunks)
    Files matching the chunk pattern are grouped by base name into one logical
    video; each chunk becomes a :class:`FileUnit` carrying ``total_chunks`` so a
    video split across tapes stays visible (§3.3 Type B).

Types C / D (config, audio/media)
    Plain file-level units.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from app.models import BackupCategory, ContentType


@dataclass
class IntakeRules:
    frame_pattern: str = r"^(?P<base>.+?)[._](?P<frame>\d{2,10})\.exr(?:\.zip)?$"
    chunk_pattern: str = r"^(?P<base>.+?)[._](?P<idx>\d{1,6})\.(?:mov|mxf|avi|dpx|r3d|raw|mkv|yuv|ari)$"
    config_exts: frozenset[str] = frozenset(
        {".cfg", ".conf", ".ini", ".json", ".yaml", ".yml", ".xml", ".toml",
         ".log", ".txt", ".plist", ".csv", ".sh", ".reg"}
    )
    audio_media_exts: frozenset[str] = frozenset(
        {".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac", ".ogg",
         ".mov", ".mp4", ".mxf", ".m2ts", ".mkv", ".wmv", ".avi"}
    )
    config_dir_hints: tuple[str, ...] = ("config", "configs", "settings", "logs", "log")


@dataclass
class FrameFile:
    path: Path
    filename: str
    frame: int | None
    size: int


@dataclass
class SequenceUnit:
    kind: str = field(default="sequence", init=False)
    project_name: str = ""
    sequence_name: str | None = None
    shot_name: str | None = None
    source_path: str = ""
    source_machine: str | None = None
    backup_category: BackupCategory = BackupCategory.project_archive
    frames: list[FrameFile] = field(default_factory=list)

    @property
    def sorted_frames(self) -> list[FrameFile]:
        return sorted(self.frames, key=lambda f: (f.frame if f.frame is not None else -1, f.filename))

    @property
    def frame_numbers(self) -> list[int]:
        return [f.frame for f in self.frames if f.frame is not None]

    @property
    def frame_start(self) -> int | None:
        nums = self.frame_numbers
        return min(nums) if nums else None

    @property
    def frame_end(self) -> int | None:
        nums = self.frame_numbers
        return max(nums) if nums else None

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def total_size_bytes(self) -> int:
        return sum(f.size for f in self.frames)

    def gap_report(self) -> tuple[bool, str | None]:
        nums = sorted(set(self.frame_numbers))
        if len(nums) < 2:
            return False, None
        expected = set(range(nums[0], nums[-1] + 1))
        missing = sorted(expected - set(nums))
        if not missing:
            return False, None
        return True, _compress_ranges(missing)


@dataclass
class FileUnit:
    kind: str = field(default="item", init=False)
    content_type: ContentType = ContentType.config
    path: Path | None = None
    source_path: str = ""
    filename: str = ""
    size: int = 0
    project_name: str | None = None
    video_name: str | None = None
    chunk_index: int | None = None
    total_chunks: int | None = None
    source_machine: str | None = None
    source_system_tag: str | None = None
    backup_category: BackupCategory = BackupCategory.project_archive


def _compress_ranges(nums: list[int]) -> str:
    parts: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _rel_parts(path: Path, root: Path) -> list[str]:
    try:
        return list(path.relative_to(root).parts)
    except ValueError:
        return list(path.parts)


def scan_source(
    root: Path,
    *,
    rules: IntakeRules | None = None,
    project_name: str | None = None,
    source_machine: str | None = None,
    backup_category: BackupCategory = BackupCategory.project_archive,
) -> list[SequenceUnit | FileUnit]:
    """Walk ``root`` and return the ordered list of write units."""
    rules = rules or IntakeRules()
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"source path is not a directory: {root}")

    frame_re = re.compile(rules.frame_pattern, re.I)
    chunk_re = re.compile(rules.chunk_pattern, re.I)

    units: list[SequenceUnit | FileUnit] = []

    for dirpath, dirnames, filenames in _walk_sorted(root):
        d = Path(dirpath)
        if d.name == "_manifests" or d.name == "_catalog":
            dirnames[:] = []
            continue

        frame_groups: dict[str, list[FrameFile]] = defaultdict(list)
        leftovers: list[str] = []
        for name in sorted(filenames):
            fpath = d / name
            if not fpath.is_file() or fpath.is_symlink():
                continue
            m = frame_re.match(name)
            if m:
                frame_groups[m.group("base")].append(
                    FrameFile(path=fpath, filename=name,
                              frame=int(m.group("frame")), size=fpath.stat().st_size)
                )
            else:
                leftovers.append(name)

        # Path convention: <root>/<project>/<sequence>/<shot>/frames…, but the
        # operator may point the job straight at a project dir, so root itself
        # counts as the first component.
        parts = [root.name, *_rel_parts(d, root)]
        derived_project = project_name or parts[0]
        if len(parts) > 3:
            seq_name = parts[1]
            shot_name = parts[2]
        elif len(parts) == 3:
            seq_name, shot_name = parts[1], parts[2]
        elif len(parts) == 2:
            seq_name, shot_name = None, parts[1]
        else:
            seq_name, shot_name = None, None

        for base, frames in frame_groups.items():
            units.append(SequenceUnit(
                project_name=derived_project,
                sequence_name=seq_name,
                shot_name=shot_name or base,
                source_path=str(d),
                source_machine=source_machine,
                backup_category=backup_category,
                frames=frames,
            ))

        # video chunks in this dir, grouped by base name
        chunk_groups: dict[str, list[tuple[int, Path]]] = defaultdict(list)
        for name in list(leftovers):
            m = chunk_re.match(name)
            if m:
                chunk_groups[m.group("base")].append((int(m.group("idx")), d / name))
                leftovers.remove(name)
        for base, chunks in chunk_groups.items():
            chunks.sort()
            total = len(chunks)
            for idx, cpath in chunks:
                units.append(FileUnit(
                    content_type=ContentType.video_chunk,
                    path=cpath, source_path=str(cpath), filename=cpath.name,
                    size=cpath.stat().st_size,
                    project_name=derived_project, video_name=base,
                    chunk_index=idx, total_chunks=total,
                    source_machine=source_machine, backup_category=backup_category,
                ))

        # everything else: Type C / D file-level
        lowered_parts = {p.lower() for p in parts}
        dir_is_config = bool(lowered_parts & set(rules.config_dir_hints))
        for name in leftovers:
            fpath = d / name
            if not fpath.is_file() or fpath.is_symlink():
                continue
            ext = fpath.suffix.lower()
            if ext in rules.audio_media_exts and not dir_is_config:
                ctype, tag = ContentType.audio_media, None
            elif ext in rules.config_exts or dir_is_config:
                ctype, tag = ContentType.config, (parts[0] if parts else None)
            else:
                ctype, tag = ContentType.config, "unclassified"
            units.append(FileUnit(
                content_type=ctype, path=fpath, source_path=str(fpath),
                filename=name, size=fpath.stat().st_size,
                project_name=derived_project, source_machine=source_machine,
                source_system_tag=tag, backup_category=backup_category,
            ))

    return units


def _walk_sorted(root: Path):
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        yield dirpath, dirnames, filenames
