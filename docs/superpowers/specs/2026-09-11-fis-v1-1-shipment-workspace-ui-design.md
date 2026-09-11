# FIS V1.1 Shipment Workspace UI Design

**Status:** approved for prototype  
**Date:** 2026-09-11  
**Scope:** FBA Invoice Splitter single-page shipment review, enrichment, and generation workspace  
**Figma:** https://www.figma.com/design/ZSHmX4UCUBQiG4lKXSeKhY

## 1. Decision summary

The primary FIS workflow is a shipment workspace, not a batch form. After a source workbook is parsed, the system lists every detected FBA Shipment ID as an independent row. The user can identify a shipment from its carton count and product names, then maintain that shipment's logistics data directly in the table.

The workspace keeps batch editing as a convenience for four fields, but batch values never replace the per-shipment model. Each shipment retains its own saved values and can be edited again after a batch operation.

The interface remains a single-page, local-only web tool. It uses a clean blue-and-white desktop layout, an Excel-like wide table, compact progressive sections, and a fixed generation bar.

## 2. Information architecture

The page contains five regions in reading order:

1. Application header with the product name and entries for History and Master Data.
2. Parsed-file bar showing the workbook name, detected shipment count, and re-upload action.
3. Shipment-mapping bar showing the mapping result and an action to edit the mapping again.
4. Shipment workspace containing the wide editable table.
5. Fixed bottom action bar containing completeness, output-folder selection, and invoice generation.

History and Master Data open as right-side drawers. Closing a drawer returns the user to the same table position and preserves horizontal and vertical scroll.

## 3. File and mapping flow

Before parsing, the file section presents a single prominent workbook selector. After successful parsing it collapses into a compact file-information bar.

The mapping section expands after parsing. The pasted two-column format is:

1. Warehouse Code
2. Shipment ID

This order matches the user's operational spreadsheet. When every shipment maps successfully, the section collapses into a success summary. Missing, duplicate, or unknown mappings keep the section open and show row-level guidance. The user can reopen a successful mapping through “重新编辑映射”.

## 4. Shipment table

The table is optimized for approximately 20 shipments. It keeps the header and the first three columns visible while the remaining columns scroll horizontally.

| Order | Column | Source and behavior |
| --- | --- | --- |
| 1 | Shipment ID | Parsed from the workbook; read-only. |
| 2 | FBA total cartons | Sum of carton quantities for the shipment; read-only. |
| 3 | Chinese product names | Show the first two names followed by “等 N 种产品”; click to expand all names in the row. |
| 4 | FBA warehouse code | Initially supplied by pasted mapping; editable per shipment. |
| 5 | Warehouse full address | Selected from master data. For an unknown code, allow inline address entry and “保存到主数据”. |
| 6 | Shipping channel | Selected from master data. Default to the most recent successful value for the warehouse when available; otherwise blank. |
| 7 | Delivery date | Default to the nearest Thursday that is not in the past; editable per shipment. |
| 8 | Sailing date | Default to delivery date plus seven days. A manual change locks the value against later automatic updates. |
| 9 | Delivery warehouse | Selected from master data. |
| 10 | Completeness | Derived status showing complete, incomplete, or field error. |

The main table contains no row-selection checkboxes. All listed shipments are the current working batch.

## 5. Editing and saving

Cells display clean text or a placeholder by default. Clicking an editable cell changes it into the appropriate control: text input, date picker, or master-data selector.

Selection or entry saves immediately. The page shows a lightweight progression from “保存中” to “已保存”. A failed save keeps the entered value visible, marks only the affected field, and offers a retry without discarding unrelated edits.

Orange indicates missing information that blocks generation. Red is reserved for an actual validation or save error. Complete rows use a quiet green status.

## 6. Batch editing

Batch editing is available only for:

- Shipping channel
- Delivery date
- Sailing date
- Delivery warehouse

Each eligible column header contains a small “批量” action directly beside the field label. Activating it opens a compact popover anchored above that column. The popover places its field label above the input, then provides clear and confirm actions.

Confirming applies the value to every shipment in the current table and immediately saves the affected rows. The resulting values remain individually editable. The confirmation copy explicitly states that all shipments will be changed.

## 7. Master-data behavior

Warehouse address, shipping channel, and delivery warehouse values come from local master data.

When the pasted warehouse code is recognized, its address is filled automatically. When it is not recognized, the user enters the full address in that shipment row and can select “保存到主数据” so future shipments using the code can match automatically.

The right-side Master Data drawer supports review and maintenance without leaving the current workspace.

## 8. Output folder and generation

On first use, the output folder has no default. The user must choose a folder before generation.

After a successful generation, FIS records only that output folder path. On later uses, the bottom bar pre-fills the last successfully used folder if it still exists and is writable. Selecting a folder without completing generation does not replace the stored last-used folder.

The fixed bottom bar always shows:

- the number of complete shipments out of the total;
- the current output path or a “选择输出文件夹” prompt;
- a “更改文件夹” action when a path exists;
- the primary “生成 N 份发票” action.

Generation remains disabled until all shipments are complete and a valid output folder is present. After success, the workspace stays visible and the primary follow-up action becomes “打开文件夹”.

## 9. Visual direction

The approved direction is restrained and operational:

- light gray application background;
- white cards and table surfaces;
- blue primary actions and active states;
- compact controls with generous horizontal alignment;
- minimal borders, subtle shadows, and no decorative imagery;
- dense enough for 20 shipments without resembling a technical admin console.

The table is the visual center of the page. Upload and mapping become compact after completion so they do not compete with shipment editing. Batch actions remain discoverable in column headers but visually secondary to per-row values.

## 10. Layout and scrolling

The target is a desktop viewport suitable for Windows and macOS laptop screens. The page shell does not require a mobile layout.

- Application header remains visible.
- Table header remains visible during vertical scrolling.
- Shipment ID, carton count, and product names remain visible during horizontal scrolling.
- The bottom generation bar remains fixed.
- Popovers stay anchored to their triggering column and do not obscure the active input.

## 11. Acceptance scenarios

1. After parsing a workbook, the user can see every Shipment ID as a separate row and distinguish shipments using total cartons and product names.
2. A pasted `Warehouse Code + Shipment ID` mapping populates the correct rows regardless of table order.
3. The user can edit any shipment independently without affecting the others.
4. A batch edit changes all shipments only after explicit confirmation, and any row can then be overridden.
5. Product names remain compact until the user expands a row.
6. Known warehouse codes fill addresses from master data; unknown codes support inline entry and optional master-data saving.
7. Delivery and sailing defaults follow the approved date rules, including the manual sailing-date lock.
8. Every successful edit visibly reaches “已保存”; a failed edit is isolated to its field.
9. First use requires output-folder selection; later use reuses only the last successful writable folder.
10. Generation is available only when every shipment is complete, and success offers “打开文件夹” without navigating away.

## 12. Non-goals

- Mobile or tablet-first editing.
- Multi-user collaboration, accounts, roles, or cloud synchronization.
- Selecting a subset of shipment rows for batch operations.
- Manually editing calculated carton totals.
- Free-text alternatives for fields designated as master-data selections.
- Changing the frozen FIS V1.0 PRD baseline as part of this design.
