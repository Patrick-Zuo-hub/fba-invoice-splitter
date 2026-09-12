"""Build and publish one workbook containing every Shipment worksheet."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from fis.xlsx.generator import ShipmentOutput

_FORBIDDEN_SHEET_CHARACTERS = re.compile(r"[\[\]:*?/\\]")
_MAX_SHEET_TITLE = 31


def combined_filename(source_filename: str) -> str:
    """Return the v1.1 output name derived from the user-visible upload name."""

    display = source_filename.replace("\\", "/").split("/")[-1]
    stem = Path(display).stem
    stem = "".join(character for character in stem if character.isprintable()).strip(
        " .'_"
    )
    if not stem:
        stem = "发票"
    return f"已处理_{stem}.xlsx"


def next_output_path(output_dir: Path, source_filename: str) -> Path:
    """Return the first available output path without overwriting case variants."""

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
    """Allocate legal, deterministic and case-insensitively unique Sheet titles."""

    occupied: set[str] = set()
    result: list[str] = []
    for request in requests:
        preferred = f"{request.warehouse_code}-{request.shipment_id}"
        base = _FORBIDDEN_SHEET_CHARACTERS.sub("_", preferred).strip().strip("'")
        base = base or "货件"
        base = base[:_MAX_SHEET_TITLE]
        candidate = base
        index = 2
        while candidate.casefold() in occupied:
            suffix = f"_{index}"
            candidate = f"{base[: _MAX_SHEET_TITLE - len(suffix)]}{suffix}"
            index += 1
        occupied.add(candidate.casefold())
        result.append(candidate)
    return tuple(result)


__all__ = ["combined_filename", "next_output_path", "worksheet_titles"]
