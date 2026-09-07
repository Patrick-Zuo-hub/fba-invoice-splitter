# FBA Invoice Splitter Development Rules

This repository contains the FBA Invoice Splitter application implementation.

## Product baseline

- The product baseline is `Patrick-Zuo-hub/waymerce-prds`, path `FIS/v1`, tag `fis-v1.0-baseline`.
- Treat the external PRD as read-only. Never edit, move, rename, or delete files in the PRD repository.
- Record technical interpretations and DEV-TBC decisions under `docs/techspec/` or `docs/decisions/`.
- A product-scope change requires a new PRD version in the PRD repository; do not silently redefine business rules in code.

## Repository boundary

- Keep source code under `src/`, tests under `tests/`, sanitized fixtures under `samples/`, and technical documentation under `docs/`.
- Never commit credentials, customer files, production invoices, generated invoice archives, or personal data.
- Use anonymized or synthetic spreadsheet fixtures for automated tests.
