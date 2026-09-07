# FIS v1 OOXML Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse the approved FBA workbook safely and generate one structurally valid, image-correct workbook per Shipment without rebuilding unrelated OOXML parts.

**Architecture:** Treat XLSX as a constrained ZIP package. Validate a bundled template signature, build one immutable parsed model, then transform only configured worksheet, drawing, relationship, workbook-calculation, and content-type parts. Copy every other package member byte-for-byte.

**Tech Stack:** Python 3.12, `zipfile`, lxml, Pydantic 2, pytest

---

## File map

| Path | Responsibility |
| --- | --- |
| `src/fis/xlsx/template.py` | Typed built-in template configuration |
| `src/fis/xlsx/package.py` | XLSX ZIP safety limits and part access |
| `src/fis/xlsx/parser.py` | Shipment/item/footer discovery |
| `src/fis/xlsx/drawings.py` | Supported image classification and relationship graph |
| `src/fis/xlsx/transform.py` | Worksheet row/reference transformation and totals |
| `src/fis/xlsx/generator.py` | Per-Shipment package generation |
| `src/fis/xlsx/validate.py` | Independent output invariant validation |
| `tests/fixtures/standard_v1_builder.py` | Synthetic, non-customer XLSX fixture generator |
| `tests/xlsx/` | Parser, transformation, security, and golden tests |

### Task 1: Pin the V1 template contract with a synthetic fixture

**Files:**
- Create: `src/fis/xlsx/__init__.py`
- Create: `src/fis/xlsx/template.py`
- Create: `tests/fixtures/standard_v1_builder.py`
- Create: `tests/xlsx/test_template.py`

- [ ] **Step 1: Write a failing template test**

```python
from fis.xlsx.template import STANDARD_V1


def test_standard_v1_contract() -> None:
    assert STANDARD_V1.sheet_name == "装箱单&发票 发货信息"
    assert STANDARD_V1.product_start_row == 12
    assert STANDARD_V1.value_ranges["warehouse_code"] == "C4:F4"
    assert STANDARD_V1.total_columns == {"cartons": "K", "gross_weight": "M", "volume": "N", "amount": "P"}
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/xlsx/test_template.py -v`

Expected: FAIL because the XLSX package is missing.

- [ ] **Step 3: Implement an immutable template definition**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateConfig:
    type: str
    version: str
    sheet_name: str
    product_start_row: int
    value_ranges: dict[str, str]
    total_columns: dict[str, str]


STANDARD_V1 = TemplateConfig(
    type="fba-customs-standard", version="v1",
    sheet_name="装箱单&发票 发货信息", product_start_row=12,
    value_ranges={
        "warehouse_code": "C4:F4", "warehouse_address": "C5:F5",
        "shipping_channel": "C6:F6", "delivery_date": "C8:D8",
        "sailing_date": "C9:D9", "delivery_warehouse": "C10:D10",
    },
    total_columns={"cartons": "K", "gross_weight": "M", "volume": "N", "amount": "P"},
)
```

Create `build_standard_v1(path, shipments, image_rows)` using `zipfile` and lxml. It must emit a minimal OOXML package with the exact sheet name, headers in A11:Z11, product rows from row 12, one blank separator, `合计TOTAL`, comments at L11/N11/P11, one-cell-high PNG anchors in column C, and relationships/content types for each image. Use generated identifiers and a 1×1 test PNG; never copy customer values or images.

- [ ] **Step 4: Run the test and generate one disposable fixture**

Run: `python -m pytest tests/xlsx/test_template.py -v`

Expected: PASS.

Run: `python -c "from pathlib import Path; from tests.fixtures.standard_v1_builder import build_standard_v1; build_standard_v1(Path('/tmp/fis-standard-v1.xlsx'), {'S1': 1}, [12])"`

Expected: `/tmp/fis-standard-v1.xlsx` is created and `unzip -t /tmp/fis-standard-v1.xlsx` reports no errors.

- [ ] **Step 5: Commit**

```bash
git add src/fis/xlsx tests/fixtures tests/xlsx/test_template.py
git commit -m "test: define synthetic standard workbook fixture"
```

### Task 2: Reject unsafe or unsupported XLSX packages

**Files:**
- Create: `src/fis/xlsx/errors.py`
- Create: `src/fis/xlsx/package.py`
- Test: `tests/xlsx/test_package.py`

- [ ] **Step 1: Write failing safety tests**

```python
from pathlib import Path
import pytest
from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import SafePackage


def test_rejects_path_traversal(tmp_path: Path, malicious_xlsx: Path) -> None:
    with pytest.raises(WorkbookError, match="路径"):
        SafePackage.open(malicious_xlsx)


def test_rejects_macro_package(macro_xlsx: Path) -> None:
    with pytest.raises(WorkbookError, match="宏"):
        SafePackage.open(macro_xlsx)
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/xlsx/test_package.py -v`

Expected: FAIL because package safety is missing.

- [ ] **Step 3: Implement bounded package loading**

```python
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile
from fis.xlsx.errors import WorkbookError

MAX_ENTRIES = 5000
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ENTRY = 50 * 1024 * 1024


@dataclass
class SafePackage:
    path: Path
    archive: ZipFile

    @classmethod
    def open(cls, path: Path) -> "SafePackage":
        try:
            archive = ZipFile(path)
        except BadZipFile as exc:
            raise WorkbookError("文件不是有效的 XLSX") from exc
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES or sum(item.file_size for item in infos) > MAX_UNCOMPRESSED:
            archive.close(); raise WorkbookError("XLSX 解压后过大")
        for item in infos:
            part = PurePosixPath(item.filename)
            if item.file_size > MAX_ENTRY or part.is_absolute() or ".." in part.parts:
                archive.close(); raise WorkbookError("XLSX 包含不安全路径或超大文件")
        names = {item.filename for item in infos}
        if "xl/vbaProject.bin" in names:
            archive.close(); raise WorkbookError("V1 不支持含宏工作簿")
        return cls(path=path, archive=archive)
```

Define `WorkbookError(code: str, message: str, location: str | None)` and make its string form return the Chinese message. Add helpers `read_xml(part, max_bytes)`, `names`, and context-manager close behavior.

- [ ] **Step 4: Run package tests**

Run: `python -m pytest tests/xlsx/test_package.py -v`

Expected: all safety cases pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/xlsx/errors.py src/fis/xlsx/package.py tests/xlsx/test_package.py
git commit -m "feat: validate XLSX package safety"
```

### Task 3: Parse strict template rows and items

**Files:**
- Create: `src/fis/xlsx/parser.py`
- Test: `tests/xlsx/test_parser.py`

- [ ] **Step 1: Write failing parser tests**

```python
from fis.xlsx.parser import parse_invoice_bundle


def test_parser_groups_multiple_rows_by_shipment(standard_workbook) -> None:
    parsed = parse_invoice_bundle(standard_workbook)
    assert tuple(parsed.shipments) == ("S1", "S2")
    assert [item.source_row for item in parsed.shipments["S1"].items] == [12, 13]
    assert parsed.total_row == 16


def test_parser_rejects_gap_before_total(workbook_with_product_gap) -> None:
    with pytest.raises(WorkbookError) as error:
        parse_invoice_bundle(workbook_with_product_gap)
    assert error.value.code == "TEMPLATE_VERSION_MISMATCH"
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/xlsx/test_parser.py -v`

Expected: FAIL because the parser is missing.

- [ ] **Step 3: Implement parsed models and strict scanning**

```python
@dataclass(frozen=True)
class ParsedItem:
    shipment_id: str
    source_row: int
    cells: dict[str, str]


@dataclass(frozen=True)
class ParsedShipment:
    shipment_id: str
    items: tuple[ParsedItem, ...]


@dataclass(frozen=True)
class ParsedBundle:
    shipments: dict[str, ParsedShipment]
    product_rows: tuple[int, ...]
    separator_row: int
    total_row: int


def scan_rows(cell_text: Callable[[str], str | None], start: int = 12) -> tuple[list[int], int, int]:
    rows: list[int] = []
    row = start
    while cell_text(f"A{row}") and cell_text(f"B{row}"):
        rows.append(row); row += 1
    if not rows or cell_text(f"A{row}") or cell_text(f"B{row}"):
        raise WorkbookError("TEMPLATE_VERSION_MISMATCH", "商品区域无法识别", f"row {row}")
    if (cell_text(f"A{row + 1}") or "").replace(" ", "").upper() != "合计TOTAL":
        raise WorkbookError("TEMPLATE_VERSION_MISMATCH", "缺少合计行", f"A{row + 1}")
    return rows, row, row + 1
```

`parse_invoice_bundle(path)` must resolve shared/inline strings, verify the configured sheet and normalized A11/B11/C11 headers, call `scan_rows`, copy A:Z values without evaluating formulas, preserve each source row number, and retain first-seen Shipment order.

- [ ] **Step 4: Run parser tests**

Run: `python -m pytest tests/xlsx/test_parser.py -v`

Expected: valid fixtures pass; moved header, missing ID, gap, and missing total fixtures produce `TEMPLATE_VERSION_MISMATCH` with cell locations.

- [ ] **Step 5: Commit**

```bash
git add src/fis/xlsx/parser.py tests/xlsx/test_parser.py
git commit -m "feat: parse standard invoice bundles"
```

### Task 4: Classify product images and reject other drawings

**Files:**
- Create: `src/fis/xlsx/drawings.py`
- Test: `tests/xlsx/test_drawings.py`

- [ ] **Step 1: Write failing classification tests**

```python
def test_one_row_high_image_belongs_to_start_row() -> None:
    anchor = Anchor(from_row=11, from_col=2, to_row=12, to_col=3, relationship_id="rId1")
    assert classify_anchor(anchor, product_rows={12}, image_column=3) == ProductImage(12, "rId1")


def test_image_crossing_two_rows_is_blocked() -> None:
    anchor = Anchor(from_row=11, from_col=2, to_row=13, to_col=3, relationship_id="rId1")
    with pytest.raises(WorkbookError) as error:
        classify_anchor(anchor, product_rows={12, 13}, image_column=3)
    assert error.value.code == "AMBIGUOUS_IMAGE"
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/xlsx/test_drawings.py -v`

Expected: FAIL because drawing types are missing.

- [ ] **Step 3: Implement anchor classification**

```python
@dataclass(frozen=True)
class Anchor:
    from_row: int  # zero-based OOXML row
    from_col: int
    to_row: int
    to_col: int
    relationship_id: str


@dataclass(frozen=True)
class ProductImage:
    source_row: int  # one-based worksheet row
    relationship_id: str


def classify_anchor(anchor: Anchor, product_rows: set[int], image_column: int) -> ProductImage:
    source_row = anchor.from_row + 1
    if (source_row not in product_rows or anchor.from_col + 1 != image_column
            or anchor.to_row > anchor.from_row + 1):
        raise WorkbookError("AMBIGUOUS_IMAGE", "图片无法归属到唯一商品行", f"row {source_row}")
    return ProductImage(source_row, anchor.relationship_id)
```

Parse worksheet drawing relationships and reject external targets, non-PNG/JPG/JPEG media, charts, SmartArt, grouped shapes, unknown anchors, duplicate relationship IDs, and orphaned media. Classify images outside the product region as fixed only when their complete anchors lie outside it.

- [ ] **Step 4: Run drawing tests**

Run: `python -m pytest tests/xlsx/test_drawings.py -v`

Expected: supported image tests pass and every unsupported fixture returns its stable error code.

- [ ] **Step 5: Commit**

```bash
git add src/fis/xlsx/drawings.py tests/xlsx/test_drawings.py
git commit -m "feat: classify supported workbook images"
```

### Task 5: Transform rows, references, totals, and formula caches

**Files:**
- Create: `src/fis/xlsx/transform.py`
- Test: `tests/xlsx/test_transform.py`

- [ ] **Step 1: Write failing row-map and total tests**

```python
from decimal import Decimal
from fis.xlsx.transform import build_row_map, compute_totals


def test_row_map_compacts_target_and_moves_footer() -> None:
    mapping = build_row_map(product_rows=(12, 13, 14, 15, 16), kept_rows=(13, 16), total_row=18)
    assert mapping == {13: 12, 16: 13, 17: 14, 18: 15}


def test_totals_sum_only_kept_rows() -> None:
    totals = compute_totals([{"K": "7", "M": "115.5", "N": "0.45", "P": "630.0"}])
    assert totals == {"K": Decimal("7"), "M": Decimal("115.5"),
                      "N": Decimal("0.45"), "P": Decimal("630.0")}
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/xlsx/test_transform.py -v`

Expected: FAIL because transformation helpers are missing.

- [ ] **Step 3: Implement deterministic helpers**

```python
from decimal import Decimal, InvalidOperation


def build_row_map(product_rows: tuple[int, ...], kept_rows: tuple[int, ...], total_row: int) -> dict[int, int]:
    first = product_rows[0]
    mapping = {old: first + index for index, old in enumerate(kept_rows)}
    separator = total_row - 1
    mapping[separator] = first + len(kept_rows)
    mapping[total_row] = first + len(kept_rows) + 1
    return mapping


def compute_totals(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    result = {column: Decimal("0") for column in ("K", "M", "N", "P")}
    for row in rows:
        for column in result:
            try:
                result[column] += Decimal(row[column].strip())
            except (KeyError, InvalidOperation) as exc:
                raise WorkbookError("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column) from exc
    return result
```

Implement `transform_sheet_xml(sheet_xml, parsed, kept_rows, output_values)` to keep rows 1-11, compact selected product rows, preserve one separator and the total row, remove other product rows, set `r` on rows/cells, adjust affected merges/validations/hyperlinks/comments/print areas, write numeric totals at K/M/N/P, mirror K/M/N into G8/G9/G10, and update O10/R10 formulas plus numeric cached values. Set workbook calculation properties to full calculation on load. Reject a reference into a removed row unless it is a configured transform target.

- [ ] **Step 4: Run focused transformation tests**

Run: `python -m pytest tests/xlsx/test_transform.py -v`

Expected: row, total, merge, comment, formula, validation, and rejected-reference cases pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/xlsx/transform.py tests/xlsx/test_transform.py
git commit -m "feat: transform invoice rows and totals"
```

### Task 6: Generate and independently validate Shipment workbooks

**Files:**
- Create: `src/fis/xlsx/generator.py`
- Create: `src/fis/xlsx/validate.py`
- Test: `tests/xlsx/test_generator.py`

- [ ] **Step 1: Write the failing end-to-end golden test**

```python
def test_generation_preserves_target_items_and_images(standard_workbook, tmp_path) -> None:
    request = ShipmentOutput(
        shipment_id="S1", warehouse_code="ONT8", warehouse_address="Ontario",
        channel="Ocean", delivery_date=date(2026, 9, 10), sailing_date=date(2026, 9, 17),
        delivery_warehouse="Yiwu",
    )
    output = generate_shipment_workbook(standard_workbook, request, tmp_path)
    report = validate_generated_workbook(output, expected_shipment="S1", expected_rows=2, expected_images=2)
    assert report.ok
    assert output.name == "S1_ONT8.xlsx"
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/xlsx/test_generator.py -v`

Expected: FAIL because generation orchestration is missing.

- [ ] **Step 3: Implement generation orchestration**

```python
@dataclass(frozen=True)
class ShipmentOutput:
    shipment_id: str
    warehouse_code: str
    warehouse_address: str
    channel: str
    delivery_date: date
    sailing_date: date
    delivery_warehouse: str


def safe_filename(request: ShipmentOutput) -> str:
    clean = re.compile(r"[^A-Za-z0-9._-]+")
    shipment = clean.sub("_", request.shipment_id).strip("._")
    warehouse = clean.sub("_", request.warehouse_code).strip("._")
    if not shipment or not warehouse:
        raise WorkbookError("INVALID_FILENAME", "货件或仓库编码无法生成安全文件名")
    return f"{shipment}_{warehouse}.xlsx"
```

`generate_shipment_workbook` must read through `SafePackage`, parse once, select target rows/images, transform configured XML parts, copy untouched entries with original bytes, omit removed media and relationships, write to a temporary filename, run `validate_generated_workbook`, then rename to the final filename. Refuse duplicate normalized filenames at the batch orchestration layer.

`validate_generated_workbook` must reopen independently, confirm package/relationship integrity, one expected Shipment ID, expected item/image counts, seven logistics values, recomputed totals, comments L11/N11/P11, supported drawing objects, expected merges, and no unexpected changed package parts. Allow only the two configured baseline-invalid list-validation references.

- [ ] **Step 4: Run all OOXML tests**

Run: `python -m pytest tests/xlsx -v`

Expected: all valid/golden fixtures pass; corrupt relationships, wrong image counts, foreign Shipment rows, filename collision, and invalid totals fail with stable errors.

- [ ] **Step 5: Run quality checks and commit**

Run: `python -m ruff check src tests && python -m mypy src && python -m pytest -v`

Expected: all commands exit 0.

```bash
git add src/fis/xlsx tests/xlsx tests/fixtures
git commit -m "feat: generate validated Shipment workbooks"
```

## OOXML engine completion checkpoint

Generate outputs from synthetic fixtures with one and multiple items per Shipment. Open the outputs with an independent parser and Microsoft Excel during the release acceptance phase. Confirm the customer sample remains outside Git and is used only for an authorized local smoke test.
