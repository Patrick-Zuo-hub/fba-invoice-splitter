# FIS V1 Simplified Local Web Design

**Status:** proposed  
**Date:** 2026-09-10  
**Scope:** FBA Invoice Splitter V1 local user interface and process lifecycle

## 1. Decision summary

FIS V1 is a single-user, offline tool that always runs on the same computer as its user. It will keep the browser-based interface, but it will not behave like an internet application.

The local web layer will therefore use a small, user-invisible safety boundary instead of login and session infrastructure:

- bind only to `127.0.0.1`;
- prefer port `8765`, then try `8766` through `8775` in order;
- reject requests whose Host does not match the actual bound address and port;
- accept state-changing requests only from the exact local Origin and with the fixed FIS request header;
- expose no CORS access, API documentation, LAN binding, user accounts, login, HTTPS, JWT, launch secret, session cookie, or CSRF token;
- exit automatically after all pages disappear and no job is running.

The product UI will be one page rather than a three-step wizard.

## 2. Goals and non-goals

### Goals

- Start by double-clicking a portable executable.
- Open the default browser automatically.
- Complete import, review, editing, output-folder selection, generation, and result review on one page.
- Preserve local master data and generation history across restarts.
- Store generated workbooks in a folder selected by the user.
- Shut down the background service automatically when use ends.
- Prevent ordinary external web pages from invoking FIS mutations through the browser.

### Non-goals

- LAN or remote access.
- Multiple users, accounts, roles, permissions, or login.
- Cloud synchronization or online services.
- HTTPS, JWT, OAuth, or persistent web sessions.
- Keeping uploaded source workbook copies after the active job ends.
- Storing generated workbook contents inside SQLite.

## 3. Process and port lifecycle

### 3.1 Single instance

The launcher acquires a per-user single-instance lock in the FIS runtime-data directory.

- If the lock is free, the launcher starts the local service.
- If the lock belongs to a healthy FIS process, the launcher reads the recorded port, opens the existing page, and exits.
- If the lock is stale, the launcher removes only that validated stale lock and starts normally.
- The port record is written only after the server has successfully bound.

The lock and port record are runtime coordination files, not authentication credentials.

### 3.2 Port selection

The server attempts to bind atomically in this order:

`127.0.0.1:8765`, then `127.0.0.1:8766` through `127.0.0.1:8775`.

It must not perform a separate “is this port free?” scan followed by a later bind. The bind operation itself is the availability check, avoiding a race with other processes.

If all ports are unavailable, the launcher displays a local, user-readable error and exits. It never binds to `0.0.0.0`, `::`, a LAN address, or an arbitrary port outside the configured range.

### 3.3 Browser and shutdown

After the service is ready, the launcher opens the exact bound URL in the default browser.

Each open FIS page sends a heartbeat every five seconds. The service tracks the most recent heartbeat from all page instances.

- When at least one page is active, the service remains running.
- When all heartbeats stop, a 30-second grace period begins.
- A refreshed or reopened page cancels the pending shutdown.
- If no generation job is running when the grace period expires, the service exits.
- If a generation job is running, the service finishes or safely fails that job, publishes no partial output, and then exits.

Shutdown removes active-job temporary files and releases the lock and port record. Persistent SQLite data and completed output workbooks remain.

## 4. Local request boundary

### 4.1 Read requests

Read-only routes may be requested without login or cookies. GET and HEAD routes must not mutate application, filesystem, or database state.

The health route contains only process-health information. Framework API documentation and OpenAPI endpoints remain disabled.

### 4.2 State-changing requests

POST, PUT, PATCH, and DELETE requests must satisfy all of the following:

- Host exactly matches `127.0.0.1:<bound-port>`;
- Origin exactly matches `http://127.0.0.1:<bound-port>`;
- request contains the fixed header `X-FIS-Request: 1`;
- request content type is one explicitly supported by the target route;
- the route applies its existing upload-size, workbook-package, and field validation.

The application sends no permissive CORS response. A normal external web page cannot add the custom header without a cross-origin preflight, and FIS does not approve that preflight.

Requests with duplicate, ambiguous, malformed, forwarded, or proxy-derived Host/Origin values are rejected. FIS does not trust `X-Forwarded-*` headers.

This boundary is designed to stop ordinary browser-origin attacks. It does not claim to defend against malware already executing as the same operating-system user.

## 5. Single-page user experience

The page has five progressive sections. Later sections stay compact until their data is available.

1. **Select invoice** — choose one supported `.xlsx` file and parse it locally.
2. **Review shipments** — show detected Shipment IDs, product counts, and blocking workbook issues.
3. **Complete information** — edit common logistics values once; expand a Shipment only when it needs an override.
4. **Select output folder** — open the native Windows or macOS folder picker through the local backend.
5. **Generate and review** — generate the complete batch, show per-Shipment success or failure, and provide “Open folder”.

There is no login page, launch exchange, wizard navigation, or web session screen.

The page may expose lightweight secondary panels for History and Master Data, but they stay within the same application shell and do not interrupt the primary flow.

## 6. Local data and files

SQLite persists only local operational data:

- warehouse codes and addresses;
- common shipping channels;
- last-used defaults;
- generation timestamp, Shipment ID, status, and output path;
- enough error metadata to explain a failed generation without storing workbook contents.

SQLite does not store source workbook blobs, image blobs, generated workbook blobs, passwords, tokens, or browser sessions.

The source workbook is copied into a job-specific temporary directory only while it is being parsed or generated. The temporary copy is removed after success, failure, or process shutdown.

Generated workbooks are written directly to the user-selected output directory through the existing atomic-output mechanism. History stores paths only. A missing historical path is shown as “file moved or deleted” and does not corrupt the history record.

The native folder picker must return a local directory path. Cancellation leaves the current form unchanged. An inaccessible or unwritable directory produces a clear validation message before generation begins.

## 7. Components and boundaries

- **Launcher:** single-instance lock, port binding, browser open, native folder picker, shutdown coordination.
- **Local HTTP application:** page rendering, request-boundary checks, upload orchestration, heartbeat, and user actions.
- **Job service:** temporary source lifecycle, parse/generate calls, active-job count, and atomic batch outcome.
- **SQLite repositories:** master data, defaults, and history only.
- **XLSX engine:** the already completed parser, transformer, generator, and independent output validator. It remains independent of FastAPI.

The web layer calls the XLSX engine through typed application services. XLSX code must not depend on HTTP request objects, templates, cookies, or UI state.

## 8. Error handling

- All ports unavailable: show a startup error naming the attempted range.
- Existing healthy instance: open it rather than starting another instance.
- Invalid workbook: show the stable workbook error and keep the form available for another file.
- Invalid output directory: block generation and explain the path problem.
- Batch failure: publish no partial batch and record a failed history entry without retaining the uploaded workbook.
- Browser closes during generation: finish the active atomic job, then shut down.
- Unexpected internal error: show a generic local error, record a sanitized diagnostic locally, and avoid exposing stack traces in the browser.

## 9. Verification strategy

Automated verification must cover:

- exact loopback binding and ordered port fallback;
- existing-instance and stale-lock behavior;
- exact Host, Origin, and `X-FIS-Request` enforcement;
- absence of login, session cookie, launch secret, CSRF token, CORS, and API docs;
- GET/HEAD mutation prohibition;
- single-page import-to-generation flow;
- folder-picker success, cancellation, and invalid-directory behavior on Windows and macOS;
- heartbeat grace period, refresh recovery, active-job delayed shutdown, and cleanup;
- SQLite persistence without source/generated file blobs;
- batch atomicity and history path behavior;
- unchanged XLSX engine regression suite and real V1 sample verification.

Windows-native launcher, folder-picker, and process-shutdown tests must run in Windows CI before release. macOS-native behavior must be verified on macOS. Browser-flow tests may use a controlled local test server and synthetic anonymous workbooks.

## 10. Effect on the current implementation plan

The existing Web Task 1 implementation is not an approved baseline because its specification review was paused before completion. It will be replaced rather than extended.

The revised plan will:

- remove launch-secret, session-cookie, and CSRF-token work;
- retain and simplify Host/Origin/request-header protection;
- add fixed preferred ports, single-instance coordination, heartbeats, and automatic shutdown;
- combine the former three-step browser flow into one page;
- retain upload, mapping, generation, History, and Master Data capabilities in the reduced interface;
- preserve the completed XLSX engine without modification unless integration testing exposes a defect.

