# FIS v1.0 Technical Design

Status: Approved in design discussion; pending final document review

Date: 2026-09-07

Product baseline: `Patrick-Zuo-hub/waymerce-prds`, `FIS/v1`, tag `fis-v1.0-baseline`
Baseline commit: `6db9a8f04bd703894b2f9c1e7f492706214e8b2c`

## 1. Purpose

This document turns the frozen FIS v1.0 PRD into an implementation-ready technical design without changing the product baseline. It records the agreed resolution of the PRD's DEV-TBC items and defines the safe implementation boundary for the first release.

FIS v1.0 is a local, single-user tool that splits one FBA customs invoice workbook containing multiple Shipment IDs into one workbook per Shipment. It supplements warehouse and logistics data, preserves the supported Excel template, packages the generated workbooks into a ZIP, and keeps successful batches available locally.

## 2. Confirmed product and delivery decisions

- Local web application; no cloud service and no network dependency.
- Single-machine, single-user operation with no login.
- Portable ZIP distribution for Windows x64 and macOS Apple Silicon.
- Python backend, FastAPI, server-rendered HTML with HTMX/minimal JavaScript, SQLite, and PyInstaller.
- Browser UI opens automatically after double-clicking the launcher.
- Application assets are bundled locally; CDNs and remote telemetry are prohibited.
- The source workbook is never modified in place.
- The current standard workbook is the only enabled V1 template.
- Successful history is retained until the user explicitly deletes a whole batch.

## 3. Scope boundaries

### 3.1 Included

- Upload and validate one `.xlsx` invoice bundle.
- Parse Shipment IDs, Shipment Items, Chinese product names, and supported embedded images.
- Parse a pasted Shipment ID to FBA Warehouse Code mapping.
- Confirm or batch-set shipping channel, delivery date, sailing date, and delivery warehouse.
- Add a previously unknown FBA warehouse address during the current workflow.
- Generate one preserved Excel workbook per Shipment and one batch ZIP.
- Download individual workbooks or the ZIP, reopen the output folder, and retrieve successful history.
- Maintain FBA warehouses, shipping channels, and delivery warehouses.

### 3.2 Excluded

- Amazon, ERP, finance, quotation, or tracking integrations.
- Cloud sync, multi-user access, login, roles, or remote administration.
- Arbitrary or heuristic interpretation of unknown Excel templates.
- A user-editable Template Configuration interface.
- Automatic application updates.
- Support for `.xls`, macro-enabled workbooks, external images, SVG, GIF, charts, SmartArt, grouped shapes, or other unsupported drawing objects.

## 4. Architecture

```text
Default browser
    │ HTTP on 127.0.0.1 + per-launch secret
    ▼
FastAPI application
    ├── HTML/HTMX user interface
    ├── workflow and validation services
    ├── master-data and history services
    ├── SQLite repositories
    └── XLSX/OOXML engine
           ├── template validation
           ├── workbook parsing
           ├── row/reference transformation
           ├── drawing/media transformation
           └── output validation and ZIP packaging
```

The backend binds only to `127.0.0.1`. A launcher owns the application process, selects an available port, establishes a single-instance lock, creates a random launch secret, and opens the browser. A second launch opens the existing instance instead of starting another database writer.

The browser provides an explicit **Exit FIS** action. Closing only the browser does not discard work or stop the process; double-clicking the launcher reopens the existing instance. OS shutdown terminates the process normally.

## 5. Portable filesystem layout

```text
FBA-Invoice-Splitter/
├── Start FIS.exe              # Windows distribution
├── Start FIS.app              # macOS distribution
├── app/                       # packaged runtime and bundled assets
└── data/                      # preserved during an application upgrade
    ├── fis.db
    ├── logs/
    └── batches/
        └── <batch-id>/
            ├── source/
            │   └── <original-name>.xlsx
            ├── work/          # temporary; recoverable and safe to clear
            └── output/
                ├── <ShipmentID>_<WarehouseCode>.xlsx
                └── <batch-id>.zip
```

Application upgrades replace packaged application files but not `data/`. Paths stored in SQLite are relative to the application data root so that the entire portable folder can be moved.

## 6. User experience and information architecture

The top navigation contains only:

1. **Process Invoice** — default page and primary workflow.
2. **History** — successful batches, batch detail, individual download, ZIP download, open folder, and whole-batch deletion.
3. **Master Data** — FBA warehouses, shipping channels, and delivery warehouses.

The primary workflow has three steps:

1. Select or drag in one `.xlsx` file.
2. Review the parsed Shipment count, item/image counts, mapping, logistics fields, and blocking issues; then generate.
3. Download the ZIP, open the output folder, download an individual invoice, or process another workbook.

No project creation, account setup, login, or separate batch-name field is required. The original filename and processing time identify a batch to the user.

Normal warnings do not interrupt the workflow. Missing required business data is edited on the confirmation page. Any condition that could produce the wrong Shipment, image, amount, or workbook structure blocks generation and identifies the affected sheet, row, field, or object.

## 7. Workflow and state model

Internal batch states are:

```text
PARSING → AWAITING_CONFIRMATION → GENERATING → COMPLETED
    └──────────────┴──────────────────► FAILED/INCOMPLETE
```

- `PARSING`: the source copy exists and template/parser validation is running.
- `AWAITING_CONFIRMATION`: parsed data is available for mapping and user edits.
- `GENERATING`: one generation action owns the batch lock.
- `COMPLETED`: every invoice and the final ZIP passed validation and was atomically published.
- `FAILED/INCOMPLETE`: parsing or generation failed; the batch remains available in the active workflow for correction or retry but is not part of formal History.

Generated files are first written beneath `work/`. Only after every Shipment output passes validation is the entire output set atomically moved into `output/`, the batch marked `COMPLETED`, and the recent successful channel values updated. A failure publishes neither an incomplete ZIP nor a successful history record.

On startup, interrupted `PARSING` or `GENERATING` batches are marked incomplete and temporary output is cleared. The source copy and confirmed values remain available for retry.

## 8. Data model

SQLite stores metadata and snapshots; source workbooks, generated workbooks, and images remain in the filesystem.

### 8.1 Core tables

| Table | Key content |
| --- | --- |
| `processing_batches` | UUID, source filename and SHA-256, template type/version, state, counts, relative paths, error code/detail, timestamps |
| `shipments` | batch ID, Shipment ID, seven required logistics fields as snapshots, Chinese-name summary, sailing-date manual-mode flag, output path |
| `shipment_items` | batch/Shipment IDs, original row number, reference ID, normalized fields JSON, associated drawing relationship IDs |
| `fba_warehouses` | code, name, address, status, last-successful-channel ID, timestamps |
| `shipping_channels` | ID, name, status, sort order, timestamps |
| `delivery_warehouses` | ID, name, address, status, sort order, timestamps |

Template Configuration is bundled, version-controlled application data rather than user-editable database content in V1.

Within a batch, Shipment ID is unique. History values are immutable snapshots: later edits or deactivation of master data do not alter prior batches.

### 8.2 Deletion

Deletion is whole-batch only and requires explicit confirmation. It removes the successful history record, Shipment and item snapshots, source copy, generated invoices, and ZIP for the selected batch. The application must resolve and verify every deletion target beneath that batch's directory before deleting it.

## 9. Master-data rules

- An FBA Warehouse Code is normalized and matched against FBA Warehouse Master.
- An unknown code may be created with an address from the confirmation page; an existing address cannot be overwritten there.
- Shipping Channel defaults from the warehouse's most recent **successful** batch. Selecting a channel in an incomplete or failed batch does not update the default.
- Delivery Warehouse is not remembered by FBA warehouse.
- Disabled channels and delivery warehouses are unavailable for new selections but remain visible in history snapshots.
- Delivery Date defaults to the nearest Thursday on or after the current local date.
- Sailing Date defaults to Delivery Date plus seven calendar days. A direct edit enables manual mode; subsequent Delivery Date changes no longer overwrite it.

## 10. Template Configuration and validation

V1 bundles one configuration:

```yaml
template_type: fba-customs-standard
template_version: v1
workbook_extension: .xlsx
worksheet_name: 装箱单&发票 发货信息
product_start_row: 12
fields:
  fba_warehouse_code: C4:F4
  fba_warehouse_address: C5:F5
  shipping_channel: C6:F6
  delivery_date: C8:D8
  sailing_date: C9:D9
  delivery_warehouse: C10:D10
  shipment_id_header: A11
  reference_id_header: B11
  product_region_columns: A:Z
  image_column: C
  blank_separator_rows: 1
  total_label: 合计TOTAL
  totals:
    cartons: K
    gross_weight: M
    volume: N
    declared_amount: P
  summary_cells:
    cartons: G8:L8
    gross_weight: G9:L9
    volume: G10:L10
    declared_amount_per_volume: O10:P10
    gross_weight_per_volume: R10:S10
supported_images: [png, jpg, jpeg]
```

The PRD names the labelled regions; direct inspection of the approved sample establishes the value ranges above. Writers place a value in the top-left cell of each merged value range and retain the merge itself.

### 10.1 Inspected sample invariants

The approved sample contains one sheet, `装箱单&发票 发货信息`, with an OOXML dimension of `A1:AF18`. Rows 12-16 are five contiguous product rows. Row 17 is a styled blank separator and row 18 is the `合计TOTAL` footer. Comments are attached to `L11`, `N11`, and `P11` and must remain attached to those header cells.

The sample contains five PNG `twoCellAnchor` drawings. Their zero-based start anchors are `(11,2)` through `(15,2)`, corresponding to `C12:C16`. Each ends at the next row and next column boundary. This exact one-row/two-column rectangle is a supported product-image shape; it is not treated as a cross-product-row image.

The total footer holds cartons, gross weight, volume, and declared amount in columns `K`, `M`, `N`, and `P`. Generated outputs recompute these four totals from the retained target rows. The header summary cells mirror cartons, gross weight, and volume in `G8`, `G9`, and `G10`. Formula cells `O10` (`P<total-row>/N<total-row>`) and `R10` (`G9/G10`) are rewritten if their references move, supplied with computed cached numeric values, and marked for Excel recalculation.

The source sample has pre-existing cached `#VALUE!` results in `O10` and `R10` because the source totals are stored as text, plus pre-existing `#REF!` list-validation sources for `O4:P4` and `R5:S5`. Generation corrects the directly affected numeric totals and summary formula caches. It preserves the unrelated invalid list validations as baseline content and records them in the template's allowlisted baseline anomalies rather than silently redefining their business rules.

A workbook is accepted only if all configured signature checks pass:

- `.xlsx` is a valid, non-encrypted OOXML package with no macros.
- The configured worksheet and required header labels are present in the expected positions.
- The product region starts at row 12. Every contiguous product row has a Shipment ID in column A and a Reference ID in column B.
- The product region is followed by exactly one styled blank separator row and then a row whose column A value normalizes to `合计TOTAL`.
- Required relationships and worksheet/drawing parts are internally valid.
- All drawing objects are classified as supported product images, supported fixed images, or explicitly unsupported objects.

Filename and ordinary business-cell changes do not invalidate a template. Missing headers, moved key columns, ambiguous gaps, unknown formula/total structures, or changed drawing structures produce `TEMPLATE_VERSION_MISMATCH`; FIS does not guess.

Customer order number, FBA flag, customs mode, ordinary/special-goods fields, non-FBA address, FDA/VAT, and other existing extension fields are preserved from the original workbook unless a later approved product baseline defines explicit per-Shipment writing behavior. They must never be silently removed.

## 11. OOXML processing strategy

The preservation engine operates on the XLSX package. It does not recreate the workbook from an in-memory spreadsheet model.

### 11.1 Preservation principle

1. Copy the source XLSX to a per-Shipment working file.
2. Copy every untouched OOXML package part byte-for-byte.
3. Parse and narrowly modify only configured worksheet, drawing, relationship, calculation, and package-index parts.
4. Preserve unsupported-but-unaffected workbook content only when validation proves the output relationship graph remains safe; otherwise block before generation.

A high-level library may be used for read-only inspection and fixture assertions, but it is not the authoritative writer because a full workbook save can discard or rewrite Excel-specific content.

### 11.2 Product rows

The parser builds a source-row-to-Shipment map and identifies the configured product region. For each output:

- retain only rows assigned to the target Shipment;
- compact retained product rows from the configured start row;
- shift footer rows by the number of removed product rows;
- apply one shared old-row to new-row transformation to cell references, merged ranges, hyperlinks, data validations, comments, drawings, formulas/ranges covered by the configuration, and print areas;
- preserve row heights, column widths, cell styles, and number formats.

After compaction, FIS retains one blank separator row, moves the total row to the next row, and recomputes configured totals from numeric interpretations of the retained item cells. A missing or nonnumeric required total input blocks generation and identifies the source cell. It does not coerce missing business data to zero.

If a formula, named range, validation, merge, comment, or drawing references removed rows and cannot be transformed deterministically, generation is blocked instead of dropping it.

### 11.3 Images and drawings

- An embedded PNG/JPG/JPEG is a product image when its `from` anchor starts in configured image column C on a product row and its `to` anchor ends no later than the next row boundary. The source row is determined by the `from` anchor.
- Product images are kept only when their product row belongs to the target Shipment; their anchors are transformed with the retained row.
- Images outside the product region that are positively identified as fixed template content, such as a logo or header image, are retained in every output.
- A drawing whose start anchor is outside any recognised row, whose end crosses beyond the next product-row boundary, or whose ownership is otherwise ambiguous blocks the whole batch. External images, SVG/GIF, charts, SmartArt, grouped shapes, and unsupported objects also block it.
- Relationship IDs, drawing XML, media parts, content types, and package relationships are updated as one unit; orphaned or duplicate relationships fail output validation.

### 11.4 Output fields and files

Every generated invoice contains only the target Shipment Items and writes the confirmed Shipment ID, FBA Warehouse Code, FBA warehouse address, shipping channel, delivery date, sailing date, and delivery warehouse into configured locations. It recomputes configured totals and summary values using the target rows only.

The filename is `<ShipmentID>_<FBAWarehouseCode>.xlsx`. Both components are normalized for filesystem safety, while the values inside the workbook retain their business representation. Duplicate normalized filenames block generation and identify the conflicting Shipments.

After all workbooks pass validation, FIS creates one ZIP containing only the final Shipment workbooks.

## 12. Validation and errors

### 12.1 Error classes

| Class | Examples | Behavior |
| --- | --- | --- |
| Blocking | invalid template, missing Shipment ID, ambiguous rows, unsupported image/object, conflicting mapping, unsafe formula transformation, output validation failure | Stop progression or generation and identify the exact location |
| Required input | missing warehouse address, channel, delivery date, sailing date, or delivery warehouse | Mark field/row and allow correction on the confirmation page |
| Warning | safely ignored blank mapping rows or other non-impacting normalization | Show concise information without blocking |

Errors use stable internal codes plus user-facing Chinese messages. Logs may include batch ID, worksheet, row/cell/object location, stage, and error code, but must not dump complete product rows, addresses, workbook XML, or other sensitive business content.

### 12.2 Output validation

Before publication, each workbook must pass:

- ZIP/package integrity and required-part checks;
- OOXML relationship and media-reference consistency;
- exactly one target Shipment ID and only its expected item rows;
- expected item-image associations and fixed-image preservation;
- required logistics-field values;
- configured formulas and cached results, target-only totals, merges, styles, row/column dimensions, comments, and print settings, subject to the explicit baseline-anomaly allowlist;
- successful open by an independent read-only workbook parser used in tests.

## 13. Local security controls

- Bind only to `127.0.0.1`; reject non-loopback and unexpected Host headers.
- Generate a high-entropy secret per launch, establish it through the automatically opened URL, and require the resulting protected session for state-changing endpoints.
- Apply same-origin/CSRF checks to all mutations.
- Enforce upload size, extension, MIME/signature, ZIP entry count, decompressed-size, compression-ratio, and XML-size limits.
- Reject encrypted files, macros, external links/resources where unsafe, absolute paths, and ZIP path traversal.
- Resolve all reads, writes, downloads, folder opens, and deletions beneath approved batch/data roots.
- Escape workbook-derived text in HTML and never execute workbook formulas, macros, or embedded content.
- Use parameterized SQLite queries and a single-writer transaction boundary for generation and deletion.
- Provide no remote telemetry, CDN, external API, or LAN listener.

## 14. Packaging and release

PyInstaller one-folder distributions are used instead of a single self-extracting executable or installer. This makes bundled assets explicit, reduces launch overhead, and keeps the `data/` boundary clear.

- Windows: no-console `Start FIS.exe`, tested on a clean supported Windows x64 machine.
- macOS: `Start FIS.app`, built for Apple Silicon and tested on a clean supported macOS machine.
- The application bundles Python, server code, templates, UI assets, SQLite support, and the required XML libraries.
- V1 does not require Python, Node.js, a database server, administrator rights, or internet access on the target machine.
- macOS signing/notarization and Windows code signing are release-environment concerns. Unsigned internal builds may trigger OS trust warnings; production distribution should be signed when the required certificates are available.

## 15. Verification strategy

### 15.1 Automated tests

- Domain tests for defaults, manual-mode dates, mapping normalization/conflicts, master-data history, states, and deletion boundaries.
- Parser tests for valid and invalid template signatures, product/footer boundaries, formulas, merges, comments, and drawing classifications.
- OOXML golden tests using anonymized/synthetic fixtures, asserting both business content and package relationships.
- Property/edge tests for multiple items per Shipment, noncontiguous Shipment rows when rejected or supported by configuration, filename collisions, empty rows, unusual Unicode, and size limits.
- Transaction/recovery tests for interruption at each generation stage and duplicate Generate requests.
- HTTP tests for launch-secret, CSRF, path isolation, uploads, downloads, and local-only behavior.
- Browser tests for the three-step workflow, inline validation, batch setting with per-row override, history downloads, master-data maintenance, and deletion confirmation.

Generated XLSX files are binary artifacts. Tests compare normalized business/OOXML invariants rather than raw ZIP bytes, while also confirming untouched package parts remain byte-identical where required.

### 15.2 Release acceptance

For each release candidate:

1. Run the complete automated suite on Windows and macOS.
2. Launch each portable package on a clean machine without Python, Node, or database software.
3. Process an anonymized representative workbook and compare Shipment/item/image counts and all seven output fields.
4. Open every output in Microsoft Excel and perform a manual image, formula/total, formatting, comment, and print-preview check.
5. Verify retry after a forced failure, single-instance relaunch, History redownload, whole-batch deletion, application exit, and data preservation across an application-folder upgrade.
6. Verify operation with networking disabled.

## 16. DEV-TBC resolution

| Item | Resolution |
| --- | --- |
| DEV-TBC-01 | Versioned built-in mapping writes logistics values to merged ranges `C4:F4`, `C5:F5`, `C6:F6`, `C8:D8`, `C9:D9`, and `C10:D10`; sample merges, validations, comments, totals, formulas, styles, and print settings become fixture invariants. |
| DEV-TBC-02 | Product rows start at row 12 and continue while columns A/B contain Shipment/Reference IDs; exactly one blank separator and an `合计TOTAL` row terminate the region. Ambiguity blocks processing. |
| DEV-TBC-03 | Preserve supported template structure and configured invariants; unsupported or untransformable objects block generation. |
| DEV-TBC-04 | Strict signature validation; template drift produces a locatable `TEMPLATE_VERSION_MISMATCH`, with no heuristic fallback. |
| DEV-TBC-05 | Version-controlled, bundled Template Configuration keyed by type/version; no V1 editing UI and no change to the Shipment data model. |
| DEV-TBC-06 | SQLite metadata plus per-batch filesystem storage; successful history retained until explicit whole-batch deletion; interrupted work is recoverable but excluded from formal History. |
| DEV-TBC-07 | Three-step primary workflow with separate History and Master Data pages, no setup ceremony. |
| DEV-TBC-08 | No login; single local user, loopback-only service, and per-launch access protection. |
| DEV-TBC-09 | Narrow package-level OOXML transformation with row mapping, drawing/media relationship updates, strict supported-image classification, and post-generation relationship validation. |

## 17. Implementation sequence

1. Pin the anonymized template fixture and executable Template Configuration.
2. Implement domain rules, SQLite schema/migrations, and repositories with tests.
3. Implement strict XLSX/template inspection and normalized parsed snapshots.
4. Implement OOXML row, formula/range, drawing, relationship, and media transformation through golden tests.
5. Implement atomic generation, ZIP packaging, recovery, and history.
6. Implement the server-rendered three-step UI and Master Data/History pages.
7. Add launcher, single-instance behavior, local security controls, and clean exit.
8. Build and verify Windows/macOS portable distributions.

Implementation must stop and request a new PRD version if sample inspection or development reveals a required business behavior that contradicts or expands the frozen FIS v1.0 baseline.
