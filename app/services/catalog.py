"""Catalog queries — tape CRUD helpers and the unified content search that
powers §3.5 (search by project/shot, video, source machine, date range, backup
category or tape barcode)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.hardware import get_hardware
from app.models import (
    ContentItem,
    ContentTapeSpan,
    SequenceContainer,
    Tape,
)


@dataclass
class SearchFilters:
    q: str | None = None
    project: str | None = None
    source_machine: str | None = None
    backup_category: str | None = None
    content_type: str | None = None  # exr_sequence | video_chunk | config | audio_media
    tape_barcode: str | None = None
    written_from: datetime | None = None
    written_to: datetime | None = None


@dataclass
class TapeRef:
    barcode: str
    status: str
    slot: int | None
    location: str
    ltfs_path: str
    part_index: int
    part_count: int


@dataclass
class SearchHit:
    kind: str  # "sequence" | "item"
    id: int
    title: str
    project: str | None
    subtitle: str
    content_type: str
    backup_category: str
    source_machine: str | None
    source_path: str
    size_bytes: int
    written_at: datetime | None
    verified_at: datetime | None
    tapes: list[TapeRef] = field(default_factory=list)


def _slot_index(db: Session) -> dict[str, int | None]:
    try:
        state = get_hardware().library_status()
    except Exception:  # noqa: BLE001
        return {}
    idx: dict[str, int | None] = {}
    for s in state.slots:
        if s.barcode:
            idx[s.barcode] = s.number
    return idx


def _tape_refs(db: Session, spans: list[ContentTapeSpan], slots: dict[str, int | None]) -> list[TapeRef]:
    refs: list[TapeRef] = []
    for span in sorted(spans, key=lambda s: (s.part_index, s.ltfs_path)):
        tape = db.get(Tape, span.tape_id)
        if not tape:
            continue
        refs.append(TapeRef(
            barcode=tape.barcode, status=tape.status.value,
            slot=slots.get(tape.barcode),
            location=tape.physical_location or ("in library" if tape.barcode in slots else "unknown"),
            ltfs_path=span.ltfs_path, part_index=span.part_index, part_count=span.part_count,
        ))
    return refs


def search_content(db: Session, filters: SearchFilters, *, limit: int = 200) -> list[SearchHit]:
    slots = _slot_index(db)
    hits: list[SearchHit] = []

    want_seq = filters.content_type in (None, "", "exr_sequence")
    want_item = filters.content_type not in ("exr_sequence",)

    tape_id_filter: int | None = None
    if filters.tape_barcode:
        t = db.scalar(select(Tape).where(Tape.barcode == filters.tape_barcode))
        tape_id_filter = t.id if t else -1

    if want_seq:
        stmt = select(SequenceContainer)
        if filters.q:
            like = f"%{filters.q}%"
            stmt = stmt.where(or_(
                SequenceContainer.project_name.ilike(like),
                SequenceContainer.sequence_name.ilike(like),
                SequenceContainer.shot_name.ilike(like),
                SequenceContainer.source_path.ilike(like),
            ))
        if filters.project:
            stmt = stmt.where(SequenceContainer.project_name.ilike(f"%{filters.project}%"))
        if filters.source_machine:
            stmt = stmt.where(SequenceContainer.source_machine == filters.source_machine)
        if filters.backup_category:
            stmt = stmt.where(SequenceContainer.backup_category == filters.backup_category)
        if filters.written_from:
            stmt = stmt.where(SequenceContainer.written_at >= filters.written_from)
        if filters.written_to:
            stmt = stmt.where(SequenceContainer.written_at <= filters.written_to)
        for c in db.scalars(stmt.limit(limit)).all():
            spans = list(c.spans)
            if tape_id_filter is not None and not any(s.tape_id == tape_id_filter for s in spans):
                continue
            hits.append(SearchHit(
                kind="sequence", id=c.id,
                title=" / ".join(x for x in [c.sequence_name, c.shot_name] if x) or c.shot_name or "sequence",
                project=c.project_name,
                subtitle=(f"frames {c.frame_start}-{c.frame_end} ({c.frame_count}) "
                          f"{'· GAPS: ' + (c.gap_detail or '') if c.has_gaps else ''}"),
                content_type="exr_sequence", backup_category=c.backup_category.value,
                source_machine=c.source_machine, source_path=c.source_path,
                size_bytes=c.total_size_bytes, written_at=c.written_at, verified_at=c.verified_at,
                tapes=_tape_refs(db, spans, slots),
            ))

    if want_item:
        stmt = select(ContentItem)
        if filters.content_type in ("video_chunk", "config", "audio_media"):
            stmt = stmt.where(ContentItem.content_type == filters.content_type)
        if filters.q:
            like = f"%{filters.q}%"
            stmt = stmt.where(or_(
                ContentItem.project_name.ilike(like),
                ContentItem.video_name.ilike(like),
                ContentItem.filename.ilike(like),
                ContentItem.source_path.ilike(like),
            ))
        if filters.project:
            stmt = stmt.where(ContentItem.project_name.ilike(f"%{filters.project}%"))
        if filters.source_machine:
            stmt = stmt.where(ContentItem.source_machine == filters.source_machine)
        if filters.backup_category:
            stmt = stmt.where(ContentItem.backup_category == filters.backup_category)
        if filters.written_from:
            stmt = stmt.where(ContentItem.written_at >= filters.written_from)
        if filters.written_to:
            stmt = stmt.where(ContentItem.written_at <= filters.written_to)
        for it in db.scalars(stmt.limit(limit)).all():
            spans = list(it.spans)
            if tape_id_filter is not None and not any(s.tape_id == tape_id_filter for s in spans):
                continue
            if it.content_type.value == "video_chunk":
                title = f"{it.video_name} — chunk {it.chunk_index}/{it.total_chunks}"
            else:
                title = it.filename
            hits.append(SearchHit(
                kind="item", id=it.id, title=title, project=it.project_name,
                subtitle=it.source_system_tag or it.content_type.value,
                content_type=it.content_type.value, backup_category=it.backup_category.value,
                source_machine=it.source_machine, source_path=it.source_path,
                size_bytes=it.file_size_bytes, written_at=it.written_at, verified_at=it.verified_at,
                tapes=_tape_refs(db, spans, slots),
            ))

    hits.sort(key=lambda h: (h.project or "", h.title))
    return hits[:limit]


def distinct_source_machines(db: Session) -> list[str]:
    a = db.scalars(select(SequenceContainer.source_machine).distinct()).all()
    b = db.scalars(select(ContentItem.source_machine).distinct()).all()
    return sorted({x for x in [*a, *b] if x})


def catalog_totals(db: Session) -> dict:
    n_seq = db.query(SequenceContainer).count()
    n_item = db.query(ContentItem).count()
    n_span = db.query(ContentTapeSpan).count()
    bytes_seq = db.scalar(select(func.coalesce(func.sum(SequenceContainer.total_size_bytes), 0)))
    bytes_item = db.scalar(select(func.coalesce(func.sum(ContentItem.file_size_bytes), 0)))
    return {
        "sequences": n_seq, "items": n_item, "spans": n_span,
        "bytes": int(bytes_seq or 0) + int(bytes_item or 0),
    }
