import hashlib
import json
import time
import os
import secrets
import threading
from functools import wraps
from datetime import datetime
from zoneinfo import ZoneInfo

import qrcode
import qrcode.image.svg
from flask import Flask, jsonify, render_template, request, Response, session, redirect, url_for
from itsdangerous import URLSafeSerializer, BadSignature
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0)
app.config.update(
    SECRET_KEY=os.environ.get("ATTENDANCE_SECRET") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=16 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("RENDER") == "true",
)
ledger_lock = threading.RLock()
SUBJECT = "Blockchain & Technology"
COLLEGE = "Bharat College of Engineering, Badlapur"
signer = URLSafeSerializer(app.config["SECRET_KEY"], salt="student-qr-v1")


class Block:

  def __init__(self, index, transactions, previous_hash, nonce=0):
    self.index = index
    self.timestamp = time.time()
    self.transactions = transactions
    self.previous_hash = previous_hash
    self.nonce = nonce
    self.hash = self.compute_hash()

  def compute_hash(self):
    payload = json.dumps(
        {
            "index": self.index,
            "timestamp": self.timestamp,
            "transactions": self.transactions,
            "previous_hash": self.previous_hash,
            "nonce": self.nonce,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class Ledger:

  def __init__(self):
    self.chain = [self.create_genesis_block()]
    self.pending_transactions = []

  def create_genesis_block(self):
    return Block(0, ["GENESIS_NODE_INIT"], "0" * 64)

  @property
  def last_block(self):
    return self.chain[-1]

  def add_transaction(self, student, class_session):
    records = self.pending_transactions + [tx for block in self.chain[1:] for tx in block.transactions]
    if any(tx["student_id"] == student["student_id"] and
           tx["session_id"] == class_session["id"] for tx in records):
      return False, "Already present for this session."
    self.pending_transactions.append({
        "student_id": student["student_id"], "name": student["name"],
        "timestamp": time.time(), "session_id": class_session["id"],
        "session_type": class_session["type"], "subject": SUBJECT,
        "semester": "VII", "status": "Present",
        "duration_minutes": class_session["duration_minutes"],
    })
    return True, f'{student["name"]} marked present for {class_session["type"].lower()}.'

  def mine_block(self):
    if not self.pending_transactions:
      return None
    new_block = Block(
        index=len(self.chain),
        transactions=self.pending_transactions,
        previous_hash=self.last_block.hash,
    )
    self.chain.append(new_block)
    self.pending_transactions = []
    return new_block


ledger = Ledger()
students = {str(roll): {"student_id": str(roll), "name": name} for roll, name in [
    (14, "Soham Dharap"), (15, "Shravani Dongre"),
    (33, "Vikas Jogdand"), (45, "Samir Maharana"),
]}
student_tokens = {}


def new_class(kind):
  started = time.time()
  duration = 60 if kind == "Lecture" else 120
  return {"id": secrets.token_hex(8), "type": kind, "subject": SUBJECT,
          "duration_minutes": duration, "started_at": started,
          "ends_at": started + duration * 60,
          "date": datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y")}


def class_info():
  return {**active_class, "is_active": time.time() < active_class["ends_at"]}


active_class = new_class("Lecture")


def normalize_name(value):
  return "".join(value.split()).casefold() if isinstance(value, str) else ""


def roll_number(value):
  if not isinstance(value, str) or not value.strip().isascii() or not value.strip().isdigit():
    return None
  value = value.strip()
  return str(int(value)) if len(value) <= 8 and int(value) > 0 else None


def body():
  data = request.get_json(silent=True)
  return data if isinstance(data, dict) else {}


def teacher_required(fn):
  @wraps(fn)
  def wrapped(*args, **kwargs):
    if not session.get("teacher"):
      return jsonify(message="Teacher login required."), 401
    return fn(*args, **kwargs)
  return wrapped


def student_required(fn):
  @wraps(fn)
  def wrapped(*args, **kwargs):
    if session.get("student_id") not in students:
      return jsonify(message="Student login required."), 401
    return fn(*args, **kwargs)
  return wrapped


@app.before_request
def protect_mutations():
  # Custom headers cannot be sent by cross-origin forms. No CORS is enabled.
  if request.path.startswith("/api/") and request.method == "POST":
    if request.headers.get("X-Requested-With") != "Provex":
      return jsonify(message="Please submit this request from the app."), 403


@app.after_request
def response_headers(response):
  response.headers["Cache-Control"] = "no-store"
  response.headers["X-Content-Type-Options"] = "nosniff"
  response.headers["Referrer-Policy"] = "same-origin"
  return response


@app.errorhandler(HTTPException)
def api_error(error):
  if request.path.startswith("/api/"):
    return jsonify(message=error.description), error.code
  return error


@app.route("/healthz")
def health():
  return jsonify(status="ok")


@app.route("/")
def index():
  return redirect(url_for("student_page"))


@app.route("/admin")
@app.route("/admin/dashboard")
def dashboard():
  if not session.get("teacher"):
    return render_template("login.html", role="teacher")
  return render_template("index.html")


@app.route("/student")
def student_page():
  if session.get("student_id") not in students:
    return render_template("login.html", role="student")
  return render_template("student.html", student=students[session["student_id"]])


@app.post("/api/login/teacher")
def teacher_login():
  data = body()
  user, password = data.get("username"), data.get("password")
  expected_user = os.environ.get("TEACHER_USER", "Payal Mam")
  expected_password = os.environ.get("TEACHER_PASSWORD", "BT#PT")
  if not isinstance(user, str) or not isinstance(password, str) or not (
      secrets.compare_digest(user.strip().encode(), expected_user.encode()) and
      secrets.compare_digest(password.encode(), expected_password.encode())):
    return jsonify(message="Incorrect teacher ID or password."), 401
  session.clear()
  session["teacher"] = True
  return jsonify(redirect="/admin/dashboard")


@app.post("/api/login/student")
def student_login():
  data = body()
  roll = roll_number(data.get("password"))
  student = students.get(roll)
  if not student or normalize_name(data.get("username")) != normalize_name(student["name"]):
    return jsonify(message="Name and roll number do not match a student profile. Ask your teacher to check your details."), 401
  session.clear()
  session["student_id"] = roll
  return jsonify(redirect="/student")


@app.post("/api/logout")
def logout():
  target = "/admin/dashboard" if session.get("teacher") else "/student"
  session.clear()
  return jsonify(redirect=target)


@app.route("/api/students", methods=["GET", "POST"])
@teacher_required
def roster():
  with ledger_lock:
    if request.method == "POST":
      data = body()
      roll, name = roll_number(data.get("student_id")), data.get("name")
      if not roll or not isinstance(name, str) or not name.strip() or len(name) > 120:
        return jsonify(message="Enter a positive roll number (up to 8 digits) and a full name (up to 120 characters)."), 400
      if roll in students:
        return jsonify(message="A profile with this roll number already exists."), 409
      students[roll] = {"student_id": roll, "name": " ".join(name.split())}
    return jsonify(students=sorted(students.values(), key=lambda s: int(s["student_id"])))


@app.route("/api/class", methods=["GET", "POST"])
@teacher_required
def class_session():
  global active_class
  with ledger_lock:
    if request.method == "POST":
      kind = body().get("type")
      if kind not in ("Lecture", "Practical"):
        return jsonify(message="Choose Lecture or Practical."), 400
      active_class = new_class(kind)
    return jsonify(session=class_info())


def get_student_token(roll):
  now = time.time()
  cached = student_tokens.get(roll)
  if not cached or now >= cached["expires_at"]:
    expires = now + 60
    payload = {"student_id": roll, "expires_at": expires, "nonce": secrets.token_hex(8)}
    cached = {"token": signer.dumps(payload), "expires_at": expires}
    student_tokens[roll] = cached
  return cached


@app.get("/api/student/qr")
@student_required
def student_qr():
  with ledger_lock:
    token = get_student_token(session["student_id"])
    return jsonify(**token, server_time=time.time())


@app.get("/api/student/qr.svg")
@student_required
def student_qr_image():
  with ledger_lock:
    token = get_student_token(session["student_id"])
    requested = request.args.get("token")
    if requested != token["token"]:
      return jsonify(message="QR expired. Wait for the new code."), 400
    image = qrcode.make(requested, image_factory=qrcode.image.svg.SvgPathImage,
                        border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    return Response(image.to_string(), mimetype="image/svg+xml")


@app.get("/api/student/status")
@student_required
def student_status():
  with ledger_lock:
    records = ledger.pending_transactions + [tx for b in ledger.chain[1:] for tx in b.transactions]
    present = any(tx["student_id"] == session["student_id"] and tx["session_id"] == active_class["id"] for tx in records)
    return jsonify(present=present, session=class_info())


@app.post("/api/scan")
@teacher_required
def scan_attendance():
  token = body().get("token")
  if not isinstance(token, str) or len(token) > 1024:
    return jsonify(message="Scan a valid student QR code."), 400
  try:
    payload = signer.loads(token)
    if not isinstance(payload, dict):
      raise ValueError()
    roll, expires = payload.get("student_id"), payload.get("expires_at")
    if not isinstance(roll, str) or not isinstance(expires, (int, float)):
      raise ValueError()
  except (BadSignature, ValueError, TypeError):
    return jsonify(message="Invalid QR code. Use the student's live Provex QR."), 400
  with ledger_lock:
    if time.time() >= active_class["ends_at"]:
      return jsonify(message="This class has ended. Start a new lecture or practical before scanning."), 409
    if time.time() >= expires:
      return jsonify(message="QR expired. Ask the student to show their refreshed code."), 400
    if roll not in students or student_tokens.get(roll, {}).get("token") != token:
      return jsonify(message="QR no longer valid. Ask the student to refresh their page."), 400
    success, message = ledger.add_transaction(students[roll], active_class)
    return jsonify(success=success, message=message, student=students[roll], session=class_info()), 200 if success else 409


@app.get("/api/ledger")
@teacher_required
def get_ledger():
  with ledger_lock:
    blocks = [{"index": b.index, "hash": b.hash, "previous_hash": b.previous_hash,
               "timestamp": b.timestamp, "transactions": b.transactions} for b in ledger.chain]
    valid = all(b.hash == b.compute_hash() and b.index == i and
                b.previous_hash == (ledger.chain[i - 1].hash if i else "0" * 64)
                for i, b in enumerate(ledger.chain))
    return jsonify(chain=blocks, pending=ledger.pending_transactions, integrity_valid=valid,
                   session=class_info(), student_count=len(students))


@app.post("/api/mine")
@teacher_required
def mine():
  with ledger_lock:
    block = ledger.mine_block()
    if not block:
      return jsonify(success=False, message="No pending records."), 400
    return jsonify(success=True, block_index=block.index, hash=block.hash)


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
