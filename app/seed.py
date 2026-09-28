"""Demo data.

``python manage.py seed``   the 120-student register
``python manage.py demo``   the register plus four weeks of sealed lectures

Names are generated from a fixed list with a fixed seed, so the same roll
numbers always belong to the same names.
"""

from __future__ import annotations

import random
import time

FIRST_NAMES = [
    "Ansh", "Aarya", "Kunal", "Soham", "Tanvi", "Rohit", "Isha", "Aryan", "Neha", "Vivaan",
    "Sanika", "Pranav", "Meera", "Aditya", "Diya", "Rohan", "Kiara", "Arjun", "Sara", "Yash",
    "Nupur", "Dhruv", "Anaya", "Kabir", "Riya", "Om", "Shreya", "Varun", "Pooja", "Nikhil",
]
LAST_NAMES = [
    "Vaze", "Halde", "Dhamale", "Medley", "Patil", "Jadhav", "Shinde", "More", "Kulkarni", "Pawar",
    "Joshi", "Deshmukh", "Chavan", "Bhosale", "Naik", "Gaikwad", "Salvi", "Mhatre", "Rane", "Kadam",
]


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def seed_register(db, settings, count: int = 120) -> int:
    """Create the student register (roll numbers BCOE23AI001 ...)."""
    rng = random.Random(20260101)
    for position in range(1, count + 1):
        roll_no = f"{settings.college_short}23AI{position:03d}"
        db.add_student(
            roll_no=roll_no,
            name=_name(rng),
            division="A" if position <= 60 else "B",
            year="BE",
        )
    return count


def seed_demo(db, settings, weeks: int = 4) -> dict:
    """The register, plus past lectures that are already sealed into blocks.

    Each past lecture marks a random 80-95% of its division present, then the
    session is closed, which mines a block. This is what makes the chain page
    interesting on a fresh deployment, and it gives the verify page records that
    are genuinely in the chain.
    """
    if db.is_empty():
        seed_register(db, settings)

    rng = random.Random(7)
    students = db.students()
    now = time.time()
    sessions = 0
    records = 0

    for week in range(weeks, 0, -1):
        for day, subject in enumerate(settings.subjects[:3]):
            when = now - (week * 7 + day) * 86400
            session = db.open_session(subject["code"], room="Lab 204", minutes=60, at=when)
            present = rng.sample(students, int(len(students) * rng.uniform(0.80, 0.95)))
            for student in present:
                db.mark(session["id"], student["roll_no"], at=when + 600)
                records += 1
            db.close_session(session["id"], at=when + 3600)
            sessions += 1

    # One lecture left open, so the QR page has something to show immediately.
    live = db.open_session(settings.subjects[0]["code"], room="Lab 204", minutes=30)
    for student in rng.sample(students, 12):
        db.mark(live["id"], student["roll_no"])
        records += 1

    return {"students": len(students), "sessions": sessions + 1, "records": records}
