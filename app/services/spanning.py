"""Tape allocation, automatic cross-tape spanning, and greedy mode (§3.5a).

The allocator is pure / in-memory: it takes a view of the writable tapes and a
list of intake units, and returns a flat list of :class:`Placement` rows telling
the write job exactly what to copy where. The job layer is responsible for
actually formatting scratch tapes, copying bytes and persisting spans.

Rules implemented
-----------------
* **Automatic spanning (default).** A unit is placed whole on the current tape
  if it fits. If not, and it fits whole on another writable tape, it moves there
  (avoids needless fragmentation). If it fits on no single tape, it is split at a
  safe boundary — between whole frames for a sequence, on a byte boundary for a
  single oversized file — and continued on the next tape.
* **Greedy mode.** Restricts the writable pool to tapes already dedicated to the
  target source plus fresh scratch tapes (which become dedicated on first
  write). A partially-used tape belonging to another source — or to no source —
  is never touched, so the tape is left partly empty rather than mixed. This
  yields a clean, isolated recovery unit for a single machine's backup.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.intake import FileUnit, FrameFile, SequenceUnit


class AllocationError(RuntimeError):
    """Not enough writable tape to place a unit (need more scratch tapes)."""


@dataclass
class TapeView:
    tape_id: int
    barcode: str
    capacity_bytes: int
    used_bytes: int
    status: str
    dedicated_source_machine: str | None = None
    needs_format: bool = False  # scratch tape not yet LTFS-formatted this run

    @property
    def free_bytes(self) -> int:
        return max(self.capacity_bytes - self.used_bytes, 0)


@dataclass
class Placement:
    tape_id: int
    barcode: str
    part_index: int
    part_count: int
    size_bytes: int
    unit_key: str
    # sequence part
    frames: list[FrameFile] | None = None
    # oversized-file part
    whole_file: bool = True
    byte_range_start: int | None = None
    byte_range_end: int | None = None
    needs_format: bool = False


@dataclass
class AllocationResult:
    placements: list[Placement] = field(default_factory=list)
    tapes_touched: list[str] = field(default_factory=list)
    spanned_units: list[str] = field(default_factory=list)


def unit_key(unit: SequenceUnit | FileUnit) -> str:
    if isinstance(unit, SequenceUnit):
        return f"seq:{unit.source_path}:{unit.shot_name}"
    return f"item:{unit.source_path}"


class TapeAllocator:
    def __init__(
        self,
        writable: list[TapeView],
        scratch: list[TapeView],
        *,
        greedy: bool = False,
        greedy_source: str | None = None,
    ) -> None:
        self.greedy = greedy
        self.greedy_source = greedy_source
        if greedy:
            # only tapes already dedicated to this source may pre-exist in the pool
            self._open = [
                t for t in writable
                if t.dedicated_source_machine and t.dedicated_source_machine == greedy_source
            ]
        else:
            # fill partially-used active tapes before scratch
            self._open = sorted(writable, key=lambda t: (t.status != "active", -t.used_bytes))
        self._scratch = list(scratch)
        self._result = AllocationResult()

    # --- public --------------------------------------------------------

    def allocate(self, units: list[SequenceUnit | FileUnit]) -> AllocationResult:
        for unit in units:
            if isinstance(unit, SequenceUnit):
                self._place_sequence(unit)
            else:
                self._place_file(unit)
        self._result.tapes_touched = list(
            dict.fromkeys(p.barcode for p in self._result.placements)
        )
        return self._result

    # --- tape pool ----------------------------------------------------

    def _dedicate(self, tape: TapeView) -> None:
        if self.greedy and tape.dedicated_source_machine is None:
            tape.dedicated_source_machine = self.greedy_source

    def _next_fresh_tape(self) -> TapeView:
        if not self._scratch:
            raise AllocationError(
                "out of scratch tapes — load more blank tapes into the library and re-run "
                "(the job is idempotent; already-written units are skipped)"
            )
        tape = self._scratch.pop(0)
        tape.needs_format = True
        self._dedicate(tape)
        self._open.append(tape)
        return tape

    def _tape_that_fits(self, size: int) -> TapeView | None:
        for tape in self._open:
            if tape.free_bytes >= size:
                return tape
        # try opening scratch tapes until one fits or we run out
        while self._scratch:
            tape = self._next_fresh_tape()
            if tape.free_bytes >= size:
                return tape
        return None

    def _current_tape(self) -> TapeView:
        for tape in self._open:
            if tape.free_bytes > 0:
                return tape
        return self._next_fresh_tape()

    # --- placement ---------------------------------------------------

    def _record(self, placement: Placement, tape: TapeView) -> None:
        tape.used_bytes += placement.size_bytes
        # A scratch tape needs mkltfs once; the job dedupes by barcode.
        placement.needs_format = tape.needs_format
        self._result.placements.append(placement)

    def _place_sequence(self, unit: SequenceUnit) -> None:
        key = unit_key(unit)
        total = unit.total_size_bytes
        frames = unit.sorted_frames

        whole_tape = self._tape_that_fits(total)
        if whole_tape is not None:
            self._record(
                Placement(tape_id=whole_tape.tape_id, barcode=whole_tape.barcode,
                          part_index=0, part_count=1, size_bytes=total,
                          unit_key=key, frames=frames, needs_format=whole_tape.needs_format),
                whole_tape,
            )
            return

        # must span whole frames across tapes; reserve space as parts fill so
        # _next_tape_with_space actually advances to the next tape.
        self._result.spanned_units.append(key)
        parts: list[tuple[TapeView, list[FrameFile], int]] = []
        buf: list[FrameFile] = []
        buf_size = 0
        tape = self._current_tape()
        for fr in frames:
            if fr.size > tape.capacity_bytes:
                raise AllocationError(
                    f"frame {fr.filename} ({fr.size} B) exceeds a whole tape capacity"
                )
            if buf and buf_size + fr.size > tape.free_bytes:
                parts.append((tape, buf, buf_size))
                tape.used_bytes += buf_size
                buf, buf_size = [], 0
                tape = self._next_tape_with_space(fr.size)
            elif not buf and fr.size > tape.free_bytes:
                tape = self._next_tape_with_space(fr.size)
            buf.append(fr)
            buf_size += fr.size
        if buf:
            parts.append((tape, buf, buf_size))
            tape.used_bytes += buf_size

        for idx, (ptape, pframes, psize) in enumerate(parts):
            self._result.placements.append(
                Placement(tape_id=ptape.tape_id, barcode=ptape.barcode,
                          part_index=idx, part_count=len(parts), size_bytes=psize,
                          unit_key=key, frames=pframes, needs_format=ptape.needs_format)
            )

    def _place_file(self, unit: FileUnit) -> None:
        key = unit_key(unit)
        size = unit.size

        whole_tape = self._tape_that_fits(size)
        if whole_tape is not None:
            self._record(
                Placement(tape_id=whole_tape.tape_id, barcode=whole_tape.barcode,
                          part_index=0, part_count=1, size_bytes=size, unit_key=key,
                          whole_file=True, needs_format=whole_tape.needs_format),
                whole_tape,
            )
            return

        # single file bigger than any one tape: split on byte boundaries
        self._result.spanned_units.append(key)
        offset = 0
        parts: list[tuple[TapeView, int, int]] = []
        while offset < size:
            tape = self._next_tape_with_space(1)
            take = min(tape.free_bytes, size - offset)
            parts.append((tape, offset, offset + take))
            tape.used_bytes += take  # reserve now so _next_tape_with_space advances
            offset += take
        for idx, (ptape, start, end) in enumerate(parts):
            self._result.placements.append(
                Placement(tape_id=ptape.tape_id, barcode=ptape.barcode,
                          part_index=idx, part_count=len(parts), size_bytes=end - start,
                          unit_key=key, whole_file=False,
                          byte_range_start=start, byte_range_end=end,
                          needs_format=ptape.needs_format)
            )

    def _next_tape_with_space(self, need: int) -> TapeView:
        for tape in self._open:
            if tape.free_bytes >= need:
                return tape
        return self._next_fresh_tape()
