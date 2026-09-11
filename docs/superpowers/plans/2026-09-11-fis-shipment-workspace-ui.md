# FIS Shipment Workspace UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the batch-first page with the approved per-shipment workspace, complete the three missing local-Web acceptance flows (primary flow, error recovery, and automatic shutdown), and defer portable packaging.

**Architecture:** Keep FastAPI, Jinja fragments, process-local workspace state, SQLite master/history data, and the existing XLSX engine. Add presentation-ready shipment view models and narrow JSON mutation routes so each cell auto-saves independently, while bulk operations reuse the same service merge rules. Keep browser JavaScript limited to local requests, inline editing, popovers, drawers, fragment replacement, and heartbeat lifecycle.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLite, plain JavaScript/CSS, pytest, Playwright

---

## Scope guard

- Do not modify the frozen PRD repository.
- Do not build PyInstaller artifacts or portable Windows/macOS packages in this plan.
- Preserve the existing XLSX parser/generator contract unless a failing acceptance test proves a defect.
- Treat the three uncommitted UI files present before this plan as in-progress work; replace or extend them without discarding unrelated repository changes.
- Use the approved design in `docs/superpowers/specs/2026-09-11-fis-v1-1-shipment-workspace-ui-design.md` as the UI baseline.

## File map

| Path | Responsibility |
| --- | --- |
| `src/fis/domain/rules.py` | Parse the approved `Warehouse Code + Shipment ID` paste format. |
| `src/fis/services/batches.py` | Shipment summaries, effective values, row edits, inline warehouse save, completeness, and last-successful output folder. |
| `src/fis/storage/repositories.py` | Active master-data choices and successful output-folder default. |
| `src/fis/web/routes/workspace.py` | Upload, mapping, cell-save, bulk-save, inline master save, folder, and generation fragments. |
| `src/fis/web/templates/workspace.html` | Single-page application shell, header, drawers, and initial upload state. |
| `src/fis/web/templates/_invoice_summary.html` | Collapsed file bar, mapping region, shipment table, and sticky action bar. |
| `src/fis/web/templates/_shipment_editor.html` | Reusable shipment-table fragment with ten approved columns. |
| `src/fis/web/templates/_output_folder.html` | Output-folder state inside the fixed action bar. |
| `src/fis/web/templates/_generation_result.html` | In-place success state and open-folder action. |
| `src/fis/web/templates/_history.html` | History drawer contents. |
| `src/fis/web/templates/_masters.html` | Master Data drawer contents and maintenance forms. |
| `src/fis/web/static/app.js` | Auto-save, batch popovers, mapping disclosure, product expansion, drawers, fragment swaps, and heartbeat. |
| `src/fis/web/static/app.css` | Approved clean blue/white desktop workspace and sticky table/footer behavior. |
| `tests/web/` | Service/route/template contract tests. |
| `tests/browser/` | Primary, error-recovery, and shutdown acceptance flows. |

### Task 1: Pin shipment summaries and approved mapping order

**Files:**
- Modify: `tests/fixtures/standard_v1_builder.py`
- Modify: `tests/domain/test_rules.py`
- Modify: `tests/web/test_workspace_mapping.py`
- Modify: `tests/web/test_workspace_upload.py`
- Modify: `src/fis/domain/rules.py`
- Modify: `src/fis/services/batches.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Add failing tests for mapping order and shipment summaries**

Add tests proving that `ONT8\tS1` maps S1 to ONT8, an optional `仓库代码\t货件单号` header is ignored, duplicate Shipment IDs with conflicting codes are rejected, total cartons sum numeric column K for all items in one shipment, and Chinese names are de-duplicated from the confirmed fixture column.

The service-level assertion is:

```python
view = service.editor_view(workspace)
row = view["rows"][0]
assert row["shipment_id"] == "S1"
assert row["carton_count"] == 3
assert row["product_names"] == ("收纳箱", "置物架", "鞋盒")
assert row["product_summary"] == "收纳箱、置物架，等 3 种产品"
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/domain/test_rules.py tests/web/test_workspace_mapping.py tests/web/test_workspace_upload.py -v`

Expected: failures show the parser still expects Shipment ID first and the editor view lacks carton/name summaries.

- [ ] **Step 3: Implement the minimal mapping and summary behavior**

`parse_warehouse_mapping()` must return `{shipment_id: normalized_code}` while reading each data row as `code, shipment_id`. `BatchService.editor_view()` must calculate carton totals from column K using `Decimal`, ignore blank/invalid presentation values without changing XLSX validation, and derive unique product names from the fixture-backed product-name column in first-seen order.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/domain/test_rules.py tests/web/test_workspace_mapping.py tests/web/test_workspace_upload.py -v`

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/__init__.py tests/fixtures/standard_v1_builder.py tests/domain/test_rules.py \
  tests/web/test_workspace_mapping.py tests/web/test_workspace_upload.py \
  src/fis/domain/rules.py src/fis/services/batches.py
git commit -m "feat: summarize shipments for workspace"
```

### Task 2: Add per-cell auto-save, batch edits, and inline FBA address creation

**Files:**
- Modify: `tests/web/test_workspace_edits.py`
- Modify: `tests/web/test_masters.py`
- Modify: `src/fis/services/batches.py`
- Modify: `src/fis/web/routes/workspace.py`
- Modify: `src/fis/storage/repositories.py`

- [ ] **Step 1: Add failing route and service tests**

Cover one-field row edits for warehouse code, channel, delivery date, sailing date, and delivery warehouse; a bulk edit applied to all rows; manual sailing-date lock; selectors containing only active master records; known-code address resolution; unknown-code inline address creation; and refusal to overwrite an existing FBA address from the shipment page.

Use the cell-save contract:

```python
response = client.patch(
    f"/workspace/{batch_id}/shipments/S1",
    headers=mutation_headers(),
    json={"field": "channel", "value": "美森正班"},
)
assert response.status_code == 200
assert response.json()["save_state"] == "saved"
assert response.json()["row"]["fields"]["channel"] == "美森正班"
```

Use the bulk contract:

```python
response = client.post(
    f"/workspace/{batch_id}/bulk",
    headers=mutation_headers(),
    json={"field": "delivery_date", "value": "2026-09-17"},
)
assert response.status_code == 200
assert response.json()["updated"] == 3
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/web/test_workspace_edits.py tests/web/test_masters.py -v`

Expected: 404 for the new cell/bulk/inline-master routes and missing selector/completeness data.

- [ ] **Step 3: Implement narrow mutation commands**

Allow only the six editable row fields and only the four approved bulk fields. Normalize values through the existing `_merge_scope()` path. Return JSON containing the updated row, `complete_count`, `total_count`, and `save_state`. Add `save_inline_fba_warehouse(batch_id, shipment_id, address)` which requires a nonblank unknown code, creates the master record, and rejects an existing code without changing its address.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/web/test_workspace_edits.py tests/web/test_masters.py -v`

Expected: all edit and master tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/web/test_workspace_edits.py tests/web/test_masters.py \
  src/fis/services/batches.py src/fis/web/routes/workspace.py \
  src/fis/storage/repositories.py
git commit -m "feat: auto-save shipment workspace fields"
```

### Task 3: Remember only the last successfully generated output folder

**Files:**
- Modify: `tests/web/test_workspace_edits.py`
- Modify: `tests/web/test_workspace_generation.py`
- Modify: `src/fis/services/batches.py`
- Modify: `src/fis/web/routes/workspace.py`
- Modify: `src/fis/web/templates/_output_folder.html`

- [ ] **Step 1: Add failing output-folder history tests**

Prove first use has no folder, choosing a folder without generation does not persist it, successful generation persists it, a later upload pre-fills it only when the path exists and is writable, and a missing/unwritable prior path is ignored.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/web/test_workspace_edits.py tests/web/test_workspace_generation.py -v`

Expected: later workspaces do not yet restore the successful output path.

- [ ] **Step 3: Implement success-gated folder memory**

Use the key `last_successful_output_directory`. Read it when a workspace is created, validate it as an existing writable directory, and assign it only then. Write it exclusively after the XLSX engine and history transaction succeed. Do not update it in `choose_output_folder()`.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/web/test_workspace_edits.py tests/web/test_workspace_generation.py -v`

Expected: all output-folder tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/web/test_workspace_edits.py tests/web/test_workspace_generation.py \
  src/fis/services/batches.py src/fis/web/routes/workspace.py \
  src/fis/web/templates/_output_folder.html
git commit -m "feat: remember successful output folder"
```

### Task 4: Build the approved single-page workspace UI

**Files:**
- Modify: `tests/web/test_runtime_page.py`
- Modify: `tests/web/test_workspace_upload.py`
- Modify: `src/fis/web/templates/workspace.html`
- Modify: `src/fis/web/templates/_invoice_summary.html`
- Modify: `src/fis/web/templates/_shipment_editor.html`
- Modify: `src/fis/web/templates/_generation_result.html`
- Modify: `src/fis/web/templates/_history.html`
- Modify: `src/fis/web/templates/_masters.html`
- Modify: `src/fis/web/static/app.js`
- Modify: `src/fis/web/static/app.css`

- [ ] **Step 1: Add failing markup and static-behavior contract tests**

Assert the shell has the FIS header and drawer triggers; the uploaded workspace has collapsed file/mapping bars, all ten table columns, four header-level batch actions, no row selection checkboxes, the fixed output action bar, an auto-save live region, and first-three-column sticky classes. Assert JavaScript contains delegated cell-save, batch-popover, product expansion, drawer, scroll restoration, and heartbeat behavior.

- [ ] **Step 2: Run UI contract tests and confirm RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/web/test_runtime_page.py tests/web/test_workspace_upload.py -v`

Expected: failures identify the old stacked form layout and missing approved controls.

- [ ] **Step 3: Implement the Jinja, JavaScript, and CSS UI**

Render the ten-column table from the editor view. Use semantic buttons and labels; click-to-edit controls stay visually compact. Auto-save on `change` or `blur`, show `保存中/已保存/保存失败`, and patch only the changed row. Batch popovers sit beside their column headings and require confirmation before applying to all shipments. Product summaries expand in their row. History and Master Data open in right drawers without losing table scroll. CSS must implement the approved blue/white palette, sticky header/first three columns, horizontal table scrolling, orange incomplete state, red error state, and fixed bottom bar.

- [ ] **Step 4: Run UI tests and static checks**

Run: `PYTHONPATH=. .venv/bin/pytest tests/web/test_runtime_page.py tests/web/test_workspace_upload.py -v`

Run: `node --check src/fis/web/static/app.js`

Expected: tests pass and JavaScript syntax check exits 0.

- [ ] **Step 5: Commit**

```bash
git add tests/web/test_runtime_page.py tests/web/test_workspace_upload.py \
  src/fis/web/templates src/fis/web/static/app.js src/fis/web/static/app.css
git commit -m "feat: build shipment workspace interface"
```

### Task 5: Complete the three local-Web acceptance flows

**Files:**
- Create: `tests/browser/__init__.py`
- Create: `tests/browser/conftest.py`
- Create: `tests/browser/test_single_page_flow.py`
- Create: `tests/browser/test_single_page_errors.py`
- Create: `tests/browser/test_shutdown_flow.py`
- Modify only if a failing test requires it: `src/fis/runtime/server.py`
- Modify only if a failing test requires it: `src/fis/runtime/lifecycle.py`
- Modify only if a failing test requires it: `src/fis/web/static/app.js`
- Modify only if a failing test requires it: `src/fis/web/routes/workspace.py`

- [ ] **Step 1: Add the missing real-browser tests**

The primary flow uploads a synthetic workbook, maps warehouse codes in approved order, edits one row, applies one batch value, selects an output folder through a fake picker, generates N invoices, opens History and Master Data drawers, and observes only loopback network requests.

The error flow uploads an invalid file, repairs an incomplete mapping, verifies an unknown warehouse can be saved inline, and confirms field errors do not erase other row values.

The shutdown flow proves an immediate heartbeat, page-close removal, 30-second grace, refresh recovery, and active-generation delay using the tested lifecycle seam rather than waiting 30 wall-clock seconds.

- [ ] **Step 2: Run browser tests and confirm RED where behavior is incomplete**

Run: `PYTHONPATH=. .venv/bin/pytest tests/browser -v`

Expected: any remaining selector, response, or lifecycle gap fails with a user-visible assertion.

- [ ] **Step 3: Implement only behavior required by the failing acceptance tests**

Keep the server loopback-only, preserve the exact Origin and `X-FIS-Request` checks, and do not add login, cloud access, CORS, or packaging code.

- [ ] **Step 4: Run browser and local-Web regression suites**

Run: `PYTHONPATH=. .venv/bin/pytest tests/browser tests/web tests/runtime tests/platform -v`

Expected: primary flow, error flow, shutdown flow, web security, runtime, and folder tests all pass. Socket tests must run outside the restricted sandbox when required.

- [ ] **Step 5: Commit**

```bash
git add tests/browser src/fis/runtime src/fis/web
git commit -m "test: accept local web workflow"
```

### Task 6: Full verification without portable packaging

**Files:**
- Modify only for verified defects found by the full checks.

- [ ] **Step 1: Run quality checks**

Run: `.venv/bin/ruff check src tests`

Run: `.venv/bin/mypy src`

Run: `PYTHONPATH=. .venv/bin/pytest -v`

Expected: all commands exit 0; only explicitly platform-specific tests may skip.

- [ ] **Step 2: Run the local application smoke check**

Start `python -m fis` with a temporary data root, confirm the first available port in `8765–8775`, open the workspace, and verify no request targets a non-loopback host. Close the last page and confirm automatic shutdown after the configured grace period.

- [ ] **Step 3: Verify repository boundaries**

Confirm the frozen PRD repository has no changes, no production workbook or generated invoice was added, and the diff contains no portable-build output.

- [ ] **Step 4: Complete the development branch workflow**

Use `superpowers:finishing-a-development-branch` after fresh verification evidence is available.
