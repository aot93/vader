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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
