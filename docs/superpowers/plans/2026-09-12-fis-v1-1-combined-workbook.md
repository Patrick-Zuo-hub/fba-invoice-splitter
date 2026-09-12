# FIS v1.1 Combined Workbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate one validated Excel workbook per processing batch, with one ordered, fidelity-preserving worksheet per Shipment.

**Architecture:** Retain the existing single-Shipment transformation as the validated source for each worksheet, then assemble those in-memory OOXML packages into one workbook by cloning worksheet-private relationship graphs with collision-free part names. Publish once atomically, store one batch-level path, and keep legacy v1 history readable through a schema-v2 migration.

**Tech Stack:** Python 3.12, `zipfile`, `lxml`, SQLite, FastAPI/Jinja2, pytest, Playwright.

---

### Task 1: Define output and worksheet naming

**Files:**
- Create: `src/fis/xlsx/combined.py`
- Create: `tests/xlsx/test_combined.py`

- [ ] **Step 1: Write failing naming tests**

```python
def test_combined_filename_uses_original_stem() -> None:
    assert combined_filename("CC20260903010清关单20260903.xlsx") == "已处理_CC20260903010清关单20260903.xlsx"


def test_next_output_path_uses_first_free_case_insensitive_suffix(tmp_path: Path) -> None:
    (tmp_path / "已处理_invoice.xlsx").write_bytes(b"old")
    (tmp_path / "已处理_INVOICE_2.xlsx").write_bytes(b"old")
    assert next_output_path(tmp_path, "invoice.xlsx").name == "已处理_invoice_3.xlsx"


def test_worksheet_titles_are_legal_ordered_and_unique() -> None:
    requests = (_request("A/B", "ONT:8"), _request("A\\B", "ONT?8"))
    titles = worksheet_titles(requests)
    assert titles[0] == "ONT_8-A_B"
    assert titles[1] == "ONT_8-A_B_2"
    assert all(len(title) <= 31 for title in titles)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `../../.venv/bin/pytest tests/xlsx/test_combined.py -q`  
Expected: collection fails because `fis.xlsx.combined` does not exist.

- [ ] **Step 3: Implement deterministic naming**

Implement these public functions in `combined.py`:

```python
FORBIDDEN_SHEET_CHARACTERS = re.compile(r"[\\[\\]:*?/\\\\]")


def combined_filename(source_filename: str) -> str:
    display = source_filename.replace("\\\\", "/").split("/")[-1]
    stem = Path(display).stem
    stem = "".join(character for character in stem if character.isprintable()).strip(" .'_")
    if not stem:
        stem = "发票"
    return f"已处理_{stem}.xlsx"


def next_output_path(output_dir: Path, source_filename: str) -> Path:
    first = combined_filename(source_filename)
    occupied = {child.name.casefold() for child in output_dir.iterdir()}
    if first.casefold() not in occupied:
        return output_dir / first
    stem = Path(first).stem
    index = 2
    while f"{stem}_{index}.xlsx".casefold() in occupied:
        index += 1
    return output_dir / f"{stem}_{index}.xlsx"


def worksheet_titles(requests: Sequence[ShipmentOutput]) -> tuple[str, ...]:
    occupied: set[str] = set()
    result: list[str] = []
    for request in requests:
        preferred = f"{request.warehouse_code}-{request.shipment_id}"
        base = FORBIDDEN_SHEET_CHARACTERS.sub("_", preferred).strip().strip("'") or "货件"
        base = base[:31]
        candidate = base
        index = 2
        while candidate.casefold() in occupied:
            suffix = f"_{index}"
            candidate = f"{base[:31 - len(suffix)]}{suffix}"
            index += 1
        occupied.add(candidate.casefold())
        result.append(candidate)
    return tuple(result)
```

- [ ] **Step 4: Run the naming tests and verify GREEN**

Run: `../../.venv/bin/pytest tests/xlsx/test_combined.py -q`  
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/fis/xlsx/combined.py tests/xlsx/test_combined.py
git commit -m "feat: add combined workbook naming"
```

### Task 2: Assemble one OOXML workbook from validated Shipment packages

**Files:**
- Modify: `src/fis/xlsx/combined.py`
- Modify: `tests/xlsx/test_combined.py`

- [ ] **Step 1: Write failing assembly tests**

Build a three-Shipment fixture and assert with `ZipFile`/`lxml` that:

```python
payload = assemble_combined_workbook(
    tuple(generate_shipment_workbook_bytes(source, request) for request in requests),
    worksheet_titles(requests),
)
output.write_bytes(payload)
with SafePackage.open(output) as package:
    workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
    assert [node.get("name") for node in workbook.xpath("./main:sheets/main:sheet", namespaces={"main": MAIN_NS})] == [
        "ONT8-S1", "IAH3-S2", "LAX9-S3"
    ]
    assert_combined_relationships_resolve(package)
```

Add assertions that each resolved worksheet contains only its expected Shipment ID, every worksheet has a distinct drawing relationship graph, product images remain Shipment-specific, fixed images are present on every sheet, comments resolve, and defined print areas use the final Sheet names.

- [ ] **Step 2: Run the assembly test and verify RED**

Run: `../../.venv/bin/pytest tests/xlsx/test_combined.py -q`  
Expected: fails because `assemble_combined_workbook` is missing.

- [ ] **Step 3: Implement the assembler**

Add these focused internal units to `combined.py`:

```python
@dataclass(slots=True)
class _PackageParts:
    parts: dict[str, bytes]


def assemble_combined_workbook(payloads: Sequence[bytes], titles: Sequence[str]) -> bytes:
    if not payloads or len(payloads) != len(titles):
        raise WorkbookError("INVALID_COMBINED_INPUT", "合并工作簿输入无效")
    packages = tuple(_read_package(payload) for payload in payloads)
    assembled = _PackageParts(dict(packages[0].parts))
    selected = tuple(_selected_sheet(package) for package in packages)
    clones = tuple(_clone_sheet_graph(assembled, package, sheet, index + 1) for index, (package, sheet) in enumerate(zip(packages, selected, strict=True)))
    _replace_workbook_sheets(assembled, clones, titles)
    _replace_workbook_relationships(assembled, clones)
    _replace_defined_names(assembled, packages, titles)
    _sync_content_types(assembled, packages, clones)
    _assert_no_missing_internal_targets(assembled)
    return _write_package(assembled)
```

`_clone_sheet_graph` must traverse internal relationships from the selected worksheet, allocate a unique destination for every worksheet-private part, copy every part, and rewrite each relative target with `posixpath.relpath`. External relationships remain unchanged. `_replace_workbook_sheets` exposes only the cloned Shipment sheets. `_replace_defined_names` copies target-sheet local names once per new sheet, assigns `localSheetId` by output order, rewrites quoted sheet references, and removes names scoped to hidden source sheets. `_sync_content_types` copies every required override under the cloned part name and preserves extension defaults. `_assert_no_missing_internal_targets` resolves all internal relationships in the assembled package and raises `WorkbookError("COMBINED_RELATIONSHIP_INVALID", ...)` on a missing target.

- [ ] **Step 4: Run the assembly tests and verify GREEN**

Run: `../../.venv/bin/pytest tests/xlsx/test_combined.py -q`  
Expected: all combined tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/xlsx/combined.py tests/xlsx/test_combined.py
git commit -m "feat: assemble multi-shipment workbook"
```

### Task 3: Add validated atomic combined generation

**Files:**
- Modify: `src/fis/xlsx/combined.py`
- Modify: `src/fis/xlsx/generator.py`
- Modify: `src/fis/xlsx/validate.py`
- Modify: `tests/xlsx/test_combined.py`
- Modify: `tests/xlsx/test_generator.py`

- [ ] **Step 1: Write failing public-engine tests**

```python
def test_generate_combined_workbook_publishes_once_and_preserves_order(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S2": 1, "S1": 1}, [12, 13])
    path = generate_combined_workbook(source, (_request("S2", "IAH3"), _request("S1", "ONT8")), tmp_path / "out", "invoice.xlsx")
    assert path.name == "已处理_invoice.xlsx"
    assert combined_sheet_names(path) == ("IAH3-S2", "ONT8-S1")


def test_generate_combined_workbook_uses_suffix_without_overwrite(tmp_path: Path) -> None:
    existing = tmp_path / "out" / "已处理_invoice.xlsx"
    existing.parent.mkdir()
    existing.write_bytes(b"old")
    result = generate_combined_workbook(source, requests, existing.parent, "invoice.xlsx")
    assert result.name == "已处理_invoice_2.xlsx"
    assert existing.read_bytes() == b"old"


def test_failed_combined_validation_leaves_no_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(combined, "validate_combined_workbook", lambda *args, **kwargs: ValidationReport(False, "bad"))
    with pytest.raises(WorkbookError, match="bad"):
        generate_combined_workbook(source, requests, output_dir, "invoice.xlsx")
    assert list(output_dir.glob("已处理_*.xlsx")) == []
```

- [ ] **Step 2: Run and verify RED**

Run: `../../.venv/bin/pytest tests/xlsx/test_combined.py tests/xlsx/test_generator.py -q`  
Expected: failures because the combined public engine and validator do not exist.

- [ ] **Step 3: Implement validation and atomic publication**

Expose this engine contract:

```python
def generate_combined_workbook(
    source: Path | str,
    requests: tuple[ShipmentOutput, ...],
    output_dir: Path | str,
    source_filename: str,
) -> Path:
    destination = Path(output_dir)
    titles = worksheet_titles(requests)
    payloads = tuple(generate_shipment_workbook_bytes(source, request) for request in requests)
    payload = assemble_combined_workbook(payloads, titles)
    output_path = next_output_path(destination, source_filename)
    return _validate_and_publish(payload, output_path, requests, titles)
```

`validate_combined_workbook` must use `SafePackage`, verify workbook title order/count, resolve each worksheet and its internal relationships, verify its Shipment ID and required values, and confirm the configured print area. `_validate_and_publish` writes a named temporary file in `destination`, flushes and `fsync`s it, validates it, then uses `os.replace`; its `finally` block deletes any remaining temporary file.

- [ ] **Step 4: Run focused and XLSX suites**

Run: `../../.venv/bin/pytest tests/xlsx -q`  
Expected: all XLSX tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/xlsx/combined.py src/fis/xlsx/generator.py src/fis/xlsx/validate.py tests/xlsx
git commit -m "feat: publish validated combined workbook"
```

### Task 4: Migrate persistence to one batch-level output path

**Files:**
- Modify: `src/fis/storage/schema.py`
- Modify: `src/fis/storage/database.py`
- Modify: `src/fis/storage/repositories.py`
- Modify: `tests/storage/test_database.py`
- Modify: `tests/storage/test_repositories.py`

- [ ] **Step 1: Write failing migration and history tests**

Create a schema-v1 database, initialize it through current code, and assert it migrates to version 2 with `processing_batches.output_path`. Add repository tests proving a v1.1 completion stores the path once, Shipment rows retain snapshots with `output_path IS NULL`, and legacy v1 per-Shipment paths still render in history.

- [ ] **Step 2: Run and verify RED**

Run: `../../.venv/bin/pytest tests/storage/test_database.py tests/storage/test_repositories.py -q`  
Expected: migration and batch-level output assertions fail.

- [ ] **Step 3: Implement additive schema v2**

```python
SCHEMA_VERSION = 2
MIGRATIONS = {
    1: ("ALTER TABLE processing_batches ADD COLUMN output_path TEXT",),
}
```

Update `Database.initialize()` to apply each migration in one `BEGIN IMMEDIATE` transaction and update the single `schema_version` row after each step. Change `BatchRepository.record_completed(..., output_path: str, shipments: list[tuple[str, str]])` to persist the combined path on the batch and insert Shipment snapshots with a null legacy path. Extend `HistoryEntry` with `output_path`; history and `output_directory()` prefer the batch path and fall back to the first legacy Shipment path.

- [ ] **Step 4: Run storage tests and verify GREEN**

Run: `../../.venv/bin/pytest tests/storage -q`  
Expected: all storage tests pass, including schema-v1 migration.

- [ ] **Step 5: Commit**

```bash
git add src/fis/storage tests/storage
git commit -m "feat: store combined workbook at batch level"
```

### Task 5: Integrate the combined engine into the batch service

**Files:**
- Modify: `src/fis/services/batches.py`
- Modify: `tests/web/test_workspace_generation.py`
- Modify: `tests/web/test_history.py`

- [ ] **Step 1: Replace current expectations with failing v1.1 tests**

Assert generation produces only `已处理_invoice.xlsx`, a second successful batch in the same folder produces `_2.xlsx`, `GenerationResult.filename` is singular, `sheet_count` equals Shipment count, and repository history contains one batch-level path. Keep the engine-failure test and assert it publishes no file or defaults.

- [ ] **Step 2: Run and verify RED**

Run: `../../.venv/bin/pytest tests/web/test_workspace_generation.py tests/web/test_history.py -q`  
Expected: old multi-file behavior fails the new assertions.

- [ ] **Step 3: Update the service contract**

```python
GenerateEngine = Callable[[str, tuple[ShipmentOutput, ...], Path, str], Path]

@dataclass(frozen=True, slots=True)
class GenerationResult:
    batch_id: str
    output_directory: Path
    filename: str
    sheet_count: int
```

Remove per-Shipment safe-filename collision checks. Call the combined engine with `workspace.source_filename`, record the returned path once, preserve Shipment snapshots in workspace order, then store defaults only after persistence succeeds.

- [ ] **Step 4: Run service/web tests and verify GREEN**

Run: `../../.venv/bin/pytest tests/web/test_workspace_generation.py tests/web/test_history.py -q`  
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/services/batches.py tests/web/test_workspace_generation.py tests/web/test_history.py
git commit -m "feat: generate one workbook per batch"
```

### Task 6: Update result, history, and browser copy

**Files:**
- Modify: `src/fis/web/routes/workspace.py`
- Modify: `src/fis/web/templates/_generation_result.html`
- Modify: `src/fis/web/templates/_history.html`
- Modify: `src/fis/web/templates/_shipment_editor.html`
- Modify: `tests/browser/test_single_page_flow.py`
- Modify: `tests/web/test_workspace_generation.py`
- Modify: `tests/web/test_history.py`

- [ ] **Step 1: Write failing response and browser assertions**

Assert the generate button says `生成工作簿`, the success panel includes `已生成 1 个工作簿，共包含 2 个货件 Sheet` and `已处理_source.xlsx`, output contains one file, and history displays that path only once without `S1_ONT8.xlsx`, `S2_IAH3.xlsx`, or ZIP language.

- [ ] **Step 2: Run and verify RED**

Run: `../../.venv/bin/pytest tests/web/test_workspace_generation.py tests/web/test_history.py tests/browser/test_single_page_flow.py -q`  
Expected: copy and old file assertions fail.

- [ ] **Step 3: Update views**

Render `GenerationResult.filename` and `GenerationResult.sheet_count` directly. In history, show `entry.output_path` once for v1.1 entries; show the legacy Shipment list only when the batch-level path is absent. Keep the existing open-folder POST action and security headers unchanged.

- [ ] **Step 4: Run web and browser tests and verify GREEN**

Run: `../../.venv/bin/pytest tests/web tests/browser/test_single_page_flow.py -q`  
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/web tests/web tests/browser/test_single_page_flow.py
git commit -m "feat: present combined workbook results"
```

### Task 7: Validate the real sample and full regression suite

**Files:**
- Modify if required by discovered regression: relevant `src/fis/**` and matching test
- Update: `docs/superpowers/specs/2026-09-12-fis-v1-1-combined-workbook-design.md` only if implementation-level clarification is necessary

- [ ] **Step 1: Run formatting and static checks**

Run: `../../.venv/bin/ruff check src tests`  
Expected: no errors.

Run: `../../.venv/bin/mypy src/fis`  
Expected: success with no issues.

- [ ] **Step 2: Run the complete automated suite**

Run: `../../.venv/bin/pytest -q`  
Expected: all tests pass with only explicitly marked skips.

- [ ] **Step 3: Generate from the preserved real sample**

Use `samples/CC20260903010清关单20260903.xlsx`, supply valid values for every parsed Shipment, and generate into a temporary directory. Assert one file named `已处理_CC20260903010清关单20260903.xlsx`, Sheet count equals parsed Shipment count, every title is unique/legal, and combined validation passes.

- [ ] **Step 4: Inspect package and workbook behavior**

Open the result through the available spreadsheet application or macOS preview workflow and verify Sheet order, visible names, representative images, comments, totals/formulas, merged cells, and print areas. Record any application repair warning as a release blocker.

- [ ] **Step 5: Commit any verification-only test additions**

```bash
git add tests docs/superpowers/specs/2026-09-12-fis-v1-1-combined-workbook-design.md
git commit -m "test: verify FIS v1.1 combined workbook"
```

Do not push. After this plan is complete and reviewed, begin the separate Windows portable-launcher design and plan.
