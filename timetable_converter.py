"""
Timetable Converter: Converts CP-SAT solver JSON output into an Excel timetable.

The function is flexible — it adapts to whatever time slots, days, batches,
and semesters exist in the JSON rather than hardcoding any values.
Each lecture slot is treated as 50 minutes.
"""

import json
from datetime import datetime, timedelta
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side, GradientFill
)
from openpyxl.utils import get_column_letter


# ── colour palette (matching the reference timetable aesthetic) ──────────────
HEADER_BG       = "1F4E79"   # dark navy   → title row
DAY_BG          = "2E75B6"   # medium blue → day column
BATCH_BG        = "BDD7EE"   # light blue  → semester label cells
BREAK_BG        = "FFD966"   # amber       → BREAK cells
THEORY_BG       = "DEEAF1"   # pale blue   → theory cells
LAB_BG          = "E2EFDA"   # pale green  → lab cells
EMPTY_BG        = "FFFFFF"   # white       → empty cells
GRID_COLOR      = "9DC3E6"   # soft blue   → cell borders

WHITE           = "FFFFFF"
DARK_TEXT       = "1F1F1F"
HEADER_FONT_CLR = "FFFFFF"


def _make_border(color=GRID_COLOR):
    side = Side(style="thin", color=color)
    return Border(left=side, right=side, top=side, bottom=side)


def _fill(hex_color):
    return PatternFill("solid", fgColor=hex_color)


def _parse_time(t: str) -> datetime:
    return datetime.strptime(t.strip(), "%H:%M")


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _build_time_slots(assignments: list, slot_minutes: int = 50) -> list[tuple[str, str]]:
    """
    Derive ordered unique time slots from all assignments.
    Each slot is slot_minutes wide starting at `start`.
    Returns a list of (start_str, end_str) tuples sorted by start time.
    """
    starts = set()
    for a in assignments:
        starts.add(_parse_time(a["start"]))

    slots = []
    for s in sorted(starts):
        e = s + timedelta(minutes=slot_minutes)
        slots.append((_fmt_time(s), _fmt_time(e)))
    return slots


def _build_time_slots_from_horizon(horizon: dict | None) -> list[tuple[str, str]]:
    """Build ordered unique slots directly from planning horizon data."""
    if not horizon:
        return []

    time_slots = horizon.get("timeSlots", [])
    unique_slots = {
        (slot["start"], slot["end"])
        for slot in time_slots
        if slot.get("start") and slot.get("end")
    }
    return sorted(unique_slots, key=lambda pair: _parse_time(pair[0]))


def _normalize_payload(json_data: dict | str) -> tuple[dict, dict]:
    """Accept either a raw solution JSON or a {problem, solution} payload."""
    if isinstance(json_data, str):
        json_data = json.loads(json_data)

    if "solution" in json_data:
        return json_data.get("solution", {}), json_data.get("problem", {})

    return json_data, {}


def _detect_break(slots: list[tuple[str, str]]) -> str | None:
    """
    Find a gap >= 20 min between consecutive slots; treat that as a break window.
    Returns the start time (str) of the gap, or None.
    """
    for i in range(len(slots) - 1):
        end_cur  = _parse_time(slots[i][1])
        start_nxt = _parse_time(slots[i + 1][0])
        gap = (start_nxt - end_cur).seconds // 60
        if gap >= 20:
            return _fmt_time(end_cur)     # break starts right after this slot
    return None


def _batch_sort_key(name: str) -> tuple:
    """Sort batches by semester number found in name, then alphabetically."""
    import re
    nums = re.findall(r"\d+", name)
    return (int(nums[0]) if nums else 99, name)


def json_to_timetable(
    json_data: dict | str,
    output_path: str = "timetable.xlsx",
    department: str = "Department of CS&IT",
    program: str = "BS AI-CySec",
    semester_label: str = "",
    slot_minutes: int = 50,
) -> str:
    """
    Convert a CP-SAT timetable JSON solution into a formatted Excel timetable.

    Parameters
    ----------
    json_data      : dict or JSON string – the solver output
    output_path    : where to write the .xlsx file
    department     : header line 1
    program        : header line 2 (programme name)
    semester_label : optional extra label, e.g. "Fall-2025"
    slot_minutes   : duration of one lecture slot in minutes (default 50)

    Returns
    -------
    output_path  (the file that was written)
    """
    solution_data, problem_data = _normalize_payload(json_data)
    assignments = solution_data.get("assignments", [])
    if not assignments:
        raise ValueError("No assignments found in JSON.")

    summaries  = solution_data.get("summaries", {})
    batch_info = {b["batchId"]: b["batchName"]
                  for b in summaries.get("studentGroupLoad", [])}
    room_info  = {r["roomId"]: r["roomName"]
                  for r in summaries.get("roomUsage", [])}
    planning_horizon = problem_data.get("planningHorizon") or solution_data.get("planningHorizon") or {}
    metadata = problem_data.get("metadata") or solution_data.get("metadata") or {}

    # ── Collect all unique days (preserve natural week order) ────────────────
    DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days_used  = sorted({a["dayId"] for a in assignments},
                        key=lambda d: DAY_ORDER.index(d) if d in DAY_ORDER else 99)

    # ── Collect all unique batches (sorted by semester) ──────────────────────
    batches_used = sorted(
        {a["batchId"] for a in assignments},
        key=lambda b: _batch_sort_key(batch_info.get(b, b))
    )

    # ── Build time slots from actual start times ──────────────────────────────
    slot_minutes = int(
        planning_horizon.get("slotDurationMinutes")
        or metadata.get("classDurationMinutes")
        or slot_minutes
    )
    slots = _build_time_slots_from_horizon(planning_horizon)
    if not slots:
        slots = _build_time_slots(assignments, slot_minutes)
    breaks = planning_horizon.get("breaks", [])
    break_after = breaks[0]["start"] if breaks else _detect_break(slots)

    # ── Index assignments: (batchId, dayId, start_str) → assignment ──────────
    index: dict = defaultdict(list)
    for a in assignments:
        key = (a["batchId"], a["dayId"], a["start"])
        index[key].append(a)

    # ── Build workbook ────────────────────────────────────────────────────────
    wb = Workbook()
    ws = wb.active
    ws.title = "Timetable"

    # Column layout:
    #   col 1 = Day  |  col 2 = Semester/Batch  |  col 3..N = time slots
    #   (plus an optional BREAK column inserted where the gap is)

    TIME_COL_START = 3                      # first time-slot column (1-indexed)
    n_time_cols    = len(slots)

    # Insert a BREAK column after the slot that ends at break_after
    break_col_index = None
    if break_after:
        for i, (s, e) in enumerate(slots):
            if e == break_after:
                break_col_index = TIME_COL_START + i + 1   # 1-based col for BREAK
                break

    def _slot_col(slot_idx: int) -> int:
        """Return the Excel column index for time slot slot_idx (0-based)."""
        col = TIME_COL_START + slot_idx
        if break_col_index and col >= break_col_index:
            col += 1     # shift right to make room for break column
        return col

    total_cols = TIME_COL_START - 1 + n_time_cols + (1 if break_col_index else 0)

    # ── Row 1: Title ─────────────────────────────────────────────────────────
    if department == "Department of CS&IT" and metadata.get("organizationName"):
        department = metadata["organizationName"]
    title_text = f"{department}  {program} Time Table"
    if semester_label:
        title_text += f" ({semester_label})"

    ws.merge_cells(start_row=1, start_column=1,
                   end_row=1,   end_column=total_cols)
    tc = ws.cell(1, 1, title_text)
    tc.font      = Font(bold=True, size=13, color=HEADER_FONT_CLR, name="Arial")
    tc.fill      = _fill(HEADER_BG)
    tc.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 28

    # ── Row 2: Column headers (Days | Semester | slot1 | BREAK | slot2 …) ───
    HDR_ROW = 2
    ws.cell(HDR_ROW, 1, "Days").font = Font(bold=True, color=WHITE, name="Arial")
    ws.cell(HDR_ROW, 1).fill        = _fill(HEADER_BG)
    ws.cell(HDR_ROW, 1).alignment   = Alignment(horizontal="center", vertical="center")

    ws.cell(HDR_ROW, 2, "Semester").font = Font(bold=True, color=WHITE, name="Arial")
    ws.cell(HDR_ROW, 2).fill             = _fill(HEADER_BG)
    ws.cell(HDR_ROW, 2).alignment        = Alignment(horizontal="center", vertical="center")

    for si, (s, e) in enumerate(slots):
        col = _slot_col(si)
        label = f"{s} - {e}"
        c = ws.cell(HDR_ROW, col, label)
        c.font      = Font(bold=True, color=WHITE, size=9, name="Arial")
        c.fill      = _fill(HEADER_BG)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    if break_col_index:
        c = ws.cell(HDR_ROW, break_col_index, "BREAK")
        c.font      = Font(bold=True, color=DARK_TEXT, size=9, name="Arial")
        c.fill      = _fill(BREAK_BG)
        c.alignment = Alignment(horizontal="center", vertical="center")

    ws.row_dimensions[HDR_ROW].height = 32
    border = _make_border()
    for col in range(1, total_cols + 1):
        ws.cell(HDR_ROW, col).border = border

    # ── Data rows ────────────────────────────────────────────────────────────
    current_row = HDR_ROW + 1

    for day in days_used:
        day_start_row = current_row
        n_batches     = len(batches_used)

        for bi, batch_id in enumerate(batches_used):
            batch_name = batch_info.get(batch_id, batch_id)
            row        = current_row

            # Semester / batch label cell
            bc = ws.cell(row, 2, batch_name)
            bc.font      = Font(bold=True, size=9, name="Arial", color=DARK_TEXT)
            bc.fill      = _fill(BATCH_BG)
            bc.alignment = Alignment(horizontal="center", vertical="center",
                                     wrap_text=True)
            bc.border    = border

            # Break column (same row)
            if break_col_index:
                brk = ws.cell(row, break_col_index, "BREAK")
                brk.fill      = _fill(BREAK_BG)
                brk.font      = Font(bold=True, size=9, name="Arial")
                brk.alignment = Alignment(horizontal="center", vertical="center")
                brk.border    = border

            # Fill each time slot
            for si, (s, e) in enumerate(slots):
                col     = _slot_col(si)
                matches = index.get((batch_id, day, s), [])

                if matches:
                    a        = matches[0]   # take first if multiple
                    subj     = a["subjectName"]
                    room     = room_info.get(a["roomId"], a.get("roomId", ""))
                    stype    = a.get("sessionType", "theory")
                    label    = f"{subj}\n({'Lab' if stype == 'lab' else 'Lec'}:{room})"
                    bg       = LAB_BG if stype == "lab" else THEORY_BG
                    cell     = ws.cell(row, col, label)
                    cell.fill = _fill(bg)
                    cell.font = Font(size=8, name="Arial", color=DARK_TEXT)
                else:
                    cell      = ws.cell(row, col, "")
                    cell.fill = _fill(EMPTY_BG)

                cell.alignment = Alignment(horizontal="center", vertical="center",
                                           wrap_text=True)
                cell.border    = border

            ws.row_dimensions[row].height = 48
            current_row += 1

        # Day label spans all batch rows
        ws.merge_cells(start_row=day_start_row, start_column=1,
                       end_row=current_row - 1, end_column=1)
        day_cell = ws.cell(day_start_row, 1, day)
        day_cell.font      = Font(bold=True, color=WHITE, size=11, name="Arial")
        day_cell.fill      = _fill(DAY_BG)
        day_cell.alignment = Alignment(horizontal="center", vertical="center")
        day_cell.border    = border

    # ── Column widths ─────────────────────────────────────────────────────────
    ws.column_dimensions["A"].width = 10   # Days
    ws.column_dimensions["B"].width = 16   # Semester
    for si in range(len(slots)):
        col_letter = get_column_letter(_slot_col(si))
        ws.column_dimensions[col_letter].width = 22
    if break_col_index:
        ws.column_dimensions[get_column_letter(break_col_index)].width = 9

    # ── Freeze panes (header + day/semester columns) ──────────────────────────
    ws.freeze_panes = ws.cell(HDR_ROW + 1, TIME_COL_START)

    wb.save(output_path)
    print(f"Timetable saved to {output_path}")
    return output_path


# ── CLI / demo ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python timetable_converter.py <input.json> [output.xlsx]")
        sys.exit(1)

    json_path  = sys.argv[1]
    out_path   = sys.argv[2] if len(sys.argv) > 2 else "timetable_output.xlsx"

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    json_to_timetable(
        json_data      = data,
        output_path    = out_path,
        department     = "Department of CS&IT",
        program        = "BS AI-CySec",
        semester_label = "Fall-2025",
    )
