from __future__ import annotations

from app.models import ContentType
from app.services.intake import FileUnit, SequenceUnit, scan_source


def test_scan_classifies_all_four_types(make_source):
    root = make_source()
    units = scan_source(root)

    seqs = [u for u in units if isinstance(u, SequenceUnit)]
    items = [u for u in units if isinstance(u, FileUnit)]
    assert len(seqs) == 1
    seq = seqs[0]
    assert seq.project_name == "Project-Foo"
    assert seq.sequence_name == "seq010"
    assert seq.shot_name == "shot0100"
    assert seq.frame_count == 6
    assert seq.frame_start == 1 and seq.frame_end == 6
    assert seq.total_size_bytes == 6 * 20_000

    chunks = [i for i in items if i.content_type == ContentType.video_chunk]
    assert len(chunks) == 3
    assert {c.chunk_index for c in chunks} == {1, 2, 3}
    assert all(c.total_chunks == 3 for c in chunks)
    assert all(c.video_name == "reel_a" for c in chunks)

    configs = [i for i in items if i.content_type == ContentType.config]
    assert {c.filename for c in configs} == {"render.json", "notes.txt"}


def test_sequence_gap_detection(tmp_path):
    shot = tmp_path / "P" / "s" / "shot"
    shot.mkdir(parents=True)
    for i in (1, 2, 3, 7, 8):
        (shot / f"shot.{i:04d}.exr").write_bytes(b"x" * 100)
    (units := scan_source(tmp_path))
    seq = [u for u in units if isinstance(u, SequenceUnit)][0]
    has_gaps, detail = seq.gap_report()
    assert has_gaps is True
    assert detail == "4-6"


def test_unclassified_still_catalogued(tmp_path):
    d = tmp_path / "Proj"
    d.mkdir()
    (d / "weird.xyz").write_bytes(b"data")
    units = scan_source(tmp_path)
    assert len(units) == 1
    u = units[0]
    assert isinstance(u, FileUnit)
    assert u.source_system_tag == "unclassified"
