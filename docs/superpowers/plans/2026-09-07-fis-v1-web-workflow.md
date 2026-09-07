# FIS v1 Local Web Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the no-login, three-step browser workflow plus successful History and three Master Data management pages on top of the tested foundation and OOXML engine.

**Architecture:** Use a FastAPI application factory with injected settings, repositories, and generation service. Render Jinja templates on the server and use a vendored HTMX file only for small partial-page updates. Every mutation uses the launch session and CSRF token.

**Tech Stack:** FastAPI, Jinja2, HTMX, minimal JavaScript, SQLite, pytest, HTTPX, Playwright

---

## File map

| Path | Responsibility |
| --- | --- |
| `src/fis/web/app.py` | FastAPI application factory and dependencies |
| `src/fis/web/security.py` | Launch session, Host, Origin, and CSRF protection |
| `src/fis/web/routes/process.py` | Upload, parse, confirm, generate, and download routes |
| `src/fis/web/routes/history.py` | Completed history, downloads, folder open, deletion |
| `src/fis/web/routes/masters.py` | Three Master Data screens and mutations |
| `src/fis/services/batches.py` | Transactional application workflow |
| `src/fis/web/templates/` | Page and partial templates |
| `src/fis/web/static/` | Bundled CSS, HTMX, and minimal browser code |
| `tests/web/` | HTTP behavior tests |
| `tests/browser/` | User-flow browser tests |

### Task 1: Create a protected local FastAPI application

**Files:**
- Create: `src/fis/web/__init__.py`
- Create: `src/fis/web/app.py`
- Create: `src/fis/web/security.py`
- Create: `src/fis/web/templates/base.html`
- Create: `src/fis/web/static/app.css`
- Test: `tests/web/test_app.py`
- Test: `tests/web/test_security.py`

- [ ] **Step 1: Write failing health and Host tests**

```python
from fastapi.testclient import TestClient
from fis.web.app import create_app


def test_health_is_available_on_loopback(settings) -> None:
    client = TestClient(create_app(settings, launch_secret="secret"))
    assert client.get("/health", headers={"host": "127.0.0.1"}).json() == {"status": "ok"}


def test_unexpected_host_is_rejected(settings) -> None:
    client = TestClient(create_app(settings, launch_secret="secret"))
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 400
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/web/test_app.py tests/web/test_security.py -v`

Expected: FAIL because the web package is missing.

- [ ] **Step 3: Implement the application factory and middleware**

```python
from fastapi import FastAPI
from fis.config import Settings
from fis.web.security import LocalRequestMiddleware


def create_app(settings: Settings, launch_secret: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.launch_secret = launch_secret
    app.add_middleware(LocalRequestMiddleware, launch_secret=launch_secret)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
```

`LocalRequestMiddleware` accepts only Host values `127.0.0.1:<port>` and `localhost:<port>`. The first `GET /launch?secret=<secret>` compares with `secrets.compare_digest`, sets an HttpOnly `SameSite=Strict` session cookie, redirects to `/`, and removes the secret from browser history. State-changing requests require the cookie, a matching CSRF form/header token, and a loopback Origin. `/health` contains no data and is the only unauthenticated route.

- [ ] **Step 4: Run web tests**

Run: `python -m pytest tests/web/test_app.py tests/web/test_security.py -v`

Expected: health, launch exchange, missing session, bad Host, bad Origin, and bad CSRF tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/web tests/web/test_app.py tests/web/test_security.py
git commit -m "feat: add protected local web application"
```

### Task 2: Orchestrate safe upload and parsing

**Files:**
- Create: `src/fis/services/__init__.py`
- Create: `src/fis/services/batches.py`
- Create: `src/fis/web/routes/process.py`
- Create: `src/fis/web/templates/process/upload.html`
- Create: `src/fis/web/templates/process/confirm.html`
- Test: `tests/web/test_upload.py`

- [ ] **Step 1: Write failing upload tests**

```python
def test_upload_copies_source_and_redirects_to_confirmation(client, standard_workbook_bytes) -> None:
    response = client.post(
        "/batches", files={"invoice": ("invoice.xlsx", standard_workbook_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"csrf_token": client.csrf_token}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/batches/")


def test_upload_rejects_non_xlsx(client) -> None:
    response = client.post("/batches", files={"invoice": ("bad.txt", b"text", "text/plain")},
                           data={"csrf_token": client.csrf_token})
    assert response.status_code == 422
    assert "仅支持 .xlsx" in response.text
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/web/test_upload.py -v`

Expected: FAIL because batch routes and service are missing.

- [ ] **Step 3: Implement streamed upload and parse orchestration**

```python
class BatchService:
    def create_from_upload(self, filename: str, chunks: Iterable[bytes]) -> str:
        if Path(filename).suffix.lower() != ".xlsx":
            raise UserInputError("仅支持 .xlsx 文件")
        batch_id = str(uuid4())
        work = self.files.create(batch_id)
        source_dir = work.parent / "source"; source_dir.mkdir()
        source = source_dir / safe_upload_name(filename)
        digest = hashlib.sha256(); size = 0
        with source.open("xb") as target:
            for chunk in chunks:
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise UserInputError("文件不能超过 50 MB")
                digest.update(chunk); target.write(chunk)
        self.batches.create(batch_id, filename, digest.hexdigest(),
                            str(source.relative_to(self.files.batches_root.parent)), self.clock())
        parsed = parse_invoice_bundle(source)
        self.save_parsed_batch(batch_id, parsed)
        self.batches.set_state(batch_id, BatchState.AWAITING_CONFIRMATION, self.clock())
        return batch_id
```

Mount the process router in `create_app`. `POST /batches` streams the upload to `BatchService`; `GET /batches/{id}` renders counts, Shipment rows, mapping input, defaults, and locatable errors. Never accept a client-supplied filesystem path.

- [ ] **Step 4: Run upload tests**

Run: `python -m pytest tests/web/test_upload.py -v`

Expected: valid upload, invalid extension, oversize, malformed ZIP, and template mismatch cases pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/services src/fis/web/routes/process.py src/fis/web/templates/process tests/web/test_upload.py
git commit -m "feat: add invoice upload and confirmation"
```

### Task 3: Add mapping, defaults, batch edits, and validation

**Files:**
- Modify: `src/fis/services/batches.py`
- Modify: `src/fis/web/routes/process.py`
- Create: `src/fis/web/templates/process/_shipments.html`
- Test: `tests/web/test_confirmation.py`

- [ ] **Step 1: Write failing confirmation tests**

```python
def test_batch_channel_then_row_override(client, awaiting_batch) -> None:
    response = client.post(
        f"/batches/{awaiting_batch}/fields",
        data={"csrf_token": client.csrf_token, "channel_all": "Ocean",
              "shipment.S2.channel": "Air"},
    )
    assert response.status_code == 200
    assert response.text.count("Ocean") >= 1
    assert "Air" in response.text


def test_delivery_change_does_not_replace_manual_sailing_date(client, awaiting_batch) -> None:
    client.post(f"/batches/{awaiting_batch}/shipments/S1",
                data={"csrf_token": client.csrf_token, "sailing_date": "2026-10-01"})
    response = client.post(f"/batches/{awaiting_batch}/shipments/S1",
                           data={"csrf_token": client.csrf_token, "delivery_date": "2026-09-17"})
    assert "2026-10-01" in response.text
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/web/test_confirmation.py -v`

Expected: FAIL because edit routes are missing.

- [ ] **Step 3: Implement validated confirmation commands**

```python
@dataclass(frozen=True)
class ShipmentEdit:
    warehouse_code: str | None = None
    warehouse_address_for_new_code: str | None = None
    channel_id: int | None = None
    delivery_date: date | None = None
    sailing_date: date | None = None
    delivery_warehouse_id: int | None = None


def apply_dates(current: EditableShipment, edit: ShipmentEdit) -> EditableShipment:
    if edit.sailing_date is not None:
        return replace(current, sailing_date=edit.sailing_date, sailing_manual=True)
    if edit.delivery_date is not None and not current.sailing_manual:
        return replace(current, delivery_date=edit.delivery_date,
                       sailing_date=edit.delivery_date + timedelta(days=7))
    return replace(current, delivery_date=edit.delivery_date or current.delivery_date)
```

Add service commands for parsing pasted mapping, applying each of four batch-set fields before explicit row overrides, creating a new FBA warehouse only when the code does not exist, and returning field errors keyed by Shipment ID and field. `validate_ready(batch_id)` returns all missing/conflicting fields and never stops at the first error.

- [ ] **Step 4: Run confirmation tests**

Run: `python -m pytest tests/web/test_confirmation.py -v`

Expected: mapping headers/blanks, duplicate/conflicting IDs, missing mappings, recent-channel defaults, four batch fields, row overrides, manual dates, and new/existing warehouse rules pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/services/batches.py src/fis/web/routes/process.py src/fis/web/templates/process tests/web/test_confirmation.py
git commit -m "feat: confirm Shipment logistics data"
```

### Task 4: Generate, publish, and download a complete batch

**Files:**
- Modify: `src/fis/services/batches.py`
- Modify: `src/fis/web/routes/process.py`
- Create: `src/fis/web/templates/process/result.html`
- Test: `tests/web/test_generation.py`

- [ ] **Step 1: Write failing atomic-generation tests**

```python
def test_success_publishes_all_invoices_and_zip(client, ready_batch) -> None:
    response = client.post(f"/batches/{ready_batch}/generate",
                           data={"csrf_token": client.csrf_token})
    assert response.status_code == 200
    assert "下载全部 ZIP" in response.text
    assert client.batch_repository.get(ready_batch).state is BatchState.COMPLETED


def test_one_failure_publishes_nothing(client, ready_batch, failing_generator) -> None:
    response = client.post(f"/batches/{ready_batch}/generate",
                           data={"csrf_token": client.csrf_token})
    assert response.status_code == 422
    assert client.batch_repository.get(ready_batch).state is BatchState.INCOMPLETE
    assert not client.batch_files.output_path(ready_batch).exists()
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/web/test_generation.py -v`

Expected: FAIL because generation workflow is missing.

- [ ] **Step 3: Implement the generation transaction**

Add repository compare-and-set and completion methods so duplicate Generate requests cannot both acquire the batch:

```python
def compare_and_set(self, batch_id: str, expected: BatchState, new: BatchState) -> None:
    with self.database.transaction() as connection:
        cursor = connection.execute(
            "UPDATE processing_batches SET state = ? WHERE id = ? AND state = ?",
            (new.value, batch_id, expected.value),
        )
        if cursor.rowcount != 1:
            raise BatchStateError("该批次正在处理或已经生成")

def complete(self, connection: sqlite3.Connection, batch_id: str,
             output: Path, zip_name: str, now: datetime) -> None:
    connection.execute(
        """UPDATE processing_batches SET state = ?, zip_path = ?, completed_at = ?,
           updated_at = ?, error_code = NULL, error_detail = NULL WHERE id = ?""",
        (BatchState.COMPLETED.value, str((output / zip_name).name),
         now.isoformat(), now.isoformat(), batch_id),
    )
```

```python
def generate(self, batch_id: str) -> GenerationResult:
    batch = self.require_ready(batch_id)
    self.batches.compare_and_set(batch_id, BatchState.AWAITING_CONFIRMATION, BatchState.GENERATING)
    try:
        output_names = self.generator.generate_all(batch, self.files.work_path(batch_id))
        zip_name = create_batch_zip(self.files.work_path(batch_id), output_names)
        output = self.files.publish(batch_id)
        with self.database.transaction() as connection:
            self.batches.complete(connection, batch_id, output, zip_name, self.clock())
            for shipment in batch.shipments:
                connection.execute(
                    "UPDATE fba_warehouses SET last_successful_channel_id = ? WHERE code = ?",
                    (shipment.channel_id, shipment.warehouse_code),
                )
        return GenerationResult(batch_id, output_names, zip_name)
    except Exception as error:
        self.files.clear_work(batch_id)
        self.files.clear_output(batch_id)
        self.batches.set_state(batch_id, BatchState.INCOMPLETE, self.clock(),
                               error_code=error_code(error), error_detail=user_message(error))
        raise
```

`POST /batches/{id}/generate` disables duplicate actions through compare-and-set state. Download routes look up relative paths by batch/Shipment ID and serve only completed outputs with attachment headers. The ZIP writer receives an explicit filename list and never recurses through a directory.

- [ ] **Step 4: Run generation tests**

Run: `python -m pytest tests/web/test_generation.py -v`

Expected: success, incomplete output, duplicate click, filename collision, retry, individual download, and ZIP membership tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/services/batches.py src/fis/web/routes/process.py src/fis/web/templates/process/result.html tests/web/test_generation.py
git commit -m "feat: generate and download complete batches"
```

### Task 5: Add History and Master Data pages

**Files:**
- Create: `src/fis/web/routes/history.py`
- Create: `src/fis/web/routes/masters.py`
- Create: `src/fis/web/templates/history/index.html`
- Create: `src/fis/web/templates/history/detail.html`
- Create: `src/fis/web/templates/masters/index.html`
- Test: `tests/web/test_history.py`
- Test: `tests/web/test_masters.py`

- [ ] **Step 1: Write failing page tests**

```python
def test_history_excludes_incomplete_batches(client, completed_batch, incomplete_batch) -> None:
    response = client.get("/history")
    assert completed_batch in response.text
    assert incomplete_batch not in response.text


def test_delete_history_requires_confirmation(client, completed_batch) -> None:
    response = client.post(f"/history/{completed_batch}/delete",
                           data={"csrf_token": client.csrf_token, "confirmation": ""})
    assert response.status_code == 422
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/web/test_history.py tests/web/test_masters.py -v`

Expected: FAIL because pages are missing.

- [ ] **Step 3: Implement routes and templates**

History list queries only completed batches. Detail uses saved Shipment snapshots. Deletion requires the exact batch source filename as confirmation, performs database and filesystem deletion under the batch lock, and reports partial failure without pretending success. The open-folder action resolves the completed output directory and delegates only that validated path to the platform adapter.

Master routes implement create, edit, sort, activate, and deactivate for all three masters. The confirmation workflow may add only a new FBA code; editing an existing FBA address occurs only on the Master Data page. Reject blank names/addresses and case-insensitive duplicate active names.

- [ ] **Step 4: Run page tests**

Run: `python -m pytest tests/web/test_history.py tests/web/test_masters.py -v`

Expected: history filtering/download/delete and all master-data rules pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/web/routes src/fis/web/templates/history src/fis/web/templates/masters tests/web/test_history.py tests/web/test_masters.py
git commit -m "feat: add history and master data pages"
```

### Task 6: Complete the three-step browser experience

**Files:**
- Modify: `src/fis/web/templates/base.html`
- Modify: `src/fis/web/templates/process/`
- Modify: `src/fis/web/static/app.css`
- Create: `src/fis/web/static/htmx.min.js`
- Create: `src/fis/web/static/app.js`
- Create: `tests/browser/test_primary_flow.py`
- Create: `tests/browser/test_error_flow.py`

- [ ] **Step 1: Write failing Playwright flows**

```python
def test_primary_flow(page, live_server, standard_workbook_path) -> None:
    page.goto(live_server.launch_url)
    page.set_input_files("input[name=invoice]", standard_workbook_path)
    page.get_by_role("button", name="继续").click()
    page.get_by_label("Shipment 与 FBA 仓库映射").fill("S1\tONT8\nS2\tLAS1")
    page.get_by_role("button", name="解析映射").click()
    page.get_by_role("button", name=re.compile("生成 2 份发票")).click()
    expect(page.get_by_text("已成功生成")).to_be_visible()
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/browser/test_primary_flow.py -v`

Expected: FAIL at the first missing UI control or route.

- [ ] **Step 3: Implement accessible templates and minimal browser code**

Use one `<main>` heading, explicit `<label>` elements, table row headers for Shipment IDs, visible focus states, and a persistent error summary linking to invalid fields. The upload page contains one file control. Confirmation shows parsed counts, mapping textarea, batch controls, Shipment rows, and one generate button. Result shows individual files, Download All, Open Folder, Process Another, and Exit FIS.

Vendor a pinned HTMX release into `src/fis/web/static/htmx.min.js`; record its version and SHA-256 in `src/fis/web/static/THIRD_PARTY.md`. `app.js` only handles drag/drop presentation and the delete-confirmation dialog; business state remains server-owned.

- [ ] **Step 4: Run browser and HTTP suites**

Run: `python -m pytest tests/web tests/browser -v`

Expected: primary, blocking-error, missing-input, retry, History, and Master Data flows pass at desktop and 1280×720 viewports.

- [ ] **Step 5: Run complete quality checks and commit**

Run: `python -m ruff check src tests && python -m mypy src && python -m pytest -v`

Expected: all commands exit 0.

```bash
git add src/fis/web tests/web tests/browser
git commit -m "feat: complete local FIS web workflow"
```

## Web workflow completion checkpoint

Start the app with a temporary data root, complete one two-Shipment synthetic batch in a real browser, redownload it from History, maintain each master type, and delete the batch. Verify the browser network log contains no request to a non-loopback host.
