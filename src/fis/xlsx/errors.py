"""Errors raised while inspecting uploaded workbook packages."""

from dataclasses import dataclass


@dataclass(slots=True)
class WorkbookError(Exception):
    """A user-facing workbook validation error with a stable machine code."""

    code: str
    message: str
    location: str | None = None

    def __str__(self) -> str:
        return self.message
