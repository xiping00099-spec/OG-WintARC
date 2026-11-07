"""export.py - XLSX export (openpyxl). Uses the same build_summaries() as the leaderboard and chart,
so the numbers always reconcile."""
import io
import json
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import leaderboard
import reports
import scoring

HEAD_FILL = PatternFill("solid", fgColor="1A1A1A")
HEAD_FONT = Font(bold=True, color="8CEB37")
TOTAL_FONT = Font(bold=True)
STATUS_FILLS = {
    scoring.GREEN: "8CEB37", scoring.HALF: "D4F0A0", scoring.RED: "EB3232", scoring.PENDING: "FFB020",
    scoring.HEALTH: "40AAF5", scoring.EMPTY: "EEEEEE",
}


def _sheet(wb, title, headers, rows, widths=None, date_cols=(), num_fmt=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for r in rows:
        ws.append(list(r))
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    for i, h in enumerate(headers, 1):
        w = (widths or {}).get(h, max(10, min(40, len(h) + 4)))
        ws.column_dimensions[get_column_letter(i)].width = w
    for col in date_cols:
        for cell in ws[get_column_letter(col)][1:]:
            cell.number_format = "yyyy-mm-dd"
    for col, fmt in (num_fmt or {}).items():
        for cell in ws[get_column_letter(col)][1:]:
            cell.number_format = fmt
    return ws


def _d(text):
    return datetime.strptime(text, "%Y-%m-%d").date() if text else None


def build_workbook(conn, session, generated_at, today=None) -> bytes:
    today = today or generated_at.date()
    sid = session["id"]
    summaries = reports.build_summaries(conn, session, today, only_active=False)
    ranked = leaderboard.rank(summaries, "net")
    rank_of = {s.participant_id: r for r, s in ranked}
    start, end = reports.session_range(session)
    wb = Workbook()
    wb.remove(wb.active)

    info = wb.create_sheet("Info")
    for row in [("Session", reports.session_title(session)), ("Group", session["group_name"]),
                ("Period", f"{start} to {end}"), ("Generated", generated_at.strftime("%Y-%m-%d %H:%M IST")),
                ("Full-green threshold (min)", session["full_green_minutes"]),
                ("Tolerance (min)", session["tolerance_minutes"]), ("Rounding policy", session["rounding_policy"]),
                ("Note", "Only admin-approved reports count toward points and statistics. "
                         "Totals exclude inactive participants.")]:
        info.append(row)
    info.column_dimensions["A"].width = 30
    info.column_dimensions["B"].width = 80
    for c in info["A"]:
        c.font = TOTAL_FONT

    # Participants
    rows = conn.execute(
        """SELECT p.*, m.is_active AS member_active FROM session_members m JOIN participants p ON p.id=m.participant_id
           WHERE m.session_id=? ORDER BY lower(p.display_name), p.id""", (sid,)).fetchall()
    _sheet(wb, "Participants", ["ID", "Telegram ID", "Username", "Name", "School type", "Registered", "Active", "In session"],
           [(r["id"], r["telegram_user_id"], r["username"], r["display_name"], r["school_type"], r["registered_at"],
             "Yes" if r["is_active"] else "No", "Yes" if r["member_active"] else "No") for r in rows],
           {"Name": 28, "Registered": 24})

    # Daily targets
    rows = conn.execute(
        """SELECT t.*, p.display_name FROM daily_targets t JOIN participants p ON p.id=t.participant_id
           WHERE t.session_id=? ORDER BY t.target_date, lower(p.display_name)""", (sid,)).fetchall()
    _sheet(wb, "Daily Targets", ["Participant", "Date", "Planned minutes", "Planned hours", "Physics Q", "Chemistry Q",
                                 "Biology Q", "Exercises", "Notes", "Version", "Late", "Submitted", "Updated"],
           [(r["display_name"], _d(r["target_date"]), r["planned_minutes"], round(r["planned_minutes"] / 60, 2),
             r["planned_physics"], r["planned_chemistry"], r["planned_biology"], r["planned_exercises"], r["notes"],
             r["version"], "Yes" if r["is_late"] else "No", r["submitted_at"], r["updated_at"]) for r in rows],
           {"Participant": 26, "Notes": 40, "Submitted": 24, "Updated": 24}, date_cols=(2,))

    # Daily reports
    rows = conn.execute(
        """SELECT r.*, p.display_name FROM daily_reports r JOIN participants p ON p.id=r.participant_id
           WHERE r.session_id=? ORDER BY r.report_date, lower(p.display_name)""", (sid,)).fetchall()
    _sheet(wb, "Daily Reports", ["Participant", "Date", "Status", "Independent min", "School min", "Total min",
                                 "Total hours", "Exercises", "Exercise names", "Physics Q", "Chemistry Q", "Biology Q",
                                 "Notes", "Submitted", "Reviewed by (admin ID)", "Review note", "Version"],
           [(r["display_name"], _d(r["report_date"]), r["status"], r["independent_minutes"], r["school_minutes"],
             reports.total_minutes(r), round(reports.total_minutes(r) / 60, 2), r["exercise_count"], r["exercise_names"],
             r["physics_q"], r["chemistry_q"], r["biology_q"], r["notes"], r["submitted_at"], r["reviewed_by"],
             r["review_note"], r["version"]) for r in rows],
           {"Participant": 26, "Exercise names": 30, "Notes": 40, "Submitted": 24, "Review note": 30}, date_cols=(2,))

    # Daily point breakdown (approved reports only)
    pr = []
    for s in sorted(summaries, key=lambda x: (x.name.casefold(), x.participant_id)):
        for day, rec in s.days.items():
            if rec.points is not None:
                p = rec.points
                pr.append((s.name, day, p.raw_minutes, p.hours_credited, p.study_points, p.exercise_points,
                           p.subject_points, p.gross, rec.penalty, p.gross - rec.penalty))
    pr.sort(key=lambda r: (r[1], r[0].casefold()))
    _sheet(wb, "Daily Points", ["Participant", "Date", "Verified minutes", "Credited hours", "Study pts", "Exercise pts",
                                "Subject pts", "Gross pts", "Penalty that day", "Net that day"], pr,
           {"Participant": 26}, date_cols=(2,))

    # Penalty ledger
    pen = reports.list_penalties(conn, sid)
    _sheet(wb, "Penalty Ledger", ["Penalty ID", "Participant", "Date", "Type", "Amount", "Reason", "Admin ID", "Automatic",
                                  "Created", "Status", "Reversed by", "Reversed at", "Reversal reason"],
           [(p["id"], p["display_name"], _d(p["penalty_date"]), p["kind"], p["amount"], p["reason"], p["admin_id"],
             "Yes" if p["is_auto"] else "No", p["created_at"], p["status"], p["reversed_by"], p["reversed_at"],
             p["reversal_reason"]) for p in pen],
           {"Participant": 26, "Reason": 40, "Created": 24, "Reversed at": 24, "Reversal reason": 30}, date_cols=(3,))

    # Summary: study hours, gross, penalties, net, streaks (+ totals via formulas over ACTIVE rows)
    ordered = [s for _, s in ranked] + [s for s in summaries if not s.active]
    headers = ["Rank", "Participant", "Active", "Verified hours", "Gross points", "Penalty deductions", "Net points",
               "Current streak", "Longest streak", "Full-green days", "Partial days", "Unsuccessful days", "Health days",
               "Pending reports", "Missing reports", "Avg study (h/day)", "Physics Q", "Chemistry Q", "Biology Q",
               "Exercises done", "Exercise days"]
    rows = [(rank_of.get(s.participant_id, ""), s.name, "Yes" if s.active else "No", round(s.verified_minutes / 60, 2),
             s.gross_points, s.penalty_total, s.net_points, s.current_streak, s.longest_streak, s.success_days,
             s.partial_days, s.unsuccessful_days, s.health_days, s.pending_reports, s.missing_reports,
             round(s.avg_minutes / 60, 2), s.physics_total, s.chemistry_total, s.biology_total, s.exercise_total,
             s.exercise_days) for s in ordered]
    ws = _sheet(wb, "Summary", headers, rows, {"Participant": 26}, num_fmt={4: "0.00", 16: "0.00"})
    if rows:
        n = len(rows) + 1
        tr = n + 1
        ws.cell(tr, 2, "TOTAL (active participants)").font = TOTAL_FONT
        for col in (4, 5, 6, 7, 10, 11, 12, 13, 14, 15, 17, 18, 19, 20, 21):
            L = get_column_letter(col)
            c = ws.cell(tr, col, f'=SUMIF($C$2:$C${n},"Yes",{L}2:{L}{n})')
            c.font = TOTAL_FONT
            c.number_format = "0.00" if col == 4 else "0"

    # Attendance history grid
    from itertools import islice
    days = list(scoring.daterange(start, end))
    ws = _sheet(wb, "Attendance", ["Participant", "Active"] + [d.strftime("%d %b") for d in days],
                [(s.name, "Yes" if s.active else "No", *[s.days[d].status for d in days]) for s in ordered],
                {"Participant": 26})
    for row in islice(ws.iter_rows(min_row=2), len(ordered)):
        for cell in row[2:]:
            color = STATUS_FILLS.get(cell.value)
            if color:
                cell.fill = PatternFill("solid", fgColor=color)
            cell.alignment = Alignment(horizontal="center")
    for i in range(3, 3 + len(days)):
        ws.column_dimensions[get_column_letter(i)].width = 9

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
