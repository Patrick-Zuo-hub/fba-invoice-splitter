import os
import subprocess
from pathlib import Path

import pytest

import fis.storage.batch_files as batch_files_module
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


def test_cleanup_rejects_data_root_replaced_with_symlink(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    files = BatchFiles(data_root)
    files.create("batch-1")
    retained_data = tmp_path / "retained-data"
    data_root.rename(retained_data)

    external = tmp_path / "outside"
    external_work = external / "batches" / "batch-1" / "work"
    external_work.mkdir(parents=True)
    keep = external_work / "keep.txt"
    keep.write_text("keep")
    data_root.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="[Ss]ymlink|[Jj]unction"):
        files.clear_work("batch-1")

    assert keep.read_text() == "keep"


def test_cleanup_uses_opened_ancestor_when_parent_is_replaced_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    data_root = parent / "data"
    files = BatchFiles(data_root)
    work = files.create("batch-1")
    (work / "old.txt").write_text("old")

    outside = tmp_path / "outside"
    external_work = outside / "data" / "batches" / "batch-1" / "work"
    external_work.mkdir(parents=True)
    keep = external_work / "keep.txt"
    keep.write_text("keep")

    original_open = os.open
    injected = False

    def replace_parent_before_data_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        path_name = os.fspath(path)
        if not injected and (path_name == "data" or path_name == os.fspath(data_root)):
            retained_parent = tmp_path / "retained-parent"
            parent.rename(retained_parent)
            parent.symlink_to(outside, target_is_directory=True)
            injected = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(batch_files_module.os, "open", replace_parent_before_data_open)

    files.clear_work("batch-1")

    assert injected
    assert keep.read_text() == "keep"


def test_cleanup_fails_closed_without_secure_directory_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = BatchFiles(tmp_path / "data")
    work = files.create("batch-1")
    (work / "keep.txt").write_text("keep")
    monkeypatch.setattr(batch_files_module, "_USE_DIRECTORY_FDS", False)

    with pytest.raises(RuntimeError, match="secure directory handles"):
        files.clear_work("batch-1")

    assert (work / "keep.txt").read_text() == "keep"


def test_publish_fails_closed_without_secure_directory_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = BatchFiles(tmp_path / "data")
    work = files.create("batch-1")
    (work / "keep.txt").write_text("keep")
    monkeypatch.setattr(batch_files_module, "_USE_DIRECTORY_FDS", False)

    with pytest.raises(RuntimeError, match="secure directory handles"):
        files.publish("batch-1")

    assert work.is_dir()
    assert not files.output_path("batch-1").exists()


def test_delete_fails_closed_without_secure_directory_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = BatchFiles(tmp_path / "data")
    work = files.create("batch-1")
    (work / "keep.txt").write_text("keep")
    monkeypatch.setattr(batch_files_module, "_USE_DIRECTORY_FDS", False)

    with pytest.raises(RuntimeError, match="secure directory handles"):
        files.delete_batch("batch-1")

    assert (work / "keep.txt").read_text() == "keep"


def test_cleanup_rejects_batches_root_replaced_with_symlink(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    files = BatchFiles(data_root)
    files.create("batch-1")
    retained_batches = tmp_path / "retained-batches"
    files.batches_root.rename(retained_batches)
    files.batches_root.symlink_to(retained_batches, target_is_directory=True)

    with pytest.raises(ValueError, match="[Ss]ymlink|[Jj]unction"):
        files.clear_work("batch-1")


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions are only available on Windows")
def test_cleanup_rejects_windows_junction(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    files.create("batch-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep")
    work = files.work_path("batch-1")
    work.rmdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(work), str(outside)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(ValueError, match="[Ss]ymlink|[Jj]unction"):
        files.clear_work("batch-1")

    assert keep.read_text() == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handles are only available on Windows")
def test_windows_batch_file_operations_remain_available(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    work = files.create("batch-1")
    (work / "invoice.xlsx").write_bytes(b"xlsx")

    output = files.publish("batch-1")
    files.clear_output("batch-1")
    files.create("batch-1")
    files.delete_batch("batch-1")

    assert output.name == "output"
    assert not output.exists()


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
