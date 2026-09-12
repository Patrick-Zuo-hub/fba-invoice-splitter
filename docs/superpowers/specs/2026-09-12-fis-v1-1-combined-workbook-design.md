# FIS v1.1 Combined Workbook Design

**Date:** 2026-09-12  
**Status:** Approved  
**Product baseline:** `Waymerce-PRDs/FIS/v1.1/prd/02-combined-workbook-output.md`

## Goal

Replace the current one-workbook-per-Shipment output with one workbook per processing batch. The workbook contains exactly one visible worksheet for every Shipment, in the same order as the shipment workspace table, and is suitable for direct upload without a summary sheet or ZIP step.

## Output contract

- Publish exactly one `.xlsx` file after a successful generation.
- Name the first result `已处理_<source stem>.xlsx`.
- If that path exists, choose the first free suffix `_2`, `_3`, and so on without overwriting any existing file.
- Create one worksheet per Shipment in workspace order.
- Prefer `<warehouse code>-<shipment id>` for each worksheet title.
- Replace Excel-forbidden title characters, remove unsafe edge apostrophes, cap titles at 31 characters, compare uniqueness case-insensitively, and append a deterministic numeric suffix inside the length limit when needed.
- Do not add an index, cover, summary, or any other visible worksheet.
- Preserve the current per-Shipment output fidelity: values, formulas, comments, merged cells, dimensions, styles, print areas, fixed images, product images, and their anchors.

## Architecture

Keep the existing single-Shipment transformation as the trusted unit. For every Shipment, produce and validate its transformed package in memory. A new combined-workbook assembler then builds one OOXML package: it uses the first transformed package for shared workbook resources and clones each selected worksheet's private relationship graph under collision-free part names.

The assembler updates `xl/workbook.xml`, `xl/_rels/workbook.xml.rels`, `[Content_Types].xml`, worksheet relationship targets, drawing relationships, print-area defined names, and worksheet titles as one coherent package. Only the configured invoice worksheet is exposed; source workbook sheets that are not Shipment results are not listed in the final workbook. This avoids cross-workbook APIs that can drop drawings, comments, or print settings.

The complete package is written to a temporary file in the selected output directory. Validation checks worksheet order and titles, Shipment ownership, relationship targets, required objects, and package readability. Atomic replace publishes the file only after every Shipment and the combined package pass validation.

## Naming

`combined_filename(source_filename)` operates on the user-visible original upload name, not the internal stored name `source.xlsx`. It strips the original extension, removes path components and control characters, and returns `已处理_<stem>.xlsx`. `next_available_path` performs a case-insensitive directory scan so behavior is safe on both macOS and Windows.

`worksheet_titles(requests)` preserves request order. It normalizes each preferred `<warehouse>-<shipment>` title and allocates `_2`, `_3`, etc. after truncating the base enough to keep the complete title within 31 characters.

## Service and persistence

The generation engine contract changes from returning multiple paths to returning one path. `GenerationResult` exposes `filename` and `sheet_count`.

Database schema version 2 adds a batch-level `output_path` to `processing_batches`. New v1.1 completions store the combined path once at batch level and keep Shipment snapshots without per-Shipment paths. Migration from schema version 1 is additive and preserves existing history. History reads the batch path first and falls back to legacy Shipment paths for older records.

The generation transaction remains all-or-nothing: failed generation records no successful path, does not update remembered output-folder or channel defaults, and removes temporary output.

## UI behavior

- The generate button describes generating one workbook rather than N invoices.
- Success reads `已生成 1 个工作簿，共包含 N 个货件 Sheet` and shows the generated filename and output directory.
- History shows one combined output file for new batches. Legacy v1 batches remain readable with their historical per-Shipment paths.
- The existing `打开文件夹` action remains. No summary or ZIP action is introduced.

## Errors and recovery

- Invalid or colliding worksheet titles are normalized automatically and do not block generation.
- Existing output files are never overwritten; suffix allocation is automatic.
- Any Shipment transform, relationship clone, combined validation, or final publish failure fails the whole batch and leaves no successful artifact.
- A missing historical output directory retains the current `文件已移动或删除` behavior.

## Testing

Tests cover title normalization and uniqueness, filename suffixing, multi-Sheet order and Shipment isolation, drawings and comments relationship integrity, print settings, atomic failure, schema migration, batch-level history, success copy, and the real browser flow. The existing complete suite remains the regression gate.

## Deferred work

Windows portable packaging and its double-click launcher are a separate delivery phase after this feature. GitHub push remains deferred until both v1.1 and the Windows portable deliverable are complete and accepted.
