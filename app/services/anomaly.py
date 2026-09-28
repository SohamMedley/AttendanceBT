"""
Rule-based anomaly detection for attendance fraud.

The classic attacks on a QR attendance system, and the signature each one leaves:

======================  =====================================================
Attack                  Evidence in the data
======================  =====================================================
Proxy marking           the same device or IP marks several different students
                        inside one session
Screenshot sharing      marks cluster in the first seconds of a time slot, all
                        from one IP, well after the lecture started
Scripted submissions    suspiciously uniform client latency, machine-like
                        inter-arrival times
Away-from-class         a student marks attendance in two places at the same
                        moment, or marks while the session is elsewhere
Sudden outsiders        a student who never attends suddenly marks in a session
                        with an unusually low overall turnout
======================  =====================================================

This module deliberately uses **transparent rules**, not a black box. Every alert
states the rule, the observed value, the threshold, and the records that
triggered it, so a faculty member can act on it and a student can contest it.

That transparency is also the honest engineering answer: with a few thousand
records there is not enough labelled data to train a reliable supervised model,
and an unexplainable flag that can fail a student is worse than no flag. The
``AnomalyScorer`` interface is the seam where a future ML model (isolation
forest, autoencoder) would plug in without touching the rest of the system.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from typing import Any, Iterable

SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"


class AnomalyScorer:
    """Extension point for a future ML-based detector."""

    name = "rule-based"

    def score(self, records: list[dict[str, Any]]) -> float:  # pragma: no cover - interface
        raise NotImplementedError


def _group(records: Iterable[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    buckets: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        buckets[record.get(key)].append(record)
    return buckets


def detect_shared_devices(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same device fingerprint used by different students in one session."""
    alerts: list[dict[str, Any]] = []
    by_session = _group(attendance, "session_id")
    for session_id, records in by_session.items():
        by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            fingerprint = record.get("device_fingerprint") or ""
            if fingerprint:
                by_device[fingerprint].append(record)
        for fingerprint, group in by_device.items():
            rolls = sorted({r.get("student_roll") for r in group})
            if len(rolls) > 1:
                alerts.append(
                    {
                        "rule": "SHARED_DEVICE",
                        "severity": SEVERITY_HIGH,
                        "session_id": session_id,
                        "subject_code": group[0].get("subject_code"),
                        "observed": len(rolls),
                        "threshold": 1,
                        "message": (
                            f"One device submitted attendance for {len(rolls)} students "
                            f"in the same lecture: {', '.join(rolls)}"
                        ),
                        "evidence": {
                            "device_fingerprint": fingerprint,
                            "students": rolls,
                            "tx_ids": [r.get("tx_id") for r in group],
                        },
                    }
                )
    return alerts


def detect_burst(attendance: list[dict[str, Any]], *, window_seconds: float = 20.0) -> list[dict[str, Any]]:
    """A suspicious burst of marks in a very short window (screenshot sharing)."""
    alerts: list[dict[str, Any]] = []
    for session_id, records in _group(attendance, "session_id").items():
        stamps = sorted(float(r.get("marked_at", 0)) for r in records)
        for index in range(len(stamps) - 5):
            span = stamps[index + 5] - stamps[index]
            if 0 < span <= window_seconds:
                alerts.append(
                    {
                        "rule": "BURST",
                        "severity": SEVERITY_MEDIUM,
                        "session_id": session_id,
                        "subject_code": records[0].get("subject_code"),
                        "observed": round(span, 2),
                        "threshold": window_seconds,
                        "message": (
                            f"6 students marked within {span:.1f}s of each other -- "
                            "possible shared QR screenshot."
                        ),
                        "evidence": {
                            "window_start": stamps[index],
                            "window_end": stamps[index + 5],
                        },
                    }
                )
                break
    return alerts


def detect_scripted_clients(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identical client latencies suggest an automated script rather than a phone."""
    alerts: list[dict[str, Any]] = []
    latencies = [
        int(r["client_latency_ms"])
        for r in attendance
        if r.get("client_latency_ms") is not None
    ]
    if len(latencies) < 8:
        return alerts
    try:
        deviation = statistics.pstdev(latencies)
    except statistics.StatisticsError:  # pragma: no cover
        return alerts
    if deviation == 0:
        alerts.append(
            {
                "rule": "SCRIPTED_CLIENT",
                "severity": SEVERITY_MEDIUM,
                "session_id": None,
                "observed": 0.0,
                "threshold": 1.0,
                "message": (
                    f"All {len(latencies)} submissions report an identical client "
                    "latency -- consistent with a scripted client."
                ),
                "evidence": {"samples": len(latencies), "latency_ms": latencies[0]},
            }
        )
    return alerts


def detect_duplicate_device_students(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One student rolling across many devices -- a device-sharing ring."""
    alerts: list[dict[str, Any]] = []
    thresholds = 4
    for roll, records in _group(attendance, "student_roll").items():
        devices = {r.get("device_fingerprint") for r in records if r.get("device_fingerprint")}
        if len(devices) > thresholds:
            alerts.append(
                {
                    "rule": "MANY_DEVICES",
                    "severity": SEVERITY_LOW,
                    "session_id": None,
                    "student_roll": roll,
                    "observed": len(devices),
                    "threshold": thresholds,
                    "message": (
                        f"{roll} has marked attendance from {len(devices)} different "
                        "devices. Verify the student is not sharing credentials."
                    ),
                    "evidence": {"devices": sorted(devices)},
                }
            )
    return alerts


def detect_impossible_overlap(attendance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same student marked in two different sessions at the same instant."""
    alerts: list[dict[str, Any]] = []
    for roll, records in _group(attendance, "student_roll").items():
        ordered = sorted(records, key=lambda r: float(r.get("marked_at", 0)))
        for previous, current in zip(ordered, ordered[1:]):
            gap = float(current.get("marked_at", 0)) - float(previous.get("marked_at", 0))
            if (
                0 <= gap < 60
                and previous.get("session_id") != current.get("session_id")
            ):
                alerts.append(
                    {
                        "rule": "IMPOSSIBLE_OVERLAP",
                        "severity": SEVERITY_HIGH,
                        "student_roll": roll,
                        "observed": round(gap, 2),
                        "threshold": 60,
                        "message": (
                            f"{roll} was marked present in two different lectures "
                            f"{gap:.0f}s apart."
                        ),
                        "evidence": {
                            "first": previous.get("tx_id"),
                            "second": current.get("tx_id"),
                            "first_session": previous.get("session_id"),
                            "second_session": current.get("session_id"),
                        },
                    }
                )
    return alerts


def detect_low_turnout_outliers(
    attendance: list[dict[str, Any]], sessions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Sessions where one student attended but almost nobody else did."""
    alerts: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    for record in attendance:
        counts[record.get("session_id")] += 1

    for session in sessions:
        session_id = session.get("session_id")
        marked = counts.get(session_id, 0)
        expected = int(session.get("expected_count") or 0)
        if expected >= 10 and marked:
            ratio = marked / expected
            if ratio < 0.15:
                alerts.append(
                    {
                        "rule": "LOW_TURNOUT",
                        "severity": SEVERITY_LOW,
                        "session_id": session_id,
                        "subject_code": session.get("subject_code"),
                        "observed": round(ratio, 3),
                        "threshold": 0.15,
                        "message": (
                            f"Only {marked} of {expected} students marked attendance "
                            f"for {session.get('subject_code')} -- verify the session "
                            "ran as scheduled."
                        ),
                        "evidence": {"marked": marked, "expected": expected},
                    }
                )
    return alerts


def detect_late_start_scans(
    attendance: list[dict[str, Any]], *, seconds: float = 5.0
) -> list[dict[str, Any]]:
    """Marks landing in the first seconds of a lecture (before anyone could sit)."""
    alerts: list[dict[str, Any]] = []
    per_session = _group(attendance, "session_id")
    for session_id, records in per_session.items():
        starts = [float(r.get("session_started_at", 0)) for r in records]
        if not starts:
            continue
        started = statistics.median(starts)
        immediate = [
            r for r in records if float(r.get("marked_at", 0)) - started < seconds
        ]
        if len(immediate) >= 3:
            alerts.append(
                {
                    "rule": "INSTANT_SCANS",
                    "severity": SEVERITY_LOW,
                    "session_id": session_id,
                    "observed": len(immediate),
                    "threshold": 3,
                    "message": (
                        f"{len(immediate)} students marked within {seconds:.0f}s of the "
                        "session opening -- unusual if the lecture had not started."
                    ),
                    "evidence": {"tx_ids": [r.get("tx_id") for r in immediate]},
                }
            )
    return alerts


#: All detectors, run together by :func:`scan`.
DETECTORS = (
    detect_shared_devices,
    detect_impossible_overlap,
    detect_burst,
    detect_scripted_clients,
    detect_duplicate_device_students,
)


def scan(
    *,
    attendance: list[dict[str, Any]],
    sessions: list[dict[str, Any]] | None = None,
    include_low_turnout: bool = True,
) -> dict[str, Any]:
    """Run every detector and summarise the results."""
    alerts: list[dict[str, Any]] = []
    for detector in DETECTORS:
        try:
            alerts.extend(detector(attendance))
        except Exception as exc:  # pragma: no cover - a bad detector must not break the page
            alerts.append(
                {
                    "rule": "DETECTOR_ERROR",
                    "severity": SEVERITY_LOW,
                    "message": f"{detector.__name__} failed: {exc}",
                    "observed": None,
                    "threshold": None,
                    "evidence": {},
                }
            )

    if include_low_turnout and sessions:
        alerts.extend(detect_low_turnout_outliers(attendance, sessions))
        alerts.extend(detect_late_start_scans(attendance))

    order = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
    alerts.sort(key=lambda alert: order.get(alert.get("severity"), 3))

    return {
        "scanned_records": len(attendance),
        "alert_count": len(alerts),
        "by_severity": {
            severity: sum(1 for a in alerts if a.get("severity") == severity)
            for severity in (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW)
        },
        "alerts": alerts,
        "detectors": [detector.__name__ for detector in DETECTORS],
        "generated_at": time.time(),
    }


__all__ = ["scan", "AnomalyScorer", "DETECTORS"]
