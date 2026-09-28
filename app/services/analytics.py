"""
Attendance analytics.

University of Mumbai regulations require **75% attendance** for a student to be
allowed to appear for the end-semester examination. Everything here is built
around answering that question precisely:

* how many lectures of this subject were actually *held* for my division?
* how many did I attend?
* am I above or below the 75% bar, and by how much?
* what happens to my percentage if I miss the next N lectures?

Attendance percentage is computed as::

    attended / held * 100

where ``attended`` counts PRESENT, LATE and faculty-approved MANUAL-present
records, and ``held`` counts the sessions conducted for the student's division.
Counting the divisor from *sessions held* (not from the number of records) is
what makes the number meaningful -- a student who never scanned in still has
a 0/12 record, not an undefined one.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Iterable

MU_MIN_ATTENDANCE = 75.0
CURRENT_YEAR = time.strftime("%Y")


def _counts_as_attended(record: dict[str, Any]) -> bool:
    status = record.get("status")
    if status in {"PRESENT", "LATE"}:
        return True
    if status == "MANUAL":
        return record.get("manual_status") in {"PRESENT", "LATE"}
    return False


def _sessions_for(
    sessions: Iterable[dict[str, Any]], *, subject_code: str, year: str, division: str
) -> list[dict[str, Any]]:
    return [
        session
        for session in sessions
        if session.get("subject_code") == subject_code
        and session.get("year") == year
        and session.get("division") == division
    ]


def student_subject_summary(
    *,
    student: dict[str, Any],
    subject: dict[str, Any],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attendance for one student in one subject."""
    held = _sessions_for(
        sessions,
        subject_code=subject["code"],
        year=student.get("year", ""),
        division=student.get("division", ""),
    )
    session_ids = {session["session_id"] for session in held}
    mine = [
        record
        for record in attendance
        if record.get("student_roll") == student["roll_no"]
        and record.get("session_id") in session_ids
    ]
    attended = [r for r in mine if _counts_as_attended(r)]
    held_count = len(held) or len(mine)
    percentage = (len(attended) / held_count * 100) if held_count else 0.0

    return {
        "subject_code": subject["code"],
        "subject_name": subject.get("name", subject["code"]),
        "credits": subject.get("credits"),
        "held": held_count,
        "attended": len(attended),
        "absent": max(0, held_count - len(attended)),
        "present": sum(1 for r in mine if r.get("status") == "PRESENT"),
        "late": sum(1 for r in mine if r.get("status") == "LATE"),
        "manual": sum(1 for r in mine if r.get("status") == "MANUAL"),
        "percentage": round(percentage, 2),
        "eligible": percentage >= MU_MIN_ATTENDANCE,
        "minimum_required": MU_MIN_ATTENDANCE,
        "lectures_to_recover": _lectures_to_recover(len(attended), held_count),
    }


def _lectures_to_recover(attended: int, held: int) -> int | None:
    """How many consecutive lectures must be attended to reach 75%.

    Solves ``(attended + x) / (held + x) >= 0.75`` for the smallest integer x.
    Returns ``None`` when the target is already met.
    """
    if held == 0:
        return 0
    if attended / held >= MU_MIN_ATTENDANCE / 100:
        return None
    x = 1
    while x < 10_000:
        if (attended + x) / (held + x) >= MU_MIN_ATTENDANCE / 100:
            return x
        x += 1
    return None


def student_report(
    *,
    student: dict[str, Any],
    subjects: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-subject breakdown plus an overall figure for one student."""
    rows = [
        student_subject_summary(
            student=student, subject=subject, sessions=sessions, attendance=attendance
        )
        for subject in subjects
    ]
    total_held = sum(row["held"] for row in rows)
    total_attended = sum(row["attended"] for row in rows)
    overall = (total_attended / total_held * 100) if total_held else 0.0
    defaulters = [row["subject_code"] for row in rows if not row["eligible"]]

    return {
        "student": {
            "roll_no": student.get("roll_no"),
            "name": student.get("name"),
            "year": student.get("year"),
            "division": student.get("division"),
            "department": student.get("department"),
            "address": student.get("address"),
        },
        "subjects": rows,
        "overall": {
            "held": total_held,
            "attended": total_attended,
            "percentage": round(overall, 2),
            "eligible": overall >= MU_MIN_ATTENDANCE and not defaulters,
            "minimum_required": MU_MIN_ATTENDANCE,
            "defaulting_subjects": defaulters,
        },
        "generated_at": time.time(),
    }


def defaulter_list(
    *,
    students: list[dict[str, Any]],
    subjects: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
    threshold: float = MU_MIN_ATTENDANCE,
) -> list[dict[str, Any]]:
    """Students below the 75% bar, worst first -- the report the HoD asks for."""
    defaulters: list[dict[str, Any]] = []
    for student in students:
        rows = [
            student_subject_summary(
                student=student, subject=subject, sessions=sessions, attendance=attendance
            )
            for subject in subjects
        ]
        total_held = sum(row["held"] for row in rows)
        total_attended = sum(row["attended"] for row in rows)
        if not total_held:
            continue
        percentage = total_attended / total_held * 100
        if percentage < threshold:
            defaulters.append(
                {
                    "roll_no": student.get("roll_no"),
                    "name": student.get("name"),
                    "year": student.get("year"),
                    "division": student.get("division"),
                    "held": total_held,
                    "attended": total_attended,
                    "percentage": round(percentage, 2),
                    "shortfall": round(threshold - percentage, 2),
                    "weak_subjects": [
                        row["subject_code"]
                        for row in rows
                        if row["held"] and not row["eligible"]
                    ],
                }
            )
    return sorted(defaulters, key=lambda row: row["percentage"])


def subject_summary(
    *,
    subject: dict[str, Any],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
    student_count: int,
) -> dict[str, Any]:
    held = [s for s in sessions if s.get("subject_code") == subject["code"]]
    relevant = [r for r in attendance if r.get("subject_code") == subject["code"]]
    attended = sum(1 for r in relevant if _counts_as_attended(r))
    capacity = len(held) * max(1, student_count)

    return {
        "subject_code": subject["code"],
        "subject_name": subject.get("name", subject["code"]),
        "semester": subject.get("semester"),
        "credits": subject.get("credits"),
        "sessions_held": len(held),
        "marks_recorded": len(relevant),
        "average_percentage": round(attended / capacity * 100, 2) if capacity else 0.0,
        "present": sum(1 for r in relevant if r.get("status") == "PRESENT"),
        "late": sum(1 for r in relevant if r.get("status") == "LATE"),
        "manual": sum(1 for r in relevant if r.get("status") == "MANUAL"),
        "last_session_at": max((s.get("started_at", 0) for s in held), default=None),
    }


def daily_trend(attendance: list[dict[str, Any]], days: int = 14) -> list[dict[str, Any]]:
    """Attendance volume per day -- feeds the dashboard bar chart."""
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"present": 0, "late": 0, "manual": 0})
    for record in attendance:
        stamp = record.get("marked_at")
        if not stamp:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(float(stamp)))
        status = record.get("status")
        if status == "PRESENT":
            buckets[day]["present"] += 1
        elif status == "LATE":
            buckets[day]["late"] += 1
        elif status == "MANUAL":
            buckets[day]["manual"] += 1

    today = time.time()
    series = []
    for offset in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(today - offset * 86400))
        row = buckets.get(day, {"present": 0, "late": 0, "manual": 0})
        series.append(
            {
                "date": day,
                "label": time.strftime("%d %b", time.localtime(today - offset * 86400)),
                "present": row["present"],
                "late": row["late"],
                "manual": row["manual"],
                "total": row["present"] + row["late"] + row["manual"],
            }
        )
    return series


def hourly_heatmap(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Marks grouped by hour of day -- useful for spotting odd scanning times."""
    buckets: dict[int, int] = defaultdict(int)
    for record in attendance:
        stamp = record.get("marked_at")
        if stamp:
            buckets[time.localtime(float(stamp)).tm_hour] += 1
    return [{"hour": hour, "count": buckets.get(hour, 0)} for hour in range(24)]


def _expected_marks(
    students: list[dict[str, Any]], sessions: list[dict[str, Any]]
) -> int:
    """How many attendance marks a fully-attended term would contain.

    One mark per enrolled student per lecture held. Sessions carry the year and
    division they were held for, so students are matched the same way the
    marking path matches them.
    """
    enrolled_by_group: dict[tuple[str, str], int] = {}
    for student in students:
        group = (str(student.get("year", "")), str(student.get("division", "")))
        enrolled_by_group[group] = enrolled_by_group.get(group, 0) + 1

    expected = 0
    for session in sessions:
        group = (str(session.get("year", "")), str(session.get("division", "")))
        expected += enrolled_by_group.get(group, 0)
    return expected


def dashboard_stats(
    *,
    students: list[dict[str, Any]],
    subjects: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
    chain_stats: dict[str, Any],
    anchors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything the faculty dashboard needs, in one payload."""
    attended = [r for r in attendance if _counts_as_attended(r)]
    today = time.strftime("%Y-%m-%d")
    today_marks = [
        r
        for r in attendance
        if time.strftime("%Y-%m-%d", time.localtime(float(r.get("marked_at", 0)))) == today
    ]
    per_subject = [
        subject_summary(
            subject=subject,
            sessions=sessions,
            attendance=attendance,
            student_count=len(students),
        )
        for subject in subjects
    ]

    return {
        "totals": {
            "students": len(students),
            "subjects": len(subjects),
            "sessions": len(sessions),
            "open_sessions": sum(1 for s in sessions if s.get("status") == "OPEN"),
            "attendance_records": len(attendance),
            "attended_records": len(attended),
            "marks_today": len(today_marks),
            "blocks": chain_stats.get("blocks", 0),
            "chain_height": chain_stats.get("height", 0),
            "anchors": len(anchors),
            "integrity": "VERIFIED" if chain_stats.get("last_report_valid", True) else "FAILED",
        },
        # Turnout is attended marks over the marks that COULD have been made.
        #
        # Dividing by len(attendance) -- as an earlier version did -- always
        # returns 100%, because a record only exists when a student actually
        # scans. An absent student leaves no row. The denominator therefore has
        # to be (lectures held x students enrolled), which is what the
        # University's 75% rule is measured against.
        "average_attendance_percentage": round(
            len(attended) / expected_marks * 100, 2
        )
        if (expected_marks := _expected_marks(students, sessions))
        else 0.0,
        "expected_marks": expected_marks,
        "chain": chain_stats,
        "subjects": per_subject,
        "trend": daily_trend(attendance),
        "hourly": hourly_heatmap(attendance),
        "recent_attendance": sorted(
            attendance, key=lambda r: r.get("marked_at", 0), reverse=True
        )[:25],
        "recent_anchors": anchors[:5],
        "generated_at": time.time(),
    }


def export_rows(
    *,
    students: list[dict[str, Any]],
    subjects: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten everything into CSV-friendly rows (one per student per subject)."""
    rows: list[dict[str, Any]] = []
    for student in students:
        for subject in subjects:
            summary = student_subject_summary(
                student=student, subject=subject, sessions=sessions, attendance=attendance
            )
            rows.append(
                {
                    "roll_no": student.get("roll_no"),
                    "name": student.get("name"),
                    "year": student.get("year"),
                    "division": student.get("division"),
                    "subject_code": subject["code"],
                    "subject_name": subject.get("name"),
                    "lectures_held": summary["held"],
                    "attended": summary["attended"],
                    "absent": summary["absent"],
                    "late": summary["late"],
                    "percentage": summary["percentage"],
                    "eligible_75": "YES" if summary["eligible"] else "NO",
                }
            )
    return rows


__all__ = [
    "MU_MIN_ATTENDANCE",
    "student_subject_summary",
    "student_report",
    "defaulter_list",
    "subject_summary",
    "daily_trend",
    "hourly_heatmap",
    "dashboard_stats",
    "export_rows",
]
