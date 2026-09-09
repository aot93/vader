"""CSV export (framework doc §3.6 + §8).

Two shapes:

* :func:`tape_catalog_csv` — full content listing for one tape. Doubles as the
  text file written onto the tape itself, and as a physical-insert label source.
* :func:`full_catalog_csv` — every content row across every tape. Generated
  automatically at the end of an annual run and stored with the DB backup; if
  the database is ever lost this plain-text file still says what is on which
  tape, no app required.

Checksums are always included so the CSV is itself an integrity manifest. For
Type-A rows the ``manifest_ref`` column points at the frame-level manifest so
the export is a complete integrity record, not just the folder summary.
"""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContentItem, ContentTapeSpan, SequenceContainer, Tape

_COLUMNS = [
    "tape_barcode", "tape_status", "content_kind", "content_type",
    "project_name", "sequence_name", "shot_name", "video_name",
    "chunk_index", "total_chunks", "backup_category", "source_machine",
    "source_path", "ltfs_path", "part_index", "part_count",
    "byte_range_start", "byte_range_end", "size_bytes",
    "sha256", "manifest_ref", "written_at", "verified_at",
]


def _iso(dt: datetime | None) -> str:
    return dt.astimezone(UTC).isoformat() if dt else ""


def _rows_for_spans(db: Session, spans: list[ContentTapeSpan]):
    for span in spans:
        tape = db.get(Tape, span.tape_id)
        base = dict.fromkeys(_COLUMNS, "")
        base.update(
            tape_barcode=tape.barcode if tape else "",
            tape_status=tape.status.value if tape else "",
            ltfs_path=span.ltfs_path,
            part_index=span.part_index,
            part_count=span.part_count,
            byte_range_start=span.byte_range_start if span.byte_range_start is not None else "",
            byte_range_end=span.byte_range_end if span.byte_range_end is not None else "",
            size_bytes=span.size_bytes,
            sha256=span.sha256 or "",
            written_at=_iso(span.written_at),
            verified_at=_iso(span.verified_at),
        )
        if span.sequence_container_id:
            c: SequenceContainer = db.get(SequenceContainer, span.sequence_container_id)
            base.update(
                content_kind="sequence_container", content_type="exr_sequence",
                project_name=c.project_name, sequence_name=c.sequence_name or "",
                shot_name=c.shot_name or "", backup_category=c.backup_category.value,
                source_machine=c.source_machine or "", source_path=c.source_path,
                manifest_ref=c.manifest_ref or "",
            )
        else:
            it: ContentItem = db.get(ContentItem, span.content_item_id)
            base.update(
                content_kind="content_item", content_type=it.content_type.value,
                project_name=it.project_name or "", video_name=it.video_name or "",
                chunk_index=it.chunk_index if it.chunk_index is not None else "",
                total_chunks=it.total_chunks if it.total_chunks is not None else "",
                backup_category=it.backup_category.value,
                source_machine=it.source_machine or "", source_path=it.source_path,
            )
        yield base


def _render(rows) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def tape_catalog_csv(db: Session, barcode: str) -> str:
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode))
    if not tape:
        raise ValueError(f"unknown tape {barcode}")
    spans = db.scalars(
        select(ContentTapeSpan).where(ContentTapeSpan.tape_id == tape.id)
        .order_by(ContentTapeSpan.ltfs_path, ContentTapeSpan.part_index)
    ).all()
    return _render(_rows_for_spans(db, spans))


def full_catalog_csv(db: Session) -> str:
    spans = db.scalars(
        select(ContentTapeSpan).join(Tape, Tape.id == ContentTapeSpan.tape_id)
        .order_by(Tape.barcode, ContentTapeSpan.ltfs_path, ContentTapeSpan.part_index)
    ).all()
    return _render(_rows_for_spans(db, spans))


def write_full_catalog_export(db: Session) -> str:
    """Write the full-catalog CSV into ``$DATA_DIR/exports`` and return the path."""
    from app.config import get_settings

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = get_settings().data_dir / "exports" / f"catalog-{stamp}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full_catalog_csv(db))
    return str(path)
