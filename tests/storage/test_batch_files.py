from pathlib import Path

import pytest

from fis.storage.batch_files import BatchFiles

VALID_UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_create_returns_work_directory_under_batch_root(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")

    work = files.create("batch-1")

    assert work == (tmp_path / "data" / "batches" / "batch-1" / "work")
    assert work.is_dir()


def test_create_accepts_canonical_uuid_batch_id(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")

    work = files.create(VALID_UUID)

    assert work.is_dir()


def test_publish_atomically_moves_work_to_output(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    work = files.create("batch-1")
    (work / "invoice.xlsx").write_bytes(b"xlsx")

    output = files.publish("batch-1")

    assert output == files.output_path("batch-1")
    assert (output / "invoice.xlsx").read_bytes() == b"xlsx"
    assert not work.exists()


def test_publish_rejects_missing_work_directory(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")

    with pytest.raises(FileNotFoundError):
        files.publish("batch-1")


def test_publish_rejects_existing_output_directory(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    work = files.create("batch-1")
    files.output_path("batch-1").mkdir()
    (work / "new.txt").write_text("new")

    with pytest.raises(FileExistsError):
        files.publish("batch-1")

    assert work.is_dir()
    assert not (files.output_path("batch-1") / "new.txt").exists()


@pytest.mark.parametrize(
    "batch_id",
    [
        "",
        ".",
        "..",
        "../outside",
        "/absolute",
        "nested/batch-1",
        "batch-../x",
        "batch_A",
        "batch-Ü",
        "123e4567-e89b-12d3-a456-42661417400",
        "123e4567-e89b-12d3-a456-4266141740000",
        "123e4567_e89b_12d3_a456_426614174000",
    ],
)
def test_batch_id_must_be_safe(batch_id: str, tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")

    with pytest.raises(ValueError, match="batch ID"):
        files.create(batch_id)


@pytest.mark.parametrize("batch_id", [None, 123, Path("../outside")])
def test_non_string_batch_id_is_rejected(batch_id: object, tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")

    with pytest.raises(ValueError, match="batch ID"):
        files.create(batch_id)  # type: ignore[arg-type]


def test_clear_work_is_idempotent_for_missing_and_empty_directories(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")

    files.clear_work("batch-1")
    files.create("batch-1")
    files.clear_work("batch-1")
    files.clear_work("batch-1")

    assert not files.work_path("batch-1").exists()


def test_clear_output_is_idempotent_for_missing_and_empty_directories(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")

    files.clear_output("batch-1")
    files.create("batch-1")
    files.output_path("batch-1").mkdir()
    files.clear_output("batch-1")
    files.clear_output("batch-1")

    assert not files.output_path("batch-1").exists()


def test_cleanup_rejects_work_symlink_without_following_target(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    files.create("batch-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    files.work_path("batch-1").rmdir()
    files.work_path("batch-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="[Ss]ymlink"):
        files.clear_work("batch-1")

    assert (outside / "keep.txt").read_text() == "keep"
    assert files.work_path("batch-1").is_symlink()


def test_cleanup_rejects_output_symlink_without_following_target(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    files.create("batch-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    files.output_path("batch-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="[Ss]ymlink"):
        files.clear_output("batch-1")

    assert (outside / "keep.txt").read_text() == "keep"


def test_delete_batch_removes_only_requested_batch(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    first = files.create("batch-1")
    sibling = files.create("batch-2")
    (first / "source.xlsx").write_bytes(b"source")
    (sibling / "source.xlsx").write_bytes(b"sibling")

    files.delete_batch("batch-1")
    files.delete_batch("batch-1")

    assert not first.parent.exists()
    assert sibling.parent.is_dir()
    assert (sibling / "source.xlsx").read_bytes() == b"sibling"


def test_delete_batch_rejects_batch_symlink_without_following_target(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    files.create("batch-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    batch = files.work_path("batch-1").parent
    files.clear_work("batch-1")
    batch.rmdir()
    batch.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="[Ss]ymlink"):
        files.delete_batch("batch-1")

    assert (outside / "keep.txt").read_text() == "keep"


def test_publish_rejects_symlinked_work(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    files.create("batch-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    work = files.work_path("batch-1")
    work.rmdir()
    work.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="[Ss]ymlink"):
        files.publish("batch-1")


def test_publish_rejects_dangling_output_symlink(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    files.create("batch-1")
    output = files.output_path("batch-1")
    output.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(FileExistsError):
        files.publish("batch-1")
