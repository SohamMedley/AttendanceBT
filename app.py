import hashlib
import hmac
import json
import time
import os
import secrets
import threading
from datetime import datetime, timezone
import qrcode
import qrcode.image.svg
from flask import Flask, jsonify, render_template, request, Response
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# The preview proxy terminates HTTPS before forwarding to Flask.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0)
SECRET_KEY = os.environ.get("ATTENDANCE_SECRET", secrets.token_hex(32)).encode()
ledger_lock = threading.RLock()
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


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

  def add_transaction(self, student_id, student_name):
    # Avoid duplicates across pending and sealed records for the UTC day
    records = self.pending_transactions + [tx for block in self.chain[1:] for tx in block.transactions]
    today = datetime.now(timezone.utc).date()
    for tx in records:
      if tx["student_id"] == student_id and datetime.fromtimestamp(tx["timestamp"], timezone.utc).date() == today:
        return False, "Attendance already recorded today"

    self.pending_transactions.append({
        "student_id": student_id,
        "name": student_name,
        "timestamp": time.time(),
    })
    return True, "Verified and queued into ledger"

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


def generate_qr_token(window=None):
  window = int(time.time() // 15) if window is None else window  # 15s epoch
  sig = hmac.new(
      SECRET_KEY, f"SESSION_ROOM_A_{window}".encode(), hashlib.sha256
  ).hexdigest()[:12]
  return f"ATT-{window}-{sig}"


@app.route("/")
def index():
  return render_template("index.html")


@app.route("/api/qr-epoch")
def qr_epoch():
  now = time.time()
  remaining = 15 - int(now % 15)
  return jsonify({"token": generate_qr_token(int(now // 15)), "ttl_seconds": remaining})


@app.route("/api/qr.svg")
def qr_image():
  window = int(time.time() // 15)
  token = request.args.get("epoch") or generate_qr_token(window)
  if token not in (generate_qr_token(window), generate_qr_token(window - 1)):
    return jsonify(message="QR expired. Please refresh the session."), 400
  image = qrcode.make(request.host_url + "?token=" + token, image_factory=qrcode.image.svg.SvgPathImage, border=4, error_correction=qrcode.constants.ERROR_CORRECT_H)
  return Response(image.to_string(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.after_request
def no_cache(response):
  if request.path.startswith("/api/"):
    response.headers["Cache-Control"] = "no-store"
  return response


@app.route("/api/scan", methods=["POST"])
def scan_attendance():
  data = request.get_json(silent=True)
  if not isinstance(data, dict):
    return jsonify(success=False, message="A JSON object is required"), 400
  token = data.get("token")
  student_id = data.get("student_id")
  student_name = data.get("student_name")
  if not all(isinstance(value, str) and value.strip() for value in (token, student_id, student_name)):
    return jsonify(success=False, message="Token, student ID, and name are required"), 400
  student_id, student_name = student_id.strip().upper(), student_name.strip()
  if len(student_id) > 64 or len(student_name) > 120 or len(token) > 100:
    return jsonify(success=False, message="Input is too long"), 400

  window = int(time.time() // 15)
  if not any(hmac.compare_digest(token, generate_qr_token(w)) for w in (window, window - 1)):
    return jsonify(success=False, message="QR expired. Scan the current code and try again."), 400

  with ledger_lock:
    success, msg = ledger.add_transaction(student_id, student_name)
  if not success:
    return jsonify({"success": False, "message": msg}), 409

  return jsonify({
      "success": True,
      "message": msg,
      "pending_count": len(ledger.pending_transactions),
  })


@app.route("/api/ledger")
def get_ledger():
  with ledger_lock:
    blocks_data = [{
        "index": b.index,
        "hash": b.hash,
        "previous_hash": b.previous_hash,
        "timestamp": b.timestamp,
        "transactions": b.transactions,
    } for b in ledger.chain]
    valid = all(
        b.hash == b.compute_hash() and b.index == i and
        b.previous_hash == (ledger.chain[i - 1].hash if i else "0" * 64)
        for i, b in enumerate(ledger.chain)
    )
    return jsonify(chain=blocks_data, pending=ledger.pending_transactions,
                   integrity_valid=valid)


@app.route("/api/mine", methods=["POST"])
def mine():
  with ledger_lock:
    block = ledger.mine_block()
  if not block:
    return jsonify({"success": False, "message": "No pending transactions"}), 400
  return jsonify({"success": True, "block_index": block.index, "hash": block.hash})


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))