"""SHA256 helpers. The checksum recorded here, at write time, before the tape
is ejected, is the only reliable guard against silent tape corruption found
years later (framework doc §3.3 "Common to all four types")."""
from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 4 * 1024 * 1024


def sha256_file(path: Path, *, offset: int = 0, length: int | None = None) -> str:
    """Hash a file, or a byte-range slice of it (used for tape-split parts)."""
    h = hashlib.sha256()
    remaining = length
    with open(path, "rb") as fh:
        if offset:
            fh.seek(offset)
        while True:
            want = _CHUNK if remaining is None else min(_CHUNK, remaining)
            if want <= 0:
                break
            block = fh.read(want)
            if not block:
                break
            h.update(block)
            if remaining is not None:
                remaining -= len(block)
    return h.hexdigest()


def copy_and_hash(src: Path, dst: Path, *, offset: int = 0, length: int | None = None) -> str:
    """Stream ``src`` (or a byte-range slice of it) to ``dst`` in a single
    pass, hashing it as it goes -- one read of ``src``, one write to ``dst``,
    no read-back of ``dst`` needed to know what was written.

    This is the write path's default: it skips the tape read-back entirely,
    which on real hardware is the expensive part (forces the drive to flip
    between write and read mode). The tradeoff is real -- the recorded
    checksum then only proves what came off the source, not what physically
    landed on tape, so it can't catch tape-side corruption on its own. Write
    jobs that want that guarantee should turn on read-back verification
    (which re-reads ``dst`` with :func:`sha256_file` and compares), or rely on
    a follow-up verify job."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    remaining = length
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        if offset:
            fi.seek(offset)
        while True:
            want = _CHUNK if remaining is None else min(_CHUNK, remaining)
            if want <= 0:
                break
            block = fi.read(want)
            if not block:
                break
            fo.write(block)
            h.update(block)
            if remaining is not None:
                remaining -= len(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
