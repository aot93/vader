from __future__ import annotations

import pytest

from app.models import ContentType
from app.services.intake import FileUnit, FrameFile, SequenceUnit
from app.services.spanning import AllocationError, TapeAllocator, TapeView


def _tape(tid, cap=1000, used=0, status="scratch", dedicated=None):
    return TapeView(tape_id=tid, barcode=f"T{tid:03d}", capacity_bytes=cap,
                    used_bytes=used, status=status, dedicated_source_machine=dedicated)


def _seq(nframes, size):
    frames = [FrameFile(path=None, filename=f"f{i}.exr", frame=i, size=size) for i in range(1, nframes + 1)]
    return SequenceUnit(project_name="P", sequence_name="s", shot_name="shot",
                        source_path="/src/shot", frames=frames)


def test_small_sequence_fits_one_tape():
    alloc = TapeAllocator([], [_tape(1, cap=1000)]).allocate([_seq(4, 100)])
    assert len(alloc.placements) == 1
    p = alloc.placements[0]
    assert p.part_count == 1
    assert p.size_bytes == 400
    assert p.needs_format is True


def test_sequence_spans_multiple_tapes_on_frame_boundaries():
    # 10 frames * 100 = 1000; tapes hold 250 each -> 4 tapes, whole frames only
    scratch = [_tape(i, cap=250) for i in range(1, 6)]
    alloc = TapeAllocator([], scratch).allocate([_seq(10, 100)])
    assert len(alloc.placements) >= 4
    assert "seq:/src/shot:shot" in alloc.spanned_units
    total_frames = sum(len(p.frames) for p in alloc.placements)
    assert total_frames == 10
    for p in alloc.placements:
        assert p.size_bytes <= 250
        assert p.size_bytes == sum(f.size for f in p.frames)


def test_out_of_scratch_raises():
    with pytest.raises(AllocationError):
        TapeAllocator([], [_tape(1, cap=100)]).allocate([_seq(10, 100)])


def test_oversized_single_file_splits_on_byte_boundary():
    unit = FileUnit(content_type=ContentType.video_chunk, path=None,
                    source_path="/src/big.mov", filename="big.mov", size=1000,
                    video_name="big", chunk_index=1, total_chunks=1)
    scratch = [_tape(i, cap=400) for i in range(1, 5)]
    alloc = TapeAllocator([], scratch).allocate([unit])
    assert len(alloc.placements) == 3
    assert alloc.placements[0].whole_file is False
    covered = sum(p.byte_range_end - p.byte_range_start for p in alloc.placements)
    assert covered == 1000
    assert alloc.placements[0].byte_range_start == 0
    assert alloc.placements[-1].byte_range_end == 1000


def test_greedy_mode_ignores_other_sources_partial_tape():
    other = _tape(1, cap=1000, used=600, status="active", dedicated="Machine-01")
    mine = _tape(2, cap=1000, used=200, status="active", dedicated="Machine-07")
    fresh = _tape(3, cap=1000)
    alloc = TapeAllocator([other, mine], [fresh], greedy=True, greedy_source="Machine-07")
    res = alloc.allocate([_seq(4, 100)])
    used_barcodes = {p.barcode for p in res.placements}
    assert "T001" not in used_barcodes           # never touch another source's tape
    assert used_barcodes <= {"T002", "T003"}


def test_standard_mode_fills_active_tape_before_scratch():
    active = _tape(1, cap=1000, used=500, status="active")
    scratch = _tape(2, cap=1000)
    alloc = TapeAllocator([active], [scratch]).allocate([_seq(4, 100)])
    assert alloc.placements[0].barcode == "T001"
