from datetime import date
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from lxml import etree

from fis.services.equator import CUSTOMER, EquatorState
from fis.storage.database import Database
from fis.storage.repositories import DefaultsRepository
from fis.xlsx.equator import XDR, generate_equator, parse_supported_invoice
from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import SafePackage
from fis.xlsx.parser import MAIN_NS
from tests.fixtures.equator_builder import IDS, build_equator


def setup_state(path: Path, defaults: DefaultsRepository | None = None) -> EquatorState:
    state = EquatorState(parse_supported_invoice(path), defaults, date(2026, 11, 13))
    for id_, code in zip(IDS, ("SMF3", "TEB9"), strict=True):
        state.map_warehouse(id_, code)
        state.save(id_, "channel", "美东美森卡派(包税)")
        state.save(id_, "delivery_warehouse", "赤道深圳仓")
    return state


def test_single_sheet_preserves_different_shipments_amounts_images_and_source(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_equator(source, external=True)
    original = source.read_bytes()
    state = setup_state(source)
    state.save(IDS[1], "channel", "美西美森大货中转(不包税)")
    state.save(IDS[1], "arrival_time", "2026-11-20T14:30")
    state.save_settings({"customer_code": "TEST-CUSTOMER"})
    result = generate_equator(source, state.bundle, {s: state.fields(s) for s in IDS}, tmp_path, source.name, state.catalog)
    parsed = parse_supported_invoice(result)
    assert len(parsed.shipments) == 2 and len(parsed.product_rows) == 4
    first, second = parsed.shipments
    assert {i.cells["G"] for i in first.items} == {"CD0016"}
    assert {i.cells["G"] for i in second.items} == {"CD0209"}
    assert {i.cells["J"] for i in first.items} == {"SMF3"}
    assert {i.cells["J"] for i in second.items} == {"TEB9"}
    assert {i.cells["A"] for s in parsed.shipments for i in s.items} == {"TEST-CUSTOMER"}
    assert {i.cells["B"] for i in first.items} == {IDS[0]}
    assert {i.cells["B"] for i in second.items} == {IDS[1]}
    assert all(i.cells["B"] == s.shipment_id for s in parsed.shipments for i in s.items)
    assert sum(float(i.cells["AM"]) for s in parsed.shipments for i in s.items) == 5700
    assert source.read_bytes() == original
    with ZipFile(result) as z:
        assert not any("externalLink" in p for p in z.namelist())
        sheets = etree.fromstring(z.read("xl/workbook.xml")).findall(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet")
        assert [s.get("name") for s in sheets] == ["模版", "产品库", "FBA地址库", "base"]
        assert len(etree.fromstring(z.read("xl/drawings/drawing1.xml")).findall(f"{{{XDR}}}twoCellAnchor")) == 4
        with ZipFile(source) as original_zip:
            assert z.read("xl/media/test.png") == original_zip.read("xl/media/test.png")
        for name in z.namelist():
            if name.endswith(".xml"):
                assert not etree.fromstring(z.read(name)).findall(f".//{{{MAIN_NS}}}f")
    again = generate_equator(source, state.bundle, {s: state.fields(s) for s in IDS}, tmp_path, source.name, state.catalog)
    assert again != result and result.is_file()


def test_remembers_channel_immediately_and_uses_next_thursday_midnight(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    defaults = DefaultsRepository(database)
    source = tmp_path / "invoice.xlsx"
    build_equator(source)
    state = setup_state(source, defaults)
    assert state.fields(IDS[0])["customer_code"] == CUSTOMER
    assert state.fields(IDS[0])["arrival_time"] == "2026-11-19T00:00"
    assert defaults.get("equator_channel:SMF3") == "美东美森卡派(包税)"
    another = EquatorState(state.bundle, defaults, date(2026, 11, 19))
    another.map_warehouse(IDS[0], "SMF3")
    assert another.fields(IDS[0])["channel"] == "美东美森卡派(包税)"
    assert another.fields(IDS[0])["arrival_time"] == "2026-11-26T00:00"
    state.save(IDS[1], "channel", "美西美森大货中转(不包税)")
    another.map_warehouse(IDS[0], "TEB9")
    assert another.fields(IDS[0])["channel"] == "美西美森大货中转(不包税)"
    another.save(IDS[0], "channel", "美东美森卡派(包税)")
    another.map_warehouse(IDS[0], "LAX9")
    assert another.fields(IDS[0])["channel"] == "美东美森卡派(包税)"


def test_external_resources_remain_rejected_except_lookup_workbooks(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_equator(source, external=True)
    with pytest.raises(WorkbookError, match="外部"):
        SafePackage.open(source)
    assert len(parse_supported_invoice(source).product_rows) == 4
    with ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    key = "xl/externalLinks/_rels/externalLink1.xml.rels"
    parts[key] = parts[key].replace(b"externalLinkPath", b"image")
    with ZipFile(source, "w", ZIP_DEFLATED) as z:
        for name, payload in parts.items():
            z.writestr(name, payload)
    with pytest.raises(WorkbookError, match="外部"):
        parse_supported_invoice(source)


def test_missing_picture_blocks_publication(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_equator(source)
    state = setup_state(source)
    with ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    drawing = etree.fromstring(parts["xl/drawings/drawing1.xml"])
    drawing.remove(drawing[-1])
    parts["xl/drawings/drawing1.xml"] = etree.tostring(drawing)
    with ZipFile(source, "w", ZIP_DEFLATED) as z:
        for name, payload in parts.items():
            z.writestr(name, payload)
    with pytest.raises(WorkbookError, match="缺少图片"):
        generate_equator(source, state.bundle, {s: state.fields(s) for s in IDS}, tmp_path, source.name, state.catalog)
    assert not list(tmp_path.glob("已处理*.xlsx"))


@pytest.mark.parametrize("column,value", [("AM", "999"), ("AK", "NaN"), ("X", "1.5")])
def test_bad_amounts_and_carton_counts_are_located(tmp_path: Path, column: str, value: str) -> None:
    source = tmp_path / "invoice.xlsx"
    build_equator(source)
    with ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    sheet = etree.fromstring(parts["xl/worksheets/sheet1.xml"])
    cell = sheet.find(f'.//{{{MAIN_NS}}}c[@r="{column}5"]/{{{MAIN_NS}}}v')
    assert cell is not None
    cell.text = value
    parts["xl/worksheets/sheet1.xml"] = etree.tostring(sheet)
    with ZipFile(source, "w", ZIP_DEFLATED) as z:
        for name, payload in parts.items():
            z.writestr(name, payload)
    with pytest.raises(WorkbookError) as caught:
        parse_supported_invoice(source)
    assert caught.value.location == f"{column}5"


def test_shared_cartons_do_not_double_count_and_keep_merge(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_equator(source)
    with ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    sheet = etree.fromstring(parts["xl/worksheets/sheet1.xml"])
    for column, value in (("X", None), ("AK", "40"), ("AM", "600")):
        cell = sheet.find(f'.//{{{MAIN_NS}}}c[@r="{column}6"]/{{{MAIN_NS}}}v')
        assert cell is not None
        if value is None:
            cell.getparent().remove(cell)
        else:
            cell.text = value
    merge = etree.Element(f"{{{MAIN_NS}}}mergeCells", count="1")
    etree.SubElement(merge, f"{{{MAIN_NS}}}mergeCell", ref="X5:X6")
    sheet.insert(1, merge)
    parts["xl/worksheets/sheet1.xml"] = etree.tostring(sheet)
    with ZipFile(source, "w", ZIP_DEFLATED) as z:
        for name, payload in parts.items():
            z.writestr(name, payload)
    state = setup_state(source)
    assert state.row_view(IDS[0])["carton_count"] == "2"
    result = generate_equator(source, state.bundle, {s: state.fields(s) for s in IDS}, tmp_path, source.name, state.catalog)
    with ZipFile(result) as z:
        sheet = etree.fromstring(z.read("xl/worksheets/sheet1.xml"))
        assert sheet.find(f'.//{{{MAIN_NS}}}mergeCell[@ref="X5:X6"]') is not None


@pytest.mark.parametrize("country,currency", [("US", "USD"), ("GB", "GBP"), ("CA", "CAD"), ("DE", "EUR"), ("FR", "EUR")])
def test_currency_uses_warehouse_country_and_does_not_convert_amounts(
    tmp_path: Path, country: str, currency: str,
) -> None:
    source = tmp_path / "invoice.xlsx"
    # AN may be blank or stale. Warehouse country is the new source of truth.
    build_equator(source, currency="")
    original = source.read_bytes()
    state = setup_state(source)
    code = next(w["A"] for w in state.catalog["warehouses"] if w.get("B") == country and w.get("C") == "FBA仓")
    state.map_warehouse(IDS[0], code)
    assert state.fields(IDS[0])["currency"] == currency
    assert not state.errors(IDS[0])
    result = generate_equator(source, state.bundle, {s: state.fields(s) for s in IDS}, tmp_path, source.name, state.catalog)
    parsed = parse_supported_invoice(result)
    assert {i.cells["AN"] for i in parsed.shipments[0].items} == {currency}
    assert {i.cells["K"] for i in parsed.shipments[0].items} == {country}
    assert [i.cells["AL"] for i in parsed.shipments[0].items] == ["15", "15"]
    assert [i.cells["AM"] for i in parsed.shipments[0].items] == ["600", "2400"]
    assert {i.cells["AN"] for i in parsed.shipments[1].items} == {"USD"}
    state.map_warehouse(IDS[0], "SMF3")
    assert state.fields(IDS[0])["currency"] == "USD"
    assert source.read_bytes() == original


@pytest.mark.parametrize("country", ["PL", "CZ", "SE", "JP", "AU", "MX", "CN", ""])
def test_unconfigured_country_cannot_reuse_source_usd_or_publish(
    tmp_path: Path, country: str,
) -> None:
    source = tmp_path / "invoice.xlsx"
    build_equator(source, currency="USD")
    state = setup_state(source)
    code = next(w["A"] for w in state.catalog["warehouses"] if w.get("B", "") == country)
    state.map_warehouse(IDS[0], code)
    assert state.fields(IDS[0])["currency"] == ""
    assert "待配置" in state.errors(IDS[0])["currency"]
    with pytest.raises(WorkbookError, match="币种待配置"):
        generate_equator(source, state.bundle, {s: state.fields(s) for s in IDS}, tmp_path, source.name, state.catalog)
    assert not list(tmp_path.glob("已处理*.xlsx"))
