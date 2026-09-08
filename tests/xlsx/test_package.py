from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import SafePackage
from tests.fixtures.standard_v1_builder import build_standard_v1

CORE_PARTS = {
    "[Content_Types].xml": b"<?xml version='1.0'?><Types/>",
    "_rels/.rels": b"<?xml version='1.0'?><Relationships/>",
    "xl/workbook.xml": b"<?xml version='1.0'?><workbook/>",
}


def write_package(path: Path, parts: dict[str, bytes] | None = None) -> Path:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in (parts or CORE_PARTS).items():
            archive.writestr(name, content)
    return path


def error_code(action: Callable[[], object]) -> str:
    with pytest.raises(WorkbookError) as captured:
        action()
    assert str(captured.value) == captured.value.message
    return captured.value.code


def test_opens_anonymous_standard_v1_fixture(tmp_path: Path) -> None:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, {"SYNTHETIC-1": 1}, [12])

    with SafePackage.open(path) as package:
        assert "xl/workbook.xml" in package.names
        assert package.read("xl/workbook.xml").startswith(b"<?xml")


def test_requires_an_existing_xlsx_file(tmp_path: Path) -> None:
    assert error_code(lambda: SafePackage.open(tmp_path / "missing.xlsx")) == "INVALID_PATH"
    assert error_code(lambda: SafePackage.open(tmp_path)) == "INVALID_PATH"
    wrong_extension = write_package(tmp_path / "book.zip")
    assert error_code(lambda: SafePackage.open(wrong_extension)) == "INVALID_PATH"


@pytest.mark.parametrize(
    "name",
    [
        "../outside.xml",
        "/absolute.xml",
        "xl\\workbook.xml",
        "C:drive.xml",
        "//server/share.xml",
        ".",
        "xl//workbook.xml",
    ],
)
def test_rejects_unsafe_zip_entry_names(tmp_path: Path, name: str) -> None:
    parts = dict(CORE_PARTS)
    parts[name] = b"unsafe"
    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "unsafe.xlsx", parts)))
        == "UNSAFE_PATH"
    )


def test_rejects_duplicate_normalized_entry_names(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.xlsx"
    with ZipFile(path, "w") as archive:
        for name, content in CORE_PARTS.items():
            archive.writestr(name, content)
        archive.writestr("xl/a.xml", b"one")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("xl/a.xml", b"two")

    assert error_code(lambda: SafePackage.open(path)) == "DUPLICATE_ENTRY"


def test_rejects_encrypted_zip_entries(tmp_path: Path) -> None:
    path = tmp_path / "encrypted.xlsx"
    with ZipFile(path, "w") as archive:
        for name, content in CORE_PARTS.items():
            info = ZipInfo(name)
            if name == "xl/workbook.xml":
                info.flag_bits |= 0x1
                archive.writestr(info, content)

    # ``zipfile`` deliberately clears the flag while writing because it cannot
    # encrypt.  Set it in the central directory, which is where it is read.
    payload = bytearray(path.read_bytes())
    offset = payload.index(b"PK\x01\x02")
    payload[offset + 8] |= 0x1
    path.write_bytes(payload)

    assert error_code(lambda: SafePackage.open(path)) == "ENCRYPTED_ENTRY"


@pytest.mark.parametrize(
    ("extra_name", "extra_payload"),
    [
        ("xl/vbaProject.bin", b"macro"),
        (
            "[Content_Types].xml",
            (
                b"<Types><Override PartName='/xl/workbook.xml' "
                b"ContentType='application/vnd.ms-excel.sheet.macroEnabled.main+xml'/></Types>"
            ),
        ),
        (
            "xl/_rels/workbook.xml.rels",
            (
                b"<Relationships><Relationship Id='rId1' Type="
                b"'http://schemas.microsoft.com/office/2006/relationships/vbaProject' "
                b"Target='vbaProject.bin'/></Relationships>"
            ),
        ),
    ],
)
def test_rejects_macro_parts_content_types_and_relationships(
    tmp_path: Path, extra_name: str, extra_payload: bytes
) -> None:
    parts = dict(CORE_PARTS)
    parts[extra_name] = extra_payload
    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "macro.xlsx", parts)))
        == "MACRO_UNSUPPORTED"
    )


@pytest.mark.parametrize(
    ("part", "payload", "expected"),
    [
        (
            "xl/_rels/workbook.xml.rels",
            b"<Relationships><Relationship Id='rId1' Type='image' Target='https://example.test/a.png' TargetMode='External'/></Relationships>",
            "EXTERNAL_RESOURCE",
        ),
        ("xl/embeddings/oleObject1.bin", b"ole", "OLE_UNSUPPORTED"),
        ("xl/activeX/activeX1.bin", b"activex", "ACTIVEX_UNSUPPORTED"),
    ],
)
def test_rejects_external_ole_and_activex_parts(
    tmp_path: Path, part: str, payload: bytes, expected: str
) -> None:
    parts = dict(CORE_PARTS)
    parts[part] = payload
    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "unsupported.xlsx", parts)))
        == expected
    )


@pytest.mark.parametrize("missing", list(CORE_PARTS))
def test_requires_core_ooxml_parts(tmp_path: Path, missing: str) -> None:
    parts = dict(CORE_PARTS)
    del parts[missing]
    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "missing.xlsx", parts)))
        == "MISSING_CORE_PART"
    )


def test_rejects_bad_zip(tmp_path: Path) -> None:
    path = tmp_path / "bad.xlsx"
    path.write_bytes(b"not a zip")
    assert error_code(lambda: SafePackage.open(path)) == "INVALID_XLSX"


def test_limits_are_checked_before_opening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fis.xlsx.package as package_module

    monkeypatch.setattr(package_module, "MAX_ENTRIES", 3)
    too_many = dict(CORE_PARTS)
    too_many["xl/extra.xml"] = b"x"
    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "many.xlsx", too_many)))
        == "TOO_MANY_ENTRIES"
    )

    monkeypatch.setattr(package_module, "MAX_ENTRIES", 5000)
    monkeypatch.setattr(package_module, "MAX_ENTRY", 4)
    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "entry.xlsx")))
        == "ENTRY_TOO_LARGE"
    )

    monkeypatch.setattr(package_module, "MAX_ENTRY", 50 * 1024 * 1024)
    monkeypatch.setattr(package_module, "MAX_UNCOMPRESSED", 5)
    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "total.xlsx")))
        == "PACKAGE_TOO_LARGE"
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            b"<!DOCTYPE workbook [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><workbook>&xxe;</workbook>",
            "UNSAFE_XML",
        ),
        (b"<workbook>", "INVALID_XML"),
    ],
)
def test_read_xml_rejects_unsafe_or_malformed_xml(
    tmp_path: Path, payload: bytes, expected: str
) -> None:
    parts = dict(CORE_PARTS)
    parts["xl/workbook.xml"] = payload
    with SafePackage.open(write_package(tmp_path / "xml.xlsx", parts)) as package:
        assert error_code(lambda: package.read_xml("xl/workbook.xml", 1024)) == expected


def test_read_is_bounded_by_actual_bytes_and_missing_parts_fail(tmp_path: Path) -> None:
    parts = dict(CORE_PARTS)
    parts["xl/workbook.xml"] = b"012345"
    with SafePackage.open(write_package(tmp_path / "read.xlsx", parts)) as package:
        assert error_code(lambda: package.read("missing.xml")) == "MISSING_PART"
        assert error_code(lambda: package.read("xl/workbook.xml", max_bytes=5)) == "READ_TOO_LARGE"


def test_context_manager_and_explicit_close_make_access_unavailable(tmp_path: Path) -> None:
    package = SafePackage.open(write_package(tmp_path / "close.xlsx"))
    assert package.archive.fp is not None
    package.close()
    assert package.archive.fp is None
    assert error_code(lambda: package.read("xl/workbook.xml")) == "PACKAGE_CLOSED"
    assert error_code(lambda: package.names) == "PACKAGE_CLOSED"


def test_open_failure_closes_the_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fis.xlsx.package as package_module

    path = write_package(tmp_path / "failure.xlsx")
    created: list[ZipFile] = []
    original = cast(Callable[..., ZipFile], package_module.ZipFile)  # type: ignore[attr-defined]

    def tracked_zipfile(*args: object, **kwargs: object) -> ZipFile:
        archive = original(*args, **kwargs)
        created.append(archive)
        return archive

    monkeypatch.setattr(package_module, "ZipFile", tracked_zipfile)
    monkeypatch.setattr(package_module, "MAX_ENTRIES", 1)

    assert error_code(lambda: SafePackage.open(path)) == "TOO_MANY_ENTRIES"
    assert created[0].fp is None
