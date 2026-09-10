# FIS V1 Simplified Local Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a one-page, no-login local browser tool that starts on ports 8765–8775, persists local reference/history data, generates invoices into a user-selected folder, and exits automatically when use ends.

**Architecture:** Keep the completed XLSX engine independent and place a small FastAPI application-service layer above it. The launcher owns single-instance coordination, loopback socket binding, browser opening, native folder selection, page-heartbeat shutdown, and cleanup; the HTTP layer enforces exact parsed-ASGI Host/Origin/request-header rules without sessions or CSRF tokens. Uvicorn/h11 owns raw HTTP parsing, including legal OWS normalization and malformed-request rejection.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Jinja2, minimal browser JavaScript, SQLite, filelock, pytest, HTTPX, Playwright

---

## Plan status and replacement rule

This plan supersedes `docs/superpowers/plans/2026-09-07-fis-v1-web-workflow.md` in full. Do not execute unfinished tasks from that file.

Commit `08b4386` is not a completed Web Task 1 baseline. Its files remain normal Git history, but Task 1 below replaces launch-secret, session-cookie, and CSRF-token behavior through ordinary forward edits. Do not rewrite or delete Git history.

The completed foundation and XLSX engine through commit `03c5b69` remain the integration baseline. Web work must not edit the frozen PRD repository or store the real customer workbook in source control.

## File map

| Path | Responsibility |
| --- | --- |
| `src/fis/web/app.py` | FastAPI factory, dependency wiring, and router mounting |
| `src/fis/web/security.py` | Exact bound Host, Origin, method, and FIS-header boundary |
| `src/fis/runtime/instance.py` | Per-user lock and live-instance port record |
| `src/fis/runtime/server.py` | Ordered port binding, Uvicorn lifecycle, and browser opening |
| `src/fis/runtime/lifecycle.py` | Page heartbeat, active-job count, and graceful shutdown decision |
| `src/fis/platform/folders.py` | Windows/macOS native output-folder picker and folder opener |
| `src/fis/services/batches.py` | Upload, parse, edit, validate, generate, history, and cleanup orchestration |
| `src/fis/web/routes/workspace.py` | One-page upload, edit, folder, generate, heartbeat, and result routes |
| `src/fis/web/routes/history.py` | History queries and safe open-folder actions |
| `src/fis/web/routes/masters.py` | Warehouse/channel master-data mutations |
| `src/fis/web/templates/workspace.html` | Single-page application shell |
| `src/fis/web/templates/_*.html` | Server-rendered sections returned after actions |
| `src/fis/web/static/app.js` | Custom request header, heartbeat, and progressive section updates |
| `src/fis/web/static/app.css` | Accessible compact single-page styling |
| `tests/web/` | Request, service, persistence, and lifecycle behavior |
| `tests/browser/` | Real browser primary and error flows |

### Task 1: Replace the web session layer with the exact local request boundary

**Files:**
- Modify: `src/fis/web/app.py`
- Modify: `src/fis/web/security.py`
- Modify: `src/fis/web/templates/base.html`
- Modify: `tests/web/test_app.py`
- Modify: `tests/web/test_security.py`

- [ ] **Step 1: Replace the old tests with failing no-session boundary tests**

```python
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.web.app import create_app


BOUND_PORT = 8765
HOST = f"127.0.0.1:{BOUND_PORT}"
ORIGIN = f"http://{HOST}"


def make_client(tmp_path):
    app = create_app(Settings(data_root=tmp_path / "data"), bound_port=BOUND_PORT)
    return TestClient(app, base_url=ORIGIN)


def test_root_has_no_login_or_security_tokens(tmp_path) -> None:
    response = make_client(tmp_path).get("/", headers={"host": HOST})
    assert response.status_code == 200
    assert "fis_session" not in response.headers.get("set-cookie", "")
    assert "csrf" not in response.text.lower()
    assert "launch" not in response.text.lower()


def test_mutation_requires_exact_origin_and_fis_header(tmp_path) -> None:
    client = make_client(tmp_path)
    assert client.post("/_test/mutation", headers={"host": HOST}).status_code == 403
    assert client.post(
        "/_test/mutation",
        headers={"host": HOST, "origin": ORIGIN, "x-fis-request": "1"},
    ).status_code == 204


def test_get_route_cannot_be_registered_as_mutating(tmp_path) -> None:
    client = make_client(tmp_path)
    response = client.get("/_test/mutation", headers={"host": HOST})
    assert response.status_code == 405
```

- [ ] **Step 2: Run the boundary tests and confirm the old contract fails**

Run: `python -m pytest tests/web/test_app.py tests/web/test_security.py -v`

Expected: FAIL because `create_app` still requires `launch_secret`, `/launch` and cookies still exist, and mutations still require a CSRF token.

- [ ] **Step 3: Implement the bound-port policy and simplified application factory**

```python
from dataclasses import dataclass
from typing import Final

from starlette.requests import Request

FIS_REQUEST_HEADER: Final = "x-fis-request"
UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class LocalRequestPolicy:
    port: int

    @property
    def authority(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.authority}"

    def validate(self, request: Request) -> None:
        require_one_exact_header(request, b"host", self.authority, status=400)
        reject_forwarded_headers(request)
        if request.method in UNSAFE_METHODS:
            require_one_exact_header(request, b"origin", self.origin, status=403)
            require_one_exact_header(request, b"x-fis-request", "1", status=403)
```

```python
def create_app(settings: Settings, *, bound_port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.bound_port = bound_port
    app.add_middleware(LocalRequestMiddleware, policy=LocalRequestPolicy(bound_port))
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")
    return app
```

Delete launch-secret exchange, session-cookie parsing, per-process session state, CSRF token parsing, `/launch`, and CSRF meta markup. Retain no-store, CSP, frame denial, MIME sniffing denial, referrer policy, and permissions policy response headers.

Reject duplicate or non-ASCII Host/Origin/FIS headers at the ASGI boundary, `localhost`, missing ports, trailing dots, comma-joined values, userinfo, IPv6 authorities, and all `Forwarded` or `X-Forwarded-*` headers. Match the Host authority exactly as delivered by ASGI; do not claim access to pre-parser whitespace. Legal HTTP OWS is normalized by Uvicorn/h11, and malformed raw requests are rejected by that server layer. OPTIONS receives no CORS approval.

Install a pure ASGI error wrapper as the outermost user middleware. It catches `Exception`, but not `BaseException`, emits a fixed diagnostic code without exception text or traceback, returns a generic 500 with the complete security-header set when no response has started, and never sends a second response after `http.response.start`.

- [ ] **Step 4: Run request-boundary and regression tests**

Run: `python -m pytest tests/web/test_app.py tests/web/test_security.py -v`

Expected: PASS for exact parsed-ASGI Host, exact unsafe-request Origin/header, no cookie/token/launch route, static assets, health, secured and sanitized 500 responses, ambiguous-header rejection, no CORS, and disabled docs. At least one real Uvicorn/raw-socket test must verify legal Host OWS normalization and missing, duplicate, or malformed Host rejection instead of relying exclusively on TestClient.

Run: `python -m pytest -q`

Expected: all existing foundation and XLSX tests pass; old session/CSRF expectations have been removed rather than skipped.

- [ ] **Step 5: Commit the replacement boundary**

```bash
git add src/fis/web/app.py src/fis/web/security.py src/fis/web/templates/base.html \
  tests/web/test_app.py tests/web/test_security.py
git commit -m "refactor: simplify loopback web boundary"
```

### Task 2: Add ordered ports, single-instance coordination, heartbeat, and shutdown

**Files:**
- Modify: `pyproject.toml`
- Create: `src/fis/runtime/__init__.py`
- Create: `src/fis/runtime/instance.py`
- Create: `src/fis/runtime/lifecycle.py`
- Create: `src/fis/runtime/server.py`
- Create: `tests/runtime/test_instance.py`
- Create: `tests/runtime/test_lifecycle.py`
- Create: `tests/runtime/test_server.py`

- [ ] **Step 1: Write failing lifecycle and port tests**

```python
def test_port_selection_uses_first_bindable_port(fake_socket_factory) -> None:
    fake_socket_factory.occupied.update({8765, 8766})
    binding = bind_first_loopback_port(fake_socket_factory, range(8765, 8776))
    assert binding.port == 8767
    assert binding.socket.bound_address == ("127.0.0.1", 8767)


def test_last_page_shutdown_waits_for_job_and_grace(fake_clock) -> None:
    lifecycle = Lifecycle(clock=fake_clock, grace_seconds=30)
    page = lifecycle.register_page()
    lifecycle.begin_job()
    lifecycle.remove_page(page)
    fake_clock.advance(31)
    assert lifecycle.should_exit() is False
    lifecycle.end_job()
    assert lifecycle.should_exit() is True


def test_existing_live_instance_is_reopened(tmp_path, fake_probe) -> None:
    fake_probe.healthy_ports.add(8768)
    record = InstanceRecord(pid=123, port=8768)
    write_instance_record(tmp_path, record)
    assert resolve_existing_instance(tmp_path, fake_probe) == record
```

- [ ] **Step 2: Run tests and confirm runtime modules are missing**

Run: `python -m pytest tests/runtime -v`

Expected: FAIL during collection because `fis.runtime` has not been implemented.

- [ ] **Step 3: Implement the runtime components**

Add `filelock>=3.16,<4` to project dependencies. Use a per-user `FileLock` at `settings.data_root / "runtime" / "instance.lock"` and an atomically replaced JSON record containing only PID, port, and start timestamp.

```python
PORTS = tuple(range(8765, 8776))


class StartupError(RuntimeError):
    """The local FIS process could not start safely."""


@dataclass(frozen=True)
class BoundSocket:
    socket: socket.socket
    port: int


def bind_first_loopback_port(
    factory: Callable[[], socket.socket], ports: Iterable[int] = PORTS
) -> BoundSocket:
    failures: list[int] = []
    for port in ports:
        candidate = factory()
        try:
            candidate.bind(("127.0.0.1", port))
            candidate.listen()
            return BoundSocket(candidate, port)
        except OSError:
            candidate.close()
            failures.append(port)
    raise StartupError(f"端口 8765–8775 均被占用：{failures}")
```

Pass the already-bound socket to Uvicorn with `Server.serve(sockets=[binding.socket])`; do not release it between selection and server start.

```python
class Lifecycle:
    def __init__(self, clock: Callable[[], float], grace_seconds: float = 30) -> None:
        self._clock = clock
        self._grace_seconds = grace_seconds
        self._heartbeats: dict[str, float] = {}
        self._active_jobs = 0
        self._no_page_since: float | None = self._clock()

    def register_page(self) -> str:
        page_id = secrets.token_urlsafe(18)
        self.heartbeat(page_id)
        return page_id

    def heartbeat(self, page_id: str) -> None:
        self._heartbeats[page_id] = self._clock()
        self._no_page_since = None

    def remove_page(self, page_id: str) -> None:
        self._heartbeats.pop(page_id, None)
        if not self._heartbeats:
            self._no_page_since = self._clock()

    def begin_job(self) -> None:
        self._active_jobs += 1

    def end_job(self) -> None:
        if self._active_jobs == 0:
            raise RuntimeError("active job count is already zero")
        self._active_jobs -= 1

    def should_exit(self) -> bool:
        now = self._clock()
        previous = tuple(self._heartbeats.values())
        live = {key: value for key, value in self._heartbeats.items() if now - value <= 15}
        self._heartbeats = live
        if live:
            self._no_page_since = None
        elif self._no_page_since is None:
            self._no_page_since = max(previous) + 15 if previous else now
        return (
            self._active_jobs == 0
            and self._no_page_since is not None
            and now - self._no_page_since >= self._grace_seconds
        )
```

Use monotonic time. Heartbeats expire after 15 seconds. With zero live pages, shutdown becomes eligible after an additional 30-second grace period and only when `active_jobs == 0`. Page IDs are random in-memory identifiers, not authentication credentials and not persisted.

Before removing a stale instance record, verify that the lock can be acquired and that its recorded loopback health endpoint is not a healthy FIS process. Never signal or terminate an unrelated PID.

- [ ] **Step 4: Run runtime tests**

Run: `python -m pytest tests/runtime -v`

Expected: ordered fallback, all-occupied error, socket reservation, healthy-instance reopen, stale-record recovery, heartbeat expiry, refresh recovery, active-job delay, and cleanup tests pass.

- [ ] **Step 5: Commit runtime lifecycle**

```bash
git add pyproject.toml src/fis/runtime tests/runtime
git commit -m "feat: add local server lifecycle"
```

### Task 3: Implement temporary upload and parse orchestration for the single page

**Files:**
- Create: `src/fis/services/__init__.py`
- Create: `src/fis/services/batches.py`
- Create: `src/fis/web/routes/__init__.py`
- Create: `src/fis/web/routes/workspace.py`
- Create: `src/fis/web/templates/workspace.html`
- Create: `src/fis/web/templates/_invoice_summary.html`
- Create: `tests/web/test_workspace_upload.py`
- Modify: `src/fis/web/app.py`

- [ ] **Step 1: Write failing upload and cleanup tests**

```python
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def mutation_headers(port: int = 8765) -> dict[str, str]:
    return {
        "host": f"127.0.0.1:{port}",
        "origin": f"http://127.0.0.1:{port}",
        "x-fis-request": "1",
    }


def test_upload_returns_summary_on_same_page(client, standard_workbook_bytes) -> None:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", standard_workbook_bytes, XLSX_MIME)},
    )
    assert response.status_code == 200
    assert "3 个货件" in response.text
    assert "选择输出文件夹" in response.text


def test_failed_upload_removes_temporary_source(client, batch_files) -> None:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("broken.xlsx", b"not-a-zip", XLSX_MIME)},
    )
    assert response.status_code == 422
    assert list(batch_files.temporary_sources()) == []
```

- [ ] **Step 2: Run upload tests and confirm routes are missing**

Run: `python -m pytest tests/web/test_workspace_upload.py -v`

Expected: FAIL with 404 for `/workspace/invoice`.

- [ ] **Step 3: Implement streamed upload, parse, and page workspace state**

```python
@dataclass(frozen=True)
class WorkspaceBatch:
    id: str
    source_path: Path
    source_filename: str
    source_sha256: str
    parsed: ParsedBundle


class BatchService:
    def create_from_upload(self, filename: str, chunks: Iterable[bytes]) -> WorkspaceBatch:
        require_xlsx_name(filename)
        work = self._files.create_temporary_batch()
        try:
            source, digest = stream_source(
                work, safe_upload_name(filename), chunks, 50 * 1024 * 1024
            )
            parsed = parse_invoice_bundle(source)
            batch = WorkspaceBatch(str(uuid4()), source, filename, digest, parsed)
            self._workspaces.put(batch)
            return batch
        except Exception:
            self._files.remove_temporary_batch(work)
            raise
```

Workspace state is process-local and contains the active source path and parsed immutable model. SQLite stores the processing/history record but never stores workbook bytes. The route returns `_invoice_summary.html`, showing Shipment IDs and counts inside the same page.

Accept exactly one multipart file named `invoice`, require `.xlsx`, cap request and streamed file size at 50 MiB, sanitize the display filename, and pass the saved file through the existing SafePackage/parser limits. Do not accept a client filesystem path.

Mount the workspace router in `create_app`. The root route renders `workspace.html`; no redirect or wizard URL is required.

- [ ] **Step 4: Run upload and complete regression tests**

Run: `python -m pytest tests/web/test_workspace_upload.py -v`

Expected: valid summary, wrong extension, duplicate file field, oversize body, malformed ZIP, unsupported template, parse failure cleanup, and retry tests pass.

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit upload orchestration**

```bash
git add src/fis/services src/fis/web/app.py src/fis/web/routes \
  src/fis/web/templates/workspace.html src/fis/web/templates/_invoice_summary.html \
  tests/web/test_workspace_upload.py
git commit -m "feat: add single-page invoice upload"
```

### Task 4: Add single-page edits, persisted defaults, and native output-folder selection

**Files:**
- Modify: `src/fis/services/batches.py`
- Modify: `src/fis/storage/repositories.py`
- Create: `src/fis/platform/__init__.py`
- Create: `src/fis/platform/folders.py`
- Modify: `src/fis/web/routes/workspace.py`
- Create: `src/fis/web/templates/_shipment_editor.html`
- Create: `src/fis/web/templates/_output_folder.html`
- Create: `tests/web/test_workspace_edits.py`
- Create: `tests/platform/test_folders.py`

- [ ] **Step 1: Write failing edit, persistence, and folder tests**

```python
def test_common_value_then_shipment_override(client, uploaded_batch) -> None:
    response = client.post(
        f"/workspace/{uploaded_batch}/fields",
        headers=mutation_headers(),
        json={"channel_all": "Ocean", "shipments": {"S2": {"channel": "Air"}}},
    )
    assert response.status_code == 200
    assert "Ocean" in response.text
    assert "Air" in response.text


def test_folder_picker_cancel_keeps_previous_value(client, uploaded_batch, fake_folder_picker) -> None:
    fake_folder_picker.result = None
    response = client.post(
        f"/workspace/{uploaded_batch}/output-folder",
        headers=mutation_headers(),
    )
    assert response.status_code == 204
    assert client.batch_service.get(uploaded_batch).output_directory is None


def test_completed_value_is_available_after_database_reopen(database, defaults_repository) -> None:
    defaults_repository.set("channel", "Ocean")
    assert DefaultsRepository(Database(database.path)).get("channel") == "Ocean"
```

- [ ] **Step 2: Run focused tests and confirm the edit APIs are absent**

Run: `python -m pytest tests/web/test_workspace_edits.py tests/platform/test_folders.py -v`

Expected: FAIL because edit commands, defaults repository, and folder adapter do not exist.

- [ ] **Step 3: Implement immutable edits, local persistence, and folder adapters**

```python
@dataclass(frozen=True)
class ShipmentEdit:
    warehouse_code: str | None = None
    warehouse_address: str | None = None
    channel: str | None = None
    delivery_date: date | None = None
    sailing_date: date | None = None
    delivery_warehouse: str | None = None


@dataclass(frozen=True)
class EditableBatch:
    workspace: WorkspaceBatch
    common: ShipmentEdit
    overrides: Mapping[str, ShipmentEdit]
    output_directory: Path | None
```

Apply common values first and explicit Shipment overrides second. A manual sailing date remains unchanged when delivery date later changes. Validate all Shipments and return a complete field-error mapping instead of stopping at the first error.

Extend SQLite repositories to persist master records, successful recent-channel choices, and last-used defaults. Store strings, dates, IDs, and paths only; reject BLOB values at repository boundaries.

```python
class FolderPicker(Protocol):
    def choose(self, initial: Path | None) -> Path | None:
        """Return the selected local directory, or None after cancellation."""
        raise NotImplementedError


class NativeFolderPicker:
    def choose(self, initial: Path | None) -> Path | None:
        if sys.platform == "darwin":
            return choose_with_osascript(initial)
        if os.name == "nt":
            return choose_with_powershell(initial)
        raise PlatformError("当前系统不支持文件夹选择窗口")
```

The macOS adapter invokes `/usr/bin/osascript` with a fixed script and passes the initial directory as an argument, never interpolating it into executable script text. The Windows adapter invokes the system PowerShell executable with a fixed encoded script using `System.Windows.Forms.FolderBrowserDialog`; the path is returned through stdout and validated as an absolute existing local directory. Cancellation returns `None`. Reject symlinks/junctions when the secure output writer cannot validate them.

- [ ] **Step 4: Run edit and platform tests**

Run: `python -m pytest tests/web/test_workspace_edits.py tests/platform/test_folders.py -v`

Expected: common fields, overrides, manual dates, complete validation, repository reopen persistence, picker success/cancel, invalid path, command failure, and fixed-script argument tests pass. Native UI is replaced with fakes in automated tests.

- [ ] **Step 5: Commit edits and folder selection**

```bash
git add src/fis/services/batches.py src/fis/storage/repositories.py src/fis/platform \
  src/fis/web/routes/workspace.py src/fis/web/templates/_shipment_editor.html \
  src/fis/web/templates/_output_folder.html tests/web/test_workspace_edits.py \
  tests/platform/test_folders.py
git commit -m "feat: add local invoice editing and folder selection"
```

### Task 5: Generate an atomic batch and persist path-only history and master data

**Files:**
- Modify: `src/fis/services/batches.py`
- Modify: `src/fis/storage/repositories.py`
- Create: `src/fis/web/routes/history.py`
- Create: `src/fis/web/routes/masters.py`
- Modify: `src/fis/web/routes/workspace.py`
- Create: `src/fis/web/templates/_generation_result.html`
- Create: `src/fis/web/templates/_history.html`
- Create: `src/fis/web/templates/_masters.html`
- Create: `tests/web/test_workspace_generation.py`
- Create: `tests/web/test_history.py`
- Create: `tests/web/test_masters.py`

- [ ] **Step 1: Write failing generation and path-only history tests**

```python
def test_generation_publishes_complete_batch_to_selected_folder(client, ready_batch, output_dir) -> None:
    response = client.post(
        f"/workspace/{ready_batch}/generate",
        headers=mutation_headers(),
    )
    assert response.status_code == 200
    assert sorted(path.suffix for path in output_dir.iterdir()) == [".xlsx", ".xlsx"]
    assert "已成功生成 2 份发票" in response.text


def test_one_generation_failure_publishes_no_files(client, ready_batch, output_dir, failing_engine) -> None:
    response = client.post(
        f"/workspace/{ready_batch}/generate",
        headers=mutation_headers(),
    )
    assert response.status_code == 422
    assert list(output_dir.iterdir()) == []


def test_history_database_contains_paths_but_no_workbook_blobs(database, completed_batch) -> None:
    connection = database.connect()
    try:
        row = connection.execute(
            "SELECT source_path, zip_path FROM processing_batches WHERE id = ?",
            (completed_batch,),
        ).fetchone()
        columns = connection.execute("PRAGMA table_info(processing_batches)").fetchall()
    finally:
        connection.close()
    assert row["zip_path"].endswith(".xlsx")
    assert not any(column["type"].upper() == "BLOB" for column in columns)
```

- [ ] **Step 2: Run tests and confirm generation/history routes are missing**

Run: `python -m pytest tests/web/test_workspace_generation.py tests/web/test_history.py tests/web/test_masters.py -v`

Expected: FAIL with missing generation command and routes.

- [ ] **Step 3: Implement atomic directory publication and persistence**

```python
def generate(self, batch_id: str) -> GenerationResult:
    batch = self.require_ready(batch_id)
    destination = require_writable_output_directory(batch.output_directory)
    self._lifecycle.begin_job()
    staging = self._files.create_staging_directory(destination, batch_id)
    try:
        outputs = generate_shipment_workbooks(batch.source_path, batch.requests, staging)
        publish_complete_set(staging, destination, outputs)
        self._repositories.record_completed_paths(batch, destination, outputs, self._clock())
        self._repositories.record_successful_defaults(batch)
        return GenerationResult(batch_id, destination, tuple(path.name for path in outputs))
    except Exception as error:
        self._repositories.record_failed_batch(batch, user_error(error), self._clock())
        raise
    finally:
        self._files.remove_staging_directory(staging)
        self._files.remove_temporary_source(batch_id)
        self._lifecycle.end_job()
```

Reserve all destination names before publishing. If any name exists, is duplicated case-insensitively, or cannot be safely created, block the entire batch. Publish with the existing per-file atomic mechanism plus a batch manifest; if a later publication fails, remove only files created by that attempt and leave pre-existing user files untouched.

History stores completed and failed records, Shipment snapshots, output filenames, and absolute output paths. It stores no source/generated blobs. “Open folder” resolves only a completed saved directory and passes it to the platform adapter. If it no longer exists, render “文件已移动或删除”.

Master-data mutations create, edit, sort, activate, and deactivate FBA warehouses, delivery warehouses, and channels. Enforce nonblank values and case-insensitive active-name uniqueness. Existing FBA addresses may be changed only in Master Data, not implicitly during invoice generation.

- [ ] **Step 4: Run generation, history, and master tests**

Run: `python -m pytest tests/web/test_workspace_generation.py tests/web/test_history.py tests/web/test_masters.py -v`

Expected: complete success, duplicate click, filename collision, engine failure rollback, database failure rollback, retry, path-only history, missing historical file, safe open-folder, and all master rules pass.

- [ ] **Step 5: Commit generation and persistence**

```bash
git add src/fis/services/batches.py src/fis/storage/repositories.py src/fis/web/routes \
  src/fis/web/templates/_generation_result.html src/fis/web/templates/_history.html \
  src/fis/web/templates/_masters.html tests/web/test_workspace_generation.py \
  tests/web/test_history.py tests/web/test_masters.py
git commit -m "feat: generate local batches and persist path history"
```

### Task 6: Complete the one-page browser flow and shutdown integration

**Files:**
- Modify: `src/fis/web/templates/workspace.html`
- Modify: `src/fis/web/templates/base.html`
- Modify: `src/fis/web/static/app.css`
- Create: `src/fis/web/static/app.js`
- Modify: `src/fis/web/routes/workspace.py`
- Modify: `src/fis/runtime/server.py`
- Create: `tests/browser/test_single_page_flow.py`
- Create: `tests/browser/test_single_page_errors.py`
- Create: `tests/browser/test_shutdown_flow.py`

- [ ] **Step 1: Write failing browser flows**

```python
def test_single_page_generation(page, live_fis, standard_workbook_path, output_dir) -> None:
    page.goto(live_fis.url)
    page.set_input_files("input[name=invoice]", standard_workbook_path)
    expect(page.get_by_text("3 个货件")).to_be_visible()
    page.get_by_label("运输渠道（全部）").select_option(label="Ocean")
    live_fis.folder_picker.result = output_dir
    page.get_by_role("button", name="选择输出文件夹").click()
    page.get_by_role("button", name=re.compile("生成 3 份发票")).click()
    expect(page.get_by_text("已成功生成 3 份发票")).to_be_visible()
    assert len(list(output_dir.glob("*.xlsx"))) == 3


def test_page_close_exits_after_grace(page, live_fis, fake_clock) -> None:
    page.goto(live_fis.url)
    page.close()
    fake_clock.advance(29)
    assert live_fis.lifecycle.should_exit() is False
    fake_clock.advance(1)
    assert live_fis.lifecycle.should_exit() is True
```

- [ ] **Step 2: Run browser tests and confirm the single-page behavior is incomplete**

Run: `python -m pytest tests/browser/test_single_page_flow.py tests/browser/test_single_page_errors.py tests/browser/test_shutdown_flow.py -v`

Expected: FAIL at the first missing single-page control, browser request header, or heartbeat route.

- [ ] **Step 3: Implement the minimal browser behavior**

```javascript
const FIS_HEADERS = {"X-FIS-Request": "1"};
const pageId = crypto.randomUUID();

async function fisPost(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-FIS-Request", "1");
  return fetch(url, Object.assign({}, options, {method: "POST", headers}));
}

setInterval(() => {
  void fisPost("/runtime/heartbeat", {
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({page_id: pageId}),
    keepalive: true,
  });
}, 5000);
```

Every mutation uses `fisPost`; ordinary links remain GET and read-only. On load, send the first heartbeat immediately. `pagehide` may send a best-effort removal request, but correctness depends on heartbeat expiry rather than browser unload delivery.

The HTML uses one `main` heading, explicit labels, visible focus, status regions with `aria-live`, Shipment row headers, and an error summary linking to invalid fields. Invoice summary, editor, output folder, generation result, History, and Master Data are sections of the same shell; server-rendered fragments replace only their matching sections.

Do not add a front-end framework or remote asset. Keep business state in the application service. The browser JavaScript is limited to request headers, heartbeat, fragment replacement, file-drop presentation, section disclosure, and focus restoration.

Connect Uvicorn exit to `Lifecycle.should_exit()`. On shutdown, clean temporary active batches, close SQLite connections, release the instance lock, remove the port record, and retain completed output/history data.

- [ ] **Step 4: Run browser, HTTP, and full regression suites**

Run: `python -m pytest tests/browser tests/web tests/runtime tests/platform -v`

Expected: primary flow, multi-Shipment override, blocking error, retry, history/master panels, exact custom headers, page refresh, last-page grace, active-job delayed exit, and cleanup pass.

Run: `python -m ruff check src tests && python -m mypy src && python -m pytest -v`

Expected: all commands exit 0. Windows-only native picker and launcher tests may be skipped on macOS only when marked with an explicit platform reason.

- [ ] **Step 5: Commit the completed local browser experience**

```bash
git add src/fis/web src/fis/runtime tests/browser tests/web tests/runtime tests/platform
git commit -m "feat: complete simplified local web workflow"
```

## Web workflow completion checkpoint

Run the packaged-style launcher with a temporary data root. Verify the following manually on macOS and in Windows CI before beginning release packaging:

1. With 8765 occupied, FIS starts on 8766 and opens the correct page.
2. A second launch opens the existing instance rather than starting another process.
3. A synthetic multi-Shipment workbook completes on one page and writes only to the selected folder.
4. History survives restart and contains paths but no workbook blobs.
5. Warehouse/channel defaults survive restart.
6. Closing the final page exits after approximately 30 seconds; an active generation finishes before exit.
7. Browser network logs contain no non-loopback request and no CORS approval.
8. The full XLSX regression suite and the real V1 sample five-Shipment generation remain successful.
