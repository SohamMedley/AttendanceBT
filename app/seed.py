"""
Seed data for Bharat College of Engineering (BCOE), Badlapur.

Subject codes are the real University of Mumbai Rev-2019 'C' scheme codes for
CSE (AI & ML):

* Third Year, Semester V  -- CSC501 Computer Networks, CSC502 Web Computing,
  CSC503 Artificial Intelligence, CSDLO5011 Statistics for AI & DS,
  CSDLO5012 Advanced Algorithms, CSDLO5013 Internet of Things
* Fourth Year, Semester VII -- CSC701 Deep Learning, CSC702 Big Data Analytics,
  CSDO7022 **Blockchain Technologies**, CSDO7011 Natural Language Processing

``Blockchain Technologies (CSDO7022)`` is the Department Optional Course-4 that
this project is submitted for, so it is the default subject in the demo.

Student names are procedurally generated from common Indian name lists and are
obviously fictional; roll numbers follow the ``BCOE<year><branch><serial>``
convention with a 60-student intake, matching the department's AICTE-approved
intake.

Everything is deterministic (seeded), so a demo run always reproduces the same
register and the same history.
"""

from __future__ import annotations

import random
import time
from typing import Any

from .blockchain import ecdsa

SEED = 20260929

DEPARTMENT = "Computer Science & Engineering (AI & ML)"
BRANCH_CODE = "AI"

FIRST_NAMES = [
    "Aarav", "Aarya", "Aditi", "Aditya", "Advait", "Akanksha", "Akshay", "Amruta",
    "Ananya", "Aniket", "Anjali", "Ansh", "Anuja", "Arjun", "Arnav", "Ashwini",
    "Atharva", "Avani", "Bhavesh", "Chaitali", "Chinmay", "Darshan", "Devanshi",
    "Dhruv", "Ganesh", "Gauri", "Harsh", "Harshada", "Ishaan", "Ishita",
    "Janhavi", "Jayesh", "Kabir", "Kalyani", "Kartik", "Kavya", "Ketaki", "Kunal",
    "Lavanya", "Madhura", "Mahesh", "Manasi", "Mayur", "Meera", "Mihir", "Mitali",
    "Mohit", "Mrunal", "Nachiket", "Namrata", "Neha", "Nikhil", "Niranjan",
    "Omkar", "Pallavi", "Parth", "Pooja", "Pranav", "Prasad", "Prathamesh",
    "Priya", "Rachana", "Rahul", "Rajeshwari", "Rakesh", "Rasika", "Riddhi",
    "Rohan", "Rohini", "Rutuja", "Sagar", "Sahil", "Sakshi", "Sameer", "Samruddhi",
    "Sanika", "Sarthak", "Saurabh", "Shalmali", "Sharvari", "Shreyas", "Shruti",
    "Siddhesh", "Simran", "Sneha", "Soham", "Sohini", "Sujal", "Sumedh", "Suyash",
    "Swara", "Tanmay", "Tejas", "Trisha", "Tushar", "Vaibhav", "Vaishnavi",
    "Vedant", "Vidhi", "Vighnesh", "Vinit", "Yash", "Yashasvi", "Yogita", "Zoya",
]

SURNAMES = [
    "Adhav", "Ahire", "Ambekar", "Bane", "Bhagat", "Bhalerao", "Bhandari",
    "Bhise", "Bhoir", "Chavan", "Chaudhari", "Dalvi", "Dandekar", "Deshmukh",
    "Deshpande", "Dhage", "Dhamale", "Dubey", "Gadhave", "Gaikwad", "Gavhane",
    "Ghadge", "Gholap", "Gore", "Gujar", "Halde", "Ingle", "Jadhav", "Jagtap",
    "Jain", "Joshi", "Kadam", "Kale", "Kamble", "Kandi", "Kapse", "Karande",
    "Karkhanis", "Kasar", "Kashid", "Kene", "Khadse", "Kharat", "Khedkar",
    "Koli", "Kulkarni", "Kumbhar", "Lad", "Mahadik", "Mahajan", "Mane", "Mhatre",
    "Mirashi", "Mishra", "Mohite", "More", "Mujawar", "Naik", "Nalawade",
    "Narkar", "Nemade", "Palkar", "Pandit", "Parab", "Pardeshi", "Patil",
    "Pawar", "Pednekar", "Phadke", "Phatak", "Pol", "Prajapati", "Rane",
    "Raut", "Redij", "Sable", "Salvi", "Sanghavi", "Sankpal", "Sarnaik",
    "Sawant", "Sharma", "Shelar", "Shetty", "Shinde", "Shirke", "Shukla",
    "Sonawane", "Suryawanshi", "Tambe", "Tandel", "Thakur", "Thorat", "Tiwari",
    "Tondwalkar", "Vaidya", "Varma", "Vartak", "Vaze", "Waghmare", "Wagh",
    "Yadav", "Zarekar",
]

FACULTY_SEED = [
    {
        "faculty_id": "BCOE-FAC-001",
        "name": "Prof. Vijayalaxmi Tadkal",
        "role": "Head of Department",
        "designation": "Associate Professor & HoD",
        "email": "hod.aiml@bharatedu.co.in",
        "subjects": ["CSDO7022", "CSDLO5012"],
    },
    {
        "faculty_id": "BCOE-FAC-002",
        "name": "Prof. Sunil Kulkarni",
        "role": "faculty",
        "designation": "Assistant Professor",
        "email": "sunil.kulkarni@bharatedu.co.in",
        "subjects": ["CSC701", "CSDLO5011"],
    },
    {
        "faculty_id": "BCOE-FAC-003",
        "name": "Prof. Meenakshi Rao",
        "role": "faculty",
        "designation": "Assistant Professor",
        "email": "meenakshi.rao@bharatedu.co.in",
        "subjects": ["CSC702", "CSDLO5013"],
    },
    {
        "faculty_id": "BCOE-FAC-004",
        "name": "Prof. Anil Deshpande",
        "role": "faculty",
        "designation": "Assistant Professor",
        "email": "anil.deshpande@bharatedu.co.in",
        "subjects": ["CSC501", "CSDO7011"],
    },
    {
        "faculty_id": "BCOE-FAC-005",
        "name": "Prof. Shruti Nair",
        "role": "faculty",
        "designation": "Assistant Professor",
        "email": "shruti.nair@bharatedu.co.in",
        "subjects": ["CSC502"],
    },
    {
        "faculty_id": "BCOE-FAC-006",
        "name": "Prof. Ramesh Patil",
        "role": "faculty",
        "designation": "Assistant Professor",
        "email": "ramesh.patil@bharatedu.co.in",
        "subjects": ["CSC503", "CSDO7021"],
    },
]


def subject_catalogue() -> list[dict[str, Any]]:
    """University of Mumbai Rev-2019 'C' scheme subjects for CSE (AI & ML)."""
    semester_five = [
        ("CSC501", "Computer Networks", 3, "Theory", "BCOE-FAC-004", 4),
        ("CSC502", "Web Computing", 3, "Theory", "BCOE-FAC-005", 3),
        ("CSC503", "Artificial Intelligence", 3, "Theory", "BCOE-FAC-006", 4),
        ("CSDLO5011", "Statistics for Artificial Intelligence & Data Science", 3, "Theory", "BCOE-FAC-002", 3),
        ("CSDLO5012", "Advanced Algorithms", 3, "Theory", "BCOE-FAC-001", 4),
        ("CSDLO5013", "Internet of Things", 3, "Theory", "BCOE-FAC-003", 3),
    ]
    semester_seven = [
        ("CSC701", "Deep Learning", 3, "Theory", "BCOE-FAC-002", 4),
        ("CSC702", "Big Data Analytics", 3, "Theory", "BCOE-FAC-003", 4),
        ("CSDO7022", "Blockchain Technologies", 3, "Department Optional-4", "BCOE-FAC-001", 3),
        ("CSDO7011", "Natural Language Processing", 3, "Department Optional-3", "BCOE-FAC-004", 3),
        ("CSDO7021", "User Experience Design with VR", 3, "Department Optional-4", "BCOE-FAC-006", 3),
    ]

    subjects: list[dict[str, Any]] = []
    for code, name, credits, kind, faculty_id, hours in semester_five:
        subjects.append(
            {
                "code": code,
                "name": name,
                "department": DEPARTMENT,
                "semester": 5,
                "year": "TE",
                "credits": credits,
                "type": kind,
                "faculty_ids": [faculty_id],
                "lectures_per_week": hours,
                "min_attendance_percent": 75,
                "default_room": "Lab 301" if "Lab" in kind or code.startswith("CSDLO") else "Classroom 204",
                "scheme": "REV-2019 'C' Scheme (University of Mumbai)",
            }
        )
    for code, name, credits, kind, faculty_id, hours in semester_seven:
        subjects.append(
            {
                "code": code,
                "name": name,
                "department": DEPARTMENT,
                "semester": 7,
                "year": "BE",
                "credits": credits,
                "type": kind,
                "faculty_ids": [faculty_id],
                "lectures_per_week": hours,
                "min_attendance_percent": 75,
                "default_room": "Lab 305",
                "scheme": "REV-2019 'C' Scheme (University of Mumbai)",
            }
        )
    return subjects


def _generate_students(year: str, batch: str, admission_year: int, count: int = 60) -> list[dict[str, Any]]:
    rng = random.Random(SEED + admission_year)
    students: list[dict[str, Any]] = []
    used: set[str] = set()
    for serial in range(1, count + 1):
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)}"
            if name not in used:
                used.add(name)
                break
        roll_no = f"BCOE{str(admission_year)[2:]}{BRANCH_CODE}{serial:03d}"
        keypair = ecdsa.keypair_from_private(
            # Deterministic demo key from the seeded RNG, so re-seeding gives the
            # same wallet address for the same student.
            rng.getrandbits(200) + 12345
        )
        students.append(
            {
                "roll_no": roll_no,
                "name": name,
                "department": DEPARTMENT,
                "year": year,
                "division": "A",
                "batch": batch,
                "semester": 7 if year == "BE" else 5,
                "email": f"{roll_no.lower()}@bharatedu.co.in",
                "phone_masked": f"+91-XXXXXX{rng.randint(1000, 9999)}",
                "admission_year": admission_year,
                "public_key": keypair.public_hex,
                "address": keypair.address,
                "active": True,
                "guardian_contact": f"+91-XXXXXX{rng.randint(1000, 9999)}",
                "hostel": rng.random() < 0.25,
                "created_at": time.time(),
            }
        )
    return students


def build_seed_payload(*, students_per_year: int = 60) -> dict[str, Any]:
    """Everything needed to populate a fresh register."""
    current = time.localtime().tm_year
    students = _generate_students("BE", f"{current - 3}-{current + 1}", current - 3, students_per_year)
    students += _generate_students("TE", f"{current - 2}-{current + 2}", current - 2, students_per_year)

    faculty: list[dict[str, Any]] = []
    for record in FACULTY_SEED:
        keypair = ecdsa.keypair_from_private(
            random.Random(SEED + sum(ord(c) for c in record["faculty_id"])).getrandbits(200) + 777
        )
        faculty.append(
            {
                **record,
                "department": DEPARTMENT,
                "public_key": keypair.public_hex,
                "address": keypair.address,
                "active": True,
                "created_at": time.time(),
            }
        )

    return {
        "college": {
            "name": "Bharat College of Engineering",
            "short_name": "BCOE",
            "address": "Kanhor, Badlapur (W), Thane, Maharashtra 421503",
            "affiliation": "University of Mumbai",
        },
        "students": students,
        "faculty": faculty,
        "subjects": subject_catalogue(),
    }


__all__ = ["build_seed_payload", "subject_catalogue", "DEPARTMENT", "FACULTY_SEED"]
