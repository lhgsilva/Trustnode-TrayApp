"""Historian export endpoint.

Operator request 2026-06-12: ship a real Excel export from the
Historian page with optional template placeholders so the customer
can pre-style a beautiful workbook (logo, fonts, colours, header
rows) and have the data just *land* in the right cells.

The endpoint accepts a list of pre-rendered rows (already filtered
client-side from the visible historianRows) plus a column spec, and
returns an .xlsx file the browser triggers download for.

Template mode:
  The operator uploads a `.xlsx` whose cells contain text-mode
  placeholders. Two kinds:
    - Single cell placeholder: any cell containing  e.g. "{{ts}}"
      OR "Report generated at {{ts}}" -- the substring is replaced
      with the corresponding value from the FIRST data row when the
      cell sits OUTSIDE the loop block, OR the current row when it
      sits INSIDE the loop block.
    - Loop block: a cell whose value is exactly "{{#each}}" marks
      the start of the data-loop row. The first cell on a later row
      with value "{{/each}}" marks the end. Every row BETWEEN those
      two markers is duplicated once per data row and placeholders
      inside the block resolve to that row's values.

  Anything else in the workbook (logos, headers, styled cells,
  formulas) is preserved.
"""

from __future__ import annotations

import base64
import io
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/historian", tags=["historian"])


class ExportColumn(BaseModel):
    key: str
    label: str


class ExportXlsxRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[ExportColumn] = Field(default_factory=list)
    template_xlsx_b64: str | None = None
    template_name: str | None = None


_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_./-]+)\s*\}\}")


def _resolve_placeholder(text: str, row: dict[str, Any]) -> str:
    """Replace every {{key}} in `text` with row[key] (stringified).

    Unknown keys are left untouched so the operator can spot typos in
    their template.
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key in row:
            v = row[key]
            return "" if v is None else str(v)
        # Allow case-insensitive lookup against labels (operator types
        # "{{Tag}}" against a column labelled "Tag").
        for k in row.keys():
            if str(k).lower() == key.lower():
                v = row[k]
                return "" if v is None else str(v)
        return match.group(0)

    return _PLACEHOLDER_PATTERN.sub(_replace, str(text or ""))


def _build_plain_xlsx(rows: list[dict[str, Any]], columns: list[ExportColumn]) -> bytes:
    """Header row + data rows, no styling. Used when no template was
    uploaded."""
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - dep missing
        raise HTTPException(status_code=500, detail=f"openpyxl missing: {exc}")
    wb = Workbook()
    ws = wb.active
    ws.title = "Historian"
    if columns:
        ws.append([c.label for c in columns])
    else:
        # Fallback: derive from first row keys.
        if rows:
            ws.append(list(rows[0].keys()))
            columns = [ExportColumn(key=k, label=k) for k in rows[0].keys()]
    for r in rows:
        ws.append([r.get(c.label, r.get(c.key, "")) for c in columns])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.read()


def _build_templated_xlsx(
    rows: list[dict[str, Any]],
    columns: list[ExportColumn],
    template_b64: str,
) -> bytes:
    """Load the operator's .xlsx, apply placeholder substitutions,
    repeat the loop block once per row."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dep missing
        raise HTTPException(status_code=500, detail=f"openpyxl missing: {exc}")
    try:
        raw = base64.b64decode(template_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid template base64: {exc}")
    try:
        wb = load_workbook(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not open template: {exc}")

    ws = wb.active

    # Identify the loop block. We look for the FIRST cell containing
    # exactly "{{#each}}" (trimmed) — its row is the loop-start row.
    # Then we find the FIRST cell at or after that row containing
    # "{{/each}}" — its row is the loop-end row. Inclusive on both
    # ends. If no markers found, every placeholder is single-row.
    loop_start: int | None = None
    loop_end: int | None = None
    for row in ws.iter_rows():
        for cell in row:
            v = cell.value
            if not isinstance(v, str):
                continue
            txt = v.strip()
            if loop_start is None and txt == "{{#each}}":
                loop_start = cell.row
            elif loop_start is not None and loop_end is None and txt == "{{/each}}":
                loop_end = cell.row
                break
        if loop_end is not None:
            break

    # Build the global row context — first data row's values, used to
    # fill placeholders OUTSIDE the loop.
    first_row = rows[0] if rows else {}

    def _apply_to_cell(cell, ctx: dict[str, Any]) -> None:
        v = cell.value
        if not isinstance(v, str):
            return
        new_v = _resolve_placeholder(v, ctx)
        # Don't overwrite when the cell was a pure marker token —
        # those should become empty so the report doesn't show the
        # literal "{{#each}}" text.
        if v.strip() in ("{{#each}}", "{{/each}}"):
            cell.value = ""
            return
        cell.value = new_v

    if loop_start is None or loop_end is None:
        # No loop: just substitute every placeholder using the first
        # row context. If the template has none, the operator gets
        # back exactly what they uploaded.
        for row in ws.iter_rows():
            for cell in row:
                _apply_to_cell(cell, first_row)
    else:
        # 1. Build the loop template as a list of "row patterns".
        loop_template_rows: list[list[Any]] = []
        for r_idx in range(loop_start, loop_end + 1):
            row_cells = []
            for cell in ws[r_idx]:
                row_cells.append(cell.value)
            loop_template_rows.append(row_cells)

        # 2. Substitute non-loop cells with the first row context.
        for row in ws.iter_rows():
            if loop_start <= row[0].row <= loop_end:
                continue
            for cell in row:
                _apply_to_cell(cell, first_row)

        # 3. Clear the loop block rows on the sheet — we'll re-emit
        #    them below.
        for r_idx in range(loop_start, loop_end + 1):
            for cell in ws[r_idx]:
                cell.value = None

        # 4. Insert one set of loop rows per data row, starting at
        #    loop_start. We append to the sheet at the bottom since
        #    inserting rows in-place would invalidate styling refs.
        #    Simpler: write to fresh rows starting at loop_start.
        write_row = loop_start
        from copy import copy as shallow_copy  # for cell styles
        for data_row in rows:
            for pattern_row in loop_template_rows:
                for c_idx, val in enumerate(pattern_row, start=1):
                    cell = ws.cell(row=write_row, column=c_idx)
                    if isinstance(val, str):
                        cell.value = _resolve_placeholder(val, data_row)
                    else:
                        cell.value = val
                write_row += 1

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.read()


@router.post("/export-xlsx")
def export_xlsx(payload: ExportXlsxRequest) -> StreamingResponse:
    rows = payload.rows or []
    columns = payload.columns or []
    if payload.template_xlsx_b64:
        data = _build_templated_xlsx(rows, columns, payload.template_xlsx_b64)
    else:
        data = _build_plain_xlsx(rows, columns)
    name = payload.template_name or "historian.xlsx"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name))[:80] or "historian.xlsx"
    headers = {
        "Content-Disposition": f'attachment; filename="{safe}"',
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return StreamingResponse(iter([data]), media_type=headers["Content-Type"], headers=headers)
