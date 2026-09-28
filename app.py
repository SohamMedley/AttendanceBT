import hashlib
import hmac
import json
import time
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
SECRET_KEY = b"kalyan_attendance_node_secret_2026"


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
    # Avoid duplicate attendance in current batch
    for tx in self.pending_transactions:
      if tx["student_id"] == student_id:
        return False, "Attendance already recorded in pending block"

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


def generate_qr_token():
  window = int(time.time() // 15)  # 15s epoch
  sig = hmac.new(
      SECRET_KEY, f"SESSION_ROOM_A_{window}".encode(), hashlib.sha256
  ).hexdigest()[:12]
  return f"ATT-{window}-{sig}"


@app.route("/")
def index():
  return render_template("index.html")


@app.route("/api/qr-epoch")
def qr_epoch():
  remaining = 15 - int(time.time() % 15)
  return jsonify({"token": generate_qr_token(), "ttl_seconds": remaining})


@app.route("/api/scan", methods=["POST"])
def scan_attendance():
  data = request.json or {}
  token = data.get("token")
  student_id = data.get("student_id")
  student_name = data.get("student_name")

  current_token = generate_qr_token()
  # Allow current or immediate previous epoch (anti-latency grace window)
  prev_window = int((time.time() - 15) // 15)
  prev_sig = hmac.new(
      SECRET_KEY, f"SESSION_ROOM_A_{prev_window}".encode(), hashlib.sha256
  ).hexdigest()[:12]
  prev_token = f"ATT-{prev_window}-{prev_sig}"

  if token not in (current_token, prev_token):
    return jsonify({"success": False, "message": "QR Expired or Forged"}), 400

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
  blocks_data = []
  for b in ledger.chain:
    blocks_data.append({
        "index": b.index,
        "hash": b.hash,
        "previous_hash": b.previous_hash,
        "timestamp": b.timestamp,
        "transactions": b.transactions,
    })
  return jsonify({
      "chain": blocks_data,
      "pending": ledger.pending_transactions,
  })


@app.route("/api/mine", methods=["POST"])
def mine():
  block = ledger.mine_block()
  if not block:
    return jsonify({"success": False, "message": "No pending transactions"}), 400
  return jsonify({"success": True, "block_index": block.index, "hash": block.hash})


if __name__ == "__main__":
  app.run(debug=True, port=5000)