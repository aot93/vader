"""SQLAlchemy models — the permanent record.

Schema follows framework doc §4 (Suggested Data Model) with the additions the
prose calls for: read-error history (§3.4), a general audit log (§3.7) and a
persisted background-job table (§2 "Job execution").

Design notes
------------
* ``sequence_containers`` is intentionally separate from ``content_items`` —
  that is the two-tier Type-A model from §3.3 expressed in schema form.
* ``content_tape_spans`` is the only place a content row is tied to a tape, so
  automatically-split content (§3.5a) can reference several tapes without any
  row having to nominate a "primary" tape.
"""
from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# --- enumerations -----------------------------------------------------------


class TapeStatus(str, enum.Enum):
    scratch = "scratch"
    active = "active"
    full = "full"
    archived = "archived"
    retired = "retired"
    damaged = "damaged"


class BackupCategory(str, enum.Enum):
    project_archive = "project_archive"
    machine_drive_backup = "machine_drive_backup"


class ContentType(str, enum.Enum):
    video_chunk = "video_chunk"
    config = "config"
    audio_media = "audio_media"


class TapeEventType(str, enum.Enum):
    load = "load"
    unload = "unload"
    write = "write"
    verify = "verify"
    inventory = "inventory"
    format = "format"
    clean = "clean"
    retire = "retire"
    eject = "eject"


class EventResult(str, enum.Enum):
    pending = "pending"
    success = "success"
    error = "error"
    partial = "partial"


class JobType(str, enum.Enum):
    write = "write"
    verify = "verify"
    restore = "restore"
    backup = "backup"
    batch_format = "batch_format"
    # Note: single-tape format / clean / inventory are immediate library actions
    # run from the Library page (recorded as tape_events), not background jobs.
    # batch_format is the one exception: formatting/optimizing many tapes cycles
    # them through the drives one load/unload at a time and can run for hours
    # (mkltfs's optimize pass alone can take ~2h/tape on LTO-9), so it needs the
    # job table's progress tracking, cancellation and crash-recovery.


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"


class RestoreStatus(str, enum.Enum):
    pending = "pending"
    ready = "ready"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ConnectionHealth(str, enum.Enum):
    unknown = "unknown"
    healthy = "healthy"
    unhealthy = "unhealthy"


class ConnectionPurpose(str, enum.Enum):
    ingest = "ingest"
    restore_destination = "restore_destination"


# --- tables ---------------------------------------------------------------


class Tape(Base):
    __tablename__ = "tapes"

    id: Mapped[int] = mapped_column(primary_key=True)
    barcode: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    lto_generation: Mapped[str | None] = mapped_column(String(16))
    capacity_native_bytes: Mapped[int | None] = mapped_column(BigInteger)
    capacity_compressed_bytes: Mapped[int | None] = mapped_column(BigInteger)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[TapeStatus] = mapped_column(
        Enum(TapeStatus, native_enum=False, length=16), default=TapeStatus.scratch, index=True
    )
    first_written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    write_pass_count: Mapped[int] = mapped_column(Integer, default=0)
    physical_location: Mapped[str | None] = mapped_column(String(128))
    offsite_pair_tape_id: Mapped[int | None] = mapped_column(ForeignKey("tapes.id"))
    # Greedy-mode isolation (§3.5a): once set, only this source's data may land here.
    dedicated_source_machine: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow
    )

    spans: Mapped[list[ContentTapeSpan]] = relationship(back_populates="tape")
    events: Mapped[list[TapeEvent]] = relationship(back_populates="tape", order_by="TapeEvent.started_at")
    read_errors: Mapped[list[ReadError]] = relationship(back_populates="tape")

    @property
    def free_bytes(self) -> int:
        cap = self.capacity_native_bytes or 0
        return max(cap - (self.used_bytes or 0), 0)


class SequenceContainer(Base):
    """Type A primary unit — one row per shot / sequence folder (§3.3)."""

    __tablename__ = "sequence_containers"
    __table_args__ = (
        UniqueConstraint("source_path", name="uq_sequence_source_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_name: Mapped[str] = mapped_column(String(255), index=True)
    sequence_name: Mapped[str | None] = mapped_column(String(255), index=True)
    shot_name: Mapped[str | None] = mapped_column(String(255), index=True)
    source_path: Mapped[str] = mapped_column(Text)
    source_machine: Mapped[str | None] = mapped_column(String(255), index=True)
    backup_category: Mapped[BackupCategory] = mapped_column(
        Enum(BackupCategory, native_enum=False, length=32),
        default=BackupCategory.project_archive,
        index=True,
    )
    frame_start: Mapped[int | None] = mapped_column(Integer)
    frame_end: Mapped[int | None] = mapped_column(Integer)
    frame_count: Mapped[int | None] = mapped_column(Integer)
    has_gaps: Mapped[bool] = mapped_column(Boolean, default=False)
    gap_detail: Mapped[str | None] = mapped_column(Text)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # Sidecar frame-level manifest (filename + SHA256 per frame). Never a DB row.
    manifest_ref: Mapped[str | None] = mapped_column(Text)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    spans: Mapped[list[ContentTapeSpan]] = relationship(
        back_populates="sequence_container", cascade="all, delete-orphan"
    )


class ContentItem(Base):
    """Type B (video chunks), Type C (config/logs), Type D (audio/media)."""

    __tablename__ = "content_items"
    __table_args__ = (
        UniqueConstraint("source_path", name="uq_content_source_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    content_type: Mapped[ContentType] = mapped_column(
        Enum(ContentType, native_enum=False, length=32), index=True
    )
    project_name: Mapped[str | None] = mapped_column(String(255), index=True)
    video_name: Mapped[str | None] = mapped_column(String(255), index=True)
    chunk_index: Mapped[int | None] = mapped_column(Integer)
    total_chunks: Mapped[int | None] = mapped_column(Integer)
    source_path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(512))
    source_machine: Mapped[str | None] = mapped_column(String(255), index=True)
    source_system_tag: Mapped[str | None] = mapped_column(String(255))
    backup_category: Mapped[BackupCategory] = mapped_column(
        Enum(BackupCategory, native_enum=False, length=32),
        default=BackupCategory.project_archive,
        index=True,
    )
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    spans: Mapped[list[ContentTapeSpan]] = relationship(
        back_populates="content_item", cascade="all, delete-orphan"
    )


class ContentTapeSpan(Base):
    """Which tape(s) hold which piece of content, and where on the LTFS volume.

    Exactly one of ``sequence_container_id`` / ``content_item_id`` is set.
    ``part_index`` + byte range are only meaningful when a single item was split
    across tapes at write time (§3.5a).
    """

    __tablename__ = "content_tape_spans"
    __table_args__ = (
        CheckConstraint(
            "(sequence_container_id IS NOT NULL) <> (content_item_id IS NOT NULL)",
            name="ck_span_exactly_one_parent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tape_id: Mapped[int] = mapped_column(ForeignKey("tapes.id"), index=True)
    ltfs_path: Mapped[str] = mapped_column(Text)
    sequence_container_id: Mapped[int | None] = mapped_column(
        ForeignKey("sequence_containers.id", ondelete="CASCADE"), index=True
    )
    content_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    part_index: Mapped[int] = mapped_column(Integer, default=0)
    part_count: Mapped[int] = mapped_column(Integer, default=1)
    byte_range_start: Mapped[int | None] = mapped_column(BigInteger)
    byte_range_end: Mapped[int | None] = mapped_column(BigInteger)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tape: Mapped[Tape] = relationship(back_populates="spans")
    sequence_container: Mapped[SequenceContainer | None] = relationship(back_populates="spans")
    content_item: Mapped[ContentItem | None] = relationship(back_populates="spans")


class TapeEvent(Base):
    __tablename__ = "tape_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    tape_id: Mapped[int | None] = mapped_column(ForeignKey("tapes.id"), index=True)
    event_type: Mapped[TapeEventType] = mapped_column(
        Enum(TapeEventType, native_enum=False, length=16), index=True
    )
    slot_number: Mapped[int | None] = mapped_column(Integer)
    drive_number: Mapped[int | None] = mapped_column(Integer)
    initiated_by: Mapped[str] = mapped_column(String(128), default="operator")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[EventResult] = mapped_column(
        Enum(EventResult, native_enum=False, length=16), default=EventResult.pending
    )
    error_detail: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)

    tape: Mapped[Tape | None] = relationship(back_populates="events")


class ReadError(Base):
    """Per-tape SCSI / LTFS read-error history (§3.4, §5.7)."""

    __tablename__ = "read_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    tape_id: Mapped[int] = mapped_column(ForeignKey("tapes.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    operation: Mapped[str] = mapped_column(String(32), default="verify")
    device: Mapped[str | None] = mapped_column(String(64))
    ltfs_path: Mapped[str | None] = mapped_column(Text)
    error_text: Mapped[str] = mapped_column(Text)

    tape: Mapped[Tape] = relationship(back_populates="read_errors")


class RestoreRequest(Base):
    __tablename__ = "restore_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(128), default="operator")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sequence_container_ids: Mapped[list] = mapped_column(JSON, default=list)
    content_item_ids: Mapped[list] = mapped_column(JSON, default=list)
    destination_path: Mapped[str | None] = mapped_column(Text)
    # Set only when the destination was picked from a restore_destination
    # Connection rather than typed by hand — destination_path above is always
    # the fully-resolved path either way; this is purely for display/audit.
    destination_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("connections.id", ondelete="SET NULL")
    )
    include_manifests: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[RestoreStatus] = mapped_column(
        Enum(RestoreStatus, native_enum=False, length=16), default=RestoreStatus.pending, index=True
    )
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    """Persisted background job (§2). Survives a VM restart so an interrupted
    copy/verify can be spotted and safely re-run (§6 idempotency)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[JobType] = mapped_column(
        Enum(JobType, native_enum=False, length=16), index=True
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=16), default=JobStatus.queued, index=True
    )
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    progress_current: Mapped[int] = mapped_column(BigInteger, default=0)
    progress_total: Mapped[int] = mapped_column(BigInteger, default=0)
    progress_message: Mapped[str] = mapped_column(String(512), default="")
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(128), default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def progress_pct(self) -> int:
        if not self.progress_total:
            return 0
        return min(int(self.progress_current * 100 / self.progress_total), 100)


class AuditLog(Base):
    """App-level action log (§3.7) — catalog edits, exports, restore requests,
    config changes. Hardware actions additionally get a ``tape_events`` row."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(128), default="operator")
    action: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class LibrarySlot(Base):
    """Last-known physical library state, refreshed from ``mtx status``.
    Lets the dashboard render when a drive is busy or the changer is idle."""

    __tablename__ = "library_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_kind: Mapped[str] = mapped_column(String(16))  # storage | drive | import_export
    slot_number: Mapped[int] = mapped_column(Integer, index=True)
    barcode: Mapped[str | None] = mapped_column(String(64))
    drive_loaded_from: Mapped[int | None] = mapped_column(Integer)
    is_cleaning_tape: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("slot_kind", "slot_number", name="uq_slot_kind_number"),)


class Connection(Base):
    """An operator-configured Windows SMB share (originally "Source Manager
    design spec v1.0"; renamed/extended to cover restore destinations too).
    Vader auto-creates the mount point, a credentials file, and a systemd
    automount unit for it (see ``app/mounts``).

    ``purpose`` fixes how the mount is provisioned, for good:

    * ``ingest`` — mounted **read-only**, offered as a write-job source root.
      This is the original, unchanged behaviour.
    * ``restore_destination`` — mounted **read-write**, offered as a restore
      target. A distinct mount (own unit name / mount dir / credentials file)
      even for the same hostname as an existing ingest connection, so an
      ingest mount's read-only-ness is never touched by adding a restore
      destination for the same machine.

    Purpose is set once at creation and is not editable — changing it would
    mean re-provisioning the mount with different permissions, which is
    deliberately not offered as an in-place edit. Exactly one connection per
    ``(hostname, purpose)`` pair.

    The password is used once, at creation, to write the credentials file, and
    is never stored here or anywhere else in the database.
    """

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("hostname", "purpose", name="uq_connection_hostname_purpose"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), index=True)
    share: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[ConnectionPurpose] = mapped_column(
        Enum(ConnectionPurpose, native_enum=False, length=32),
        default=ConnectionPurpose.ingest, index=True,
    )
    mount_path: Mapped[str] = mapped_column(String(512))
    credentials_path: Mapped[str] = mapped_column(String(512))
    unit_name: Mapped[str] = mapped_column(String(255))
    smb_version: Mapped[str] = mapped_column(String(16), default="3.0")
    domain: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str] = mapped_column(String(255))
    last_health: Mapped[ConnectionHealth] = mapped_column(
        Enum(ConnectionHealth, native_enum=False, length=16), default=ConnectionHealth.unknown, index=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow
    )
