# FIS v1 Portable Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and verify portable Windows x64 and macOS Apple Silicon distributions that start once, open the protected local UI, preserve `data/`, and exit cleanly without installed development runtimes.

**Architecture:** A small launcher obtains a single-instance lock, selects a loopback port, generates the launch secret, starts Uvicorn, and opens the protected launch URL. PyInstaller one-folder bundles contain the runtime and static/template assets while keeping the sibling `data/` directory external and durable.

**Tech Stack:** Python 3.12, Uvicorn, PyInstaller, pytest, platform-native CI runners, Microsoft Excel manual acceptance

---

## File map

| Path | Responsibility |
| --- | --- |
| `src/fis/launcher.py` | Port, secret, browser, server, single-instance, and shutdown lifecycle |
| `src/fis/platform.py` | Windows/macOS folder-open and data-root adapters |
| `src/fis/web/routes/system.py` | Exit and runtime-status endpoints |
| `packaging/fis.spec` | PyInstaller one-folder definition |
| `scripts/build_windows.ps1` | Reproducible Windows build |
| `scripts/build_macos.sh` | Reproducible Apple Silicon build |
| `.github/workflows/release.yml` | Cross-platform tests and artifact builds |
| `docs/release-checklist.md` | Signed/unsigned packaging and Excel acceptance record |
| `tests/launcher/` | Lifecycle and single-instance tests |
| `tests/security/` | Local attack-boundary regression tests |

### Task 1: Implement single-instance launcher lifecycle

**Files:**
- Create: `src/fis/launcher.py`
- Create: `tests/launcher/test_launcher.py`

- [ ] **Step 1: Write failing launcher tests**

```python
def test_launcher_binds_loopback_and_opens_secret_url(fake_server, fake_browser, tmp_path) -> None:
    runtime = launch(data_root=tmp_path / "data", server=fake_server, browser=fake_browser)
    assert runtime.host == "127.0.0.1"
    assert runtime.port > 0
    assert fake_browser.urls == [f"http://127.0.0.1:{runtime.port}/launch?secret={runtime.secret}"]


def test_second_launch_reopens_existing_instance(running_instance, fake_browser) -> None:
    runtime = launch(data_root=running_instance.data_root, server=FailIfStarted(), browser=fake_browser)
    assert runtime.reused_existing is True
    assert fake_browser.urls == [running_instance.launch_url]
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/launcher/test_launcher.py -v`

Expected: FAIL because the launcher is missing.

- [ ] **Step 3: Implement runtime ownership**

```python
@dataclass(frozen=True)
class RuntimeInfo:
    host: str
    port: int
    secret: str
    reused_existing: bool = False


def reserve_loopback_port() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    return listener, int(listener.getsockname()[1])


def new_launch_secret() -> str:
    return secrets.token_urlsafe(32)
```

Use an exclusive lock file under `data/runtime/instance.lock` containing PID, port, and a distinct reopen token with owner-only permissions. Validate that the PID and `/health` instance marker are both current before reuse; remove stale locks. Transfer the reserved socket to Uvicorn to avoid a port race. Register SIGINT/SIGTERM and the web exit event for graceful database/server shutdown, then remove only the owned lock.

- [ ] **Step 4: Run launcher tests**

Run: `python -m pytest tests/launcher/test_launcher.py -v`

Expected: fresh launch, concurrent second launch, stale lock, occupied port, graceful exit, and OS signal cases pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/launcher.py tests/launcher
git commit -m "feat: add portable single-instance launcher"
```

### Task 2: Add safe system and platform actions

**Files:**
- Create: `src/fis/platform.py`
- Create: `src/fis/web/routes/system.py`
- Modify: `src/fis/web/app.py`
- Test: `tests/web/test_system.py`

- [ ] **Step 1: Write failing exit and folder tests**

```python
def test_exit_requires_csrf(client) -> None:
    assert client.post("/system/exit").status_code == 403


def test_open_folder_accepts_only_completed_batch(client, completed_batch, platform_adapter) -> None:
    response = client.post(f"/history/{completed_batch}/open-folder",
                           data={"csrf_token": client.csrf_token})
    assert response.status_code == 204
    assert platform_adapter.opened == [client.batch_files.output_path(completed_batch)]
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/web/test_system.py -v`

Expected: FAIL because system routes are missing.

- [ ] **Step 3: Implement injected system operations**

```python
class PlatformAdapter:
    def open_folder(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        if sys.platform == "darwin":
            subprocess.run(["open", str(resolved)], check=True)
        elif sys.platform == "win32":
            os.startfile(resolved)  # type: ignore[attr-defined]
        else:
            raise RuntimeError("Unsupported platform")


@router.post("/system/exit", status_code=204)
def exit_fis(request: Request) -> Response:
    request.app.state.exit_event.set()
    return Response(status_code=204)
```

Resolve the batch output path inside `BatchFiles` before calling `PlatformAdapter`; the route accepts a batch ID, never a path. After `/system/exit`, return a small closing page first, then signal the server through the injected event.

- [ ] **Step 4: Run system tests**

Run: `python -m pytest tests/web/test_system.py -v`

Expected: CSRF, incomplete batch, traversal ID, valid open-folder, and graceful exit tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/platform.py src/fis/web/routes/system.py src/fis/web/app.py tests/web/test_system.py
git commit -m "feat: add safe local system actions"
```

### Task 3: Complete local security regression tests

**Files:**
- Create: `tests/security/test_local_boundary.py`
- Create: `tests/security/test_xlsx_limits.py`
- Modify: `src/fis/web/security.py`
- Modify: `src/fis/xlsx/package.py`

- [ ] **Step 1: Add failing boundary tests**

```python
@pytest.mark.parametrize("host", ["attacker.example", "0.0.0.0", "192.168.1.5"])
def test_nonlocal_host_is_rejected(client, host) -> None:
    assert client.get("/", headers={"host": host}).status_code == 400


def test_zip_bomb_ratio_is_rejected(zip_bomb_xlsx) -> None:
    with pytest.raises(WorkbookError) as error:
        SafePackage.open(zip_bomb_xlsx)
    assert error.value.code == "UNSAFE_XLSX"


def test_download_cannot_escape_batch(client, completed_batch) -> None:
    assert client.get(f"/history/{completed_batch}/files/..%2F..%2Fsecret").status_code in {404, 422}
```

- [ ] **Step 2: Verify at least one test fails**

Run: `python -m pytest tests/security -v`

Expected: at least one new limit or traversal test fails before hardening.

- [ ] **Step 3: Enforce all designed limits**

Add a maximum compression ratio of 100:1 per XLSX member, maximum XML part size of 20 MB, defused lxml parsers with `resolve_entities=False`, `no_network=True`, and `huge_tree=False`, rejection of external relationships in workbook/worksheet/drawing parts, strict output filename lookup by stored record, and security response headers:

```python
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
```

- [ ] **Step 4: Run security and full tests**

Run: `python -m pytest tests/security -v && python -m pytest -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/web/security.py src/fis/xlsx/package.py tests/security
git commit -m "test: harden local and XLSX boundaries"
```

### Task 4: Define reproducible one-folder packages

**Files:**
- Create: `packaging/fis.spec`
- Create: `scripts/build_windows.ps1`
- Create: `scripts/build_macos.sh`
- Create: `tests/packaging/test_bundle_manifest.py`

- [ ] **Step 1: Write a failing bundle-manifest test**

```python
def test_spec_bundles_required_assets() -> None:
    text = Path("packaging/fis.spec").read_text()
    for required in ("src/fis/web/templates", "src/fis/web/static", "Start FIS"):
        assert required in text
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/packaging/test_bundle_manifest.py -v`

Expected: FAIL because `packaging/fis.spec` is missing.

- [ ] **Step 3: Add the PyInstaller definition and platform scripts**

```python
# packaging/fis.spec
from PyInstaller.utils.hooks import collect_submodules

datas = [
    ("src/fis/web/templates", "fis/web/templates"),
    ("src/fis/web/static", "fis/web/static"),
]
hiddenimports = collect_submodules("uvicorn")
a = Analysis(["src/fis/launcher.py"], pathex=["src"], datas=datas,
             hiddenimports=hiddenimports, excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Start FIS",
          console=False, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="FBA-Invoice-Splitter")
```

Windows script creates a clean virtual environment, installs the locked project and PyInstaller, runs all tests, builds with the spec, creates an empty sibling `data/`, and compresses `dist/FBA-Invoice-Splitter` to a versioned ZIP. macOS performs the same steps, then builds an `.app` wrapper that invokes the bundled executable without a Terminal window. Both scripts fail if source customer spreadsheets, populated `data/`, logs, or generated outputs are present in the bundle.

- [ ] **Step 4: Run manifest test and a host-platform build**

Run: `python -m pytest tests/packaging/test_bundle_manifest.py -v`

Expected: PASS.

Run on macOS: `bash scripts/build_macos.sh 0.1.0`

Expected: a versioned macOS ZIP containing `Start FIS.app`, bundled `app/`, and empty `data/`.

- [ ] **Step 5: Commit**

```bash
git add packaging scripts tests/packaging
git commit -m "build: add portable FIS packages"
```

### Task 5: Add cross-platform build workflow and release acceptance

**Files:**
- Create: `.github/workflows/release.yml`
- Create: `docs/release-checklist.md`
- Modify: `README.md`

- [ ] **Step 1: Write the release workflow**

```yaml
name: release
on:
  workflow_dispatch:
    inputs:
      version:
        description: Release version
        required: true
jobs:
  build:
    strategy:
      matrix:
        os: [windows-latest, macos-14]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest -v
      - if: runner.os == 'Windows'
        shell: pwsh
        run: ./scripts/build_windows.ps1 ${{ inputs.version }}
      - if: runner.os == 'macOS'
        run: bash scripts/build_macos.sh ${{ inputs.version }}
      - uses: actions/upload-artifact@v4
        with:
          name: FIS-${{ runner.os }}-${{ inputs.version }}
          path: release/*.zip
```

- [ ] **Step 2: Add the exact manual checklist**

`docs/release-checklist.md` must require reviewer/date/build hash for: clean-machine launch without Python/Node/database; loopback-only network; second-launch reuse; two-Shipment synthetic generation; all seven fields; Shipment/item/image counts; totals; comments; formulas; formatting; print preview; single/ZIP redownload; forced-failure retry; whole-batch deletion; Exit FIS; offline operation; and `data/` preservation during application replacement. It must record whether macOS and Windows artifacts are signed and the expected trust warning when unsigned.

- [ ] **Step 3: Document end-user startup**

Add concise Windows/macOS sections to `README.md`: download correct ZIP, extract the entire directory, double-click the launcher, keep `data/` when upgrading, use Exit FIS before moving the folder, and never place the portable folder on a multi-user network share.

- [ ] **Step 4: Run final automated verification**

Run: `python -m ruff check src tests && python -m mypy src && python -m pytest -v`

Expected: all commands exit 0.

Trigger the workflow manually for a release candidate. Expected: both platform build jobs pass and publish one ZIP each. Perform and sign the manual Microsoft Excel checklist on both platforms before calling V1 releasable.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/release.yml docs/release-checklist.md README.md
git commit -m "ci: verify Windows and macOS releases"
```

## Release completion checkpoint

FIS v1.0 is complete only when both portable artifacts pass automated tests and the recorded clean-machine Microsoft Excel acceptance. Code signing may remain unavailable for an internal build, but the release notes must state that fact and the corresponding OS warning; no test or safety failure may be waived silently.
