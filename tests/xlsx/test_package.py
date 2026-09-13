from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_LZMA, ZipFile, ZipInfo

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
            if "\\" in name:
                # ZipInfo.__init__ rewrites os.sep to "/", so on Windows a
                # backslash entry would be normalised away before the reader
                # ever sees it. Set the name after construction to keep the
                # malformed entry intact on every platform.
                info = ZipInfo(date_time=(2026, 1, 1, 0, 0, 0))
                info.filename = name
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, content)
            else:
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


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("custom/strings.xml", "xl/custom/strings.xml"),
        ("/xl/custom/strings.xml", "xl/custom/strings.xml"),
    ],
)
def test_resolves_relative_and_root_relative_relationship_targets(
    tmp_path: Path, target: str, expected: str
) -> None:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, {"SYNTHETIC-1": 1}, [])

    with SafePackage.open(path) as package:
        assert package.resolve_relationship_target("xl/workbook.xml", target) == expected


def test_rejects_percent_encoded_unsafe_relationship_target(tmp_path: Path) -> None:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, {"SYNTHETIC-1": 1}, [])

    with SafePackage.open(path) as package:
        assert (
            error_code(
                lambda: package.resolve_relationship_target(
                    "xl/workbook.xml", "%2e%2e/%2e%2e/strings.xml"
                )
            )
            == "UNSAFE_RELATIONSHIP"
        )


def test_rejects_duplicate_relationship_ids_within_one_relationship_part(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-relationship-id.xlsx"
    build_standard_v1(path, {"SYNTHETIC-1": 1}, [])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>",
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert error_code(lambda: SafePackage.open(path)) == "INVALID_RELATIONSHIP"


@pytest.mark.parametrize("target", ["missing.xml", "worksheets"])
def test_rejects_dangling_or_directory_relationship_targets(tmp_path: Path, target: str) -> None:
    path = tmp_path / "dangling-target.xlsx"
    build_standard_v1(path, {"SYNTHETIC-1": 1}, [])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b'Target="styles.xml"', f'Target="{target}"'.encode()
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert error_code(lambda: SafePackage.open(path)) == "MISSING_RELATIONSHIP_TARGET"


def test_rejects_relationship_part_without_a_source_part(tmp_path: Path) -> None:
    path = tmp_path / "missing-relationship-source.xlsx"
    build_standard_v1(path, {"SYNTHETIC-1": 1}, [])
    with ZipFile(path, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/drawings/_rels/nonexistent.xml.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )

    assert error_code(lambda: SafePackage.open(path)) == "MISSING_RELATIONSHIP_SOURCE"


@pytest.mark.parametrize(
    ("attribute", "value"), [("Type", ""), ("Target", ""), ("Type", "worksheet")]
)
def test_rejects_missing_or_non_absolute_relationship_contract_fields(
    tmp_path: Path, attribute: str, value: str
) -> None:
    path = tmp_path / "invalid-relationship-contract.xlsx"
    build_standard_v1(path, {"SYNTHETIC-1": 1}, [])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    original = b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"'
    replacement = (
        b'Type="" Target="styles.xml"'
        if attribute == "Type" and not value
        else b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target=""'
        if attribute == "Target"
        else b'Type="worksheet" Target="styles.xml"'
    )
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        original, replacement
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert error_code(lambda: SafePackage.open(path)) == "INVALID_RELATIONSHIP"


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


def test_rejects_truncated_zip_entry_name_with_nul(tmp_path: Path) -> None:
    path = write_package(
        tmp_path / "truncated-entry.xlsx",
        {**CORE_PARTS, "xl/fooXbad.xml": b"safe"},
    )
    payload = bytearray(path.read_bytes())
    original_name = b"xl/fooXbad.xml"
    truncated_name = b"xl/foo\x00bad.xml"
    assert len(original_name) == len(truncated_name)
    assert payload.count(original_name) == 2
    payload[:] = payload.replace(original_name, truncated_name)
    path.write_bytes(payload)

    assert error_code(lambda: SafePackage.open(path)) == "UNSAFE_PATH"


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
            b"<Relationships><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/image' Target='https://example.test/a.png' TargetMode='External'/></Relationships>",
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


@pytest.mark.parametrize("compression", [ZIP_BZIP2, ZIP_LZMA])
def test_rejects_non_deflate_zip_compression_before_reading(
    tmp_path: Path, compression: int
) -> None:
    path = tmp_path / "unsupported-compression.xlsx"
    with ZipFile(path, "w", compression=compression) as archive:
        for name, payload in CORE_PARTS.items():
            archive.writestr(name, payload)

    assert error_code(lambda: SafePackage.open(path)) == "UNSUPPORTED_COMPRESSION"


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


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_read_xml_rejects_dtd_and_entity_in_all_supported_encodings(
    tmp_path: Path, encoding: str
) -> None:
    payload = (
        f"<?xml version='1.0' encoding='{encoding.upper()}'?>"
        "<!DOCTYPE workbook [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>"
        "<workbook>&xxe;</workbook>"
    ).encode(encoding)
    parts = dict(CORE_PARTS)
    parts["xl/workbook.xml"] = payload

    with SafePackage.open(write_package(tmp_path / f"dtd-{encoding}.xlsx", parts)) as package:
        assert error_code(lambda: package.read_xml("xl/workbook.xml", 4096)) == "UNSAFE_XML"


@pytest.mark.parametrize(
    "name",
    [
        "xl/%2e%2e/outside.xml",
        "xl/%252e%252e/outside.xml",
        "xl/%2foutside.xml",
        "xl/%5coutside.xml",
        "xl/%ZZ.xml",
        "xl/%00name.xml",
    ],
)
def test_rejects_percent_encoded_unsafe_part_names(tmp_path: Path, name: str) -> None:
    parts = dict(CORE_PARTS)
    parts[name] = b"unsafe"

    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "encoded-path.xlsx", parts)))
        == "UNSAFE_PATH"
    )


def test_allows_safe_percent_encoded_opc_part_name(tmp_path: Path) -> None:
    parts = dict(CORE_PARTS)
    parts["xl/custom%20part.xml"] = b"safe"

    with SafePackage.open(write_package(tmp_path / "encoded-space.xlsx", parts)) as package:
        assert package.read("xl/custom%20part.xml") == b"safe"


def test_percent_encoded_and_case_aliases_cannot_bypass_part_checks(tmp_path: Path) -> None:
    encoded_macro = dict(CORE_PARTS)
    encoded_macro["xl/%76baProject.bin"] = b"macro"
    assert (
        error_code(
            lambda: SafePackage.open(write_package(tmp_path / "encoded-macro.xlsx", encoded_macro))
        )
        == "MACRO_UNSUPPORTED"
    )

    aliases = dict(CORE_PARTS)
    aliases["xl/%77orkbook.xml"] = b"duplicate"
    assert (
        error_code(
            lambda: SafePackage.open(write_package(tmp_path / "encoded-alias.xlsx", aliases))
        )
        == "DUPLICATE_ENTRY"
    )


def test_opc_part_names_are_case_insensitive_for_core_and_duplicates(tmp_path: Path) -> None:
    uppercase_core = {
        "[Content_Types].xml": CORE_PARTS["[Content_Types].xml"],
        "_rels/.rels": CORE_PARTS["_rels/.rels"],
        "xl/WORKBOOK.xml": CORE_PARTS["xl/workbook.xml"],
    }
    with SafePackage.open(
        write_package(tmp_path / "uppercase-core.xlsx", uppercase_core)
    ) as package:
        assert package.read("xl/workbook.xml") == CORE_PARTS["xl/workbook.xml"]

    duplicate_case = dict(CORE_PARTS)
    duplicate_case["xl/WORKBOOK.xml"] = b"duplicate"
    assert (
        error_code(
            lambda: SafePackage.open(write_package(tmp_path / "case-alias.xlsx", duplicate_case))
        )
        == "DUPLICATE_ENTRY"
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://example.test/image.png",
        "../../outside.xml",
        "%2e%2e/%2e%2e/outside.xml",
        "/%2e%2e/outside.xml",
        "C:/outside.xml",
        "http://[",
    ],
)
def test_rejects_unsafe_internal_relationship_targets(tmp_path: Path, target: str) -> None:
    parts = dict(CORE_PARTS)
    parts["xl/_rels/workbook.xml.rels"] = (
        "<Relationships><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet' "
        f"Target='{target}'/></Relationships>"
    ).encode()

    assert (
        error_code(
            lambda: SafePackage.open(write_package(tmp_path / "unsafe-relation.xlsx", parts))
        )
        == "UNSAFE_RELATIONSHIP"
    )


def test_rejects_excessive_percent_decode_rounds_without_unbounded_work(tmp_path: Path) -> None:
    target = "%25" + "25" * 15_000
    parts = dict(CORE_PARTS)
    parts["xl/_rels/workbook.xml.rels"] = (
        "<Relationships><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet' "
        f"Target='{target}'/></Relationships>"
    ).encode()

    assert (
        error_code(lambda: SafePackage.open(write_package(tmp_path / "layered-uri.xlsx", parts)))
        == "UNSAFE_RELATIONSHIP"
    )


def test_relationship_files_allow_harmless_comments_and_processing_instructions(
    tmp_path: Path,
) -> None:
    parts = dict(CORE_PARTS)
    parts["_rels/.rels"] = (
        b"<Relationships><!-- harmless --><?audit generated?><Relationship "
        b"Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='xl/workbook.xml'/></Relationships>"
    )

    with SafePackage.open(write_package(tmp_path / "commented-rels.xlsx", parts)):
        pass


def test_allows_package_root_relative_internal_relationship_target(tmp_path: Path) -> None:
    parts = dict(CORE_PARTS)
    parts["xl/worksheets/sheet1.xml"] = b"<worksheet/>"
    parts["_rels/.rels"] = (
        b"<Relationships><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet' "
        b"Target='/xl/worksheets/sheet1.xml'/></Relationships>"
    )

    with SafePackage.open(write_package(tmp_path / "root-relative.xlsx", parts)):
        pass


def test_corrupt_deflate_is_a_stable_closed_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fis.xlsx.package as package_module

    path = write_package(tmp_path / "corrupt.xlsx")
    with ZipFile(path) as archive:
        info = archive.getinfo("[Content_Types].xml")
        header_offset = info.header_offset
    payload = bytearray(path.read_bytes())
    name_length = int.from_bytes(payload[header_offset + 26 : header_offset + 28], "little")
    extra_length = int.from_bytes(payload[header_offset + 28 : header_offset + 30], "little")
    compressed_start = header_offset + 30 + name_length + extra_length
    payload[compressed_start : compressed_start + info.compress_size] = b"\x00" * info.compress_size
    path.write_bytes(payload)

    created: list[ZipFile] = []
    original = cast(Callable[..., ZipFile], package_module.ZipFile)  # type: ignore[attr-defined]

    def tracked_zipfile(*args: object, **kwargs: object) -> ZipFile:
        archive = original(*args, **kwargs)
        created.append(archive)
        return archive

    monkeypatch.setattr(package_module, "ZipFile", tracked_zipfile)
    assert error_code(lambda: SafePackage.open(path)) == "INVALID_XLSX"
    assert created[0].fp is None


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


def test_accepts_explicit_directory_entries(tmp_path: Path) -> None:
    """Many writers emit zero-length directory entries; they are not parts."""

    path = tmp_path / "with-dirs.xlsx"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for directory in ("_rels/", "xl/", "xl/worksheets/"):
            archive.writestr(ZipInfo(directory), b"")
        for name, content in CORE_PARTS.items():
            archive.writestr(name, content)

    with SafePackage.open(path) as package:
        assert "xl/" not in package.names
        assert "xl/workbook.xml" in package.names


def test_rejects_a_directory_entry_carrying_data(tmp_path: Path) -> None:
    """A directory with content is not a directory; do not wave it through."""

    path = tmp_path / "fat-dir.xlsx"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(ZipInfo("xl/"), b"payload")
        for name, content in CORE_PARTS.items():
            archive.writestr(name, content)

    assert error_code(lambda: SafePackage.open(path)) == "UNSAFE_PATH"
