"""The pages, and the whole mark-with-a-QR-code journey."""

from __future__ import annotations


def prepare(client, db):
    """A register and one open lecture, the way the app would have it."""
    db.add_student("BCOE23AI001", "Ansh Vaze")
    db.add_student("BCOE23AI002", "Aarya Halde")
    return db.open_session("CSDO7022", room="Lab 204")


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
def test_a_fresh_deployment_is_sent_to_setup(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]


def test_setup_creates_the_register(client):
    response = client.post("/setup", follow_redirects=True)
    assert response.status_code == 200
    assert b"Teacher console" in response.data


def test_the_setup_page_loads(client):
    assert client.get("/setup").status_code == 200


def test_every_page_renders(client, db):
    prepare(client, db)
    for path in ("/", "/console", "/students", "/chain", "/verify", "/setup"):
        response = client.get(path)
        assert response.status_code == 200, path


def test_the_session_page_shows_a_qr_code(client, db):
    session = prepare(client, db)
    response = client.get(f"/session/{session['id']}")
    assert response.status_code == 200
    assert b"<svg" in response.data                  # the QR code is inline SVG
    assert b"Close and seal into a block" in response.data


def test_an_unknown_session_is_a_404(client, db):
    prepare(client, db)
    assert client.get("/session/does-not-exist").status_code == 404


def test_the_register_page_lists_students(client, db):
    prepare(client, db)
    response = client.get("/students")
    assert b"BCOE23AI001" in response.data
    assert b"Ansh Vaze" in response.data


def test_a_student_can_be_added_from_the_register_page(client, db):
    prepare(client, db)
    client.post("/students", data={"roll_no": "BCOE23AI003", "name": "Kunal Dhamale"})
    assert b"Kunal Dhamale" in client.get("/students").data


# --------------------------------------------------------------------------
# the journey: open a session, scan the code, mark, seal
# --------------------------------------------------------------------------
def test_the_whole_attendance_flow(client, db):
    session = prepare(client, db)

    # 1. the projector page hands out a valid token
    token = client.get(f"/api/session/{session['id']}/token").get_json()
    assert token["ok"] and token["qr"].startswith("<svg")

    # 2. the student's phone opens the scanned link
    scan = client.get(
        f"/scan?s={session['id']}&w={token['window']}&sig={token['signature']}"
    )
    assert scan.status_code == 200
    assert b"Mark me present" in scan.data

    # 3. and marks
    marked = client.post(
        "/api/mark",
        json={
            "session_id": session["id"],
            "window": token["window"],
            "signature": token["signature"],
            "roll_no": "BCOE23AI001",
        },
    )
    assert marked.status_code == 200
    assert marked.get_json()["record"]["status"] == "PRESENT"

    # 4. it shows up on the projector page
    live = client.get(f"/api/session/{session['id']}/records").get_json()
    assert live["count"] == 1

    # 5. and the teacher seals the lecture into a block
    client.post(f"/session/{session['id']}/close")
    assert len(db.chain.blocks) == 2
    assert db.chain.is_valid()[0]

    # 6. which can then be verified, with a Merkle proof
    proof = client.get("/api/prove?roll_no=BCOE23AI001").get_json()
    assert proof["ok"] and proof["proof"]["proof_valid"] is True


def test_marking_without_a_valid_token_is_refused(client, db):
    session = prepare(client, db)
    response = client.post(
        "/api/mark",
        json={"session_id": session["id"], "window": 0, "signature": "0" * 16, "roll_no": "BCOE23AI001"},
    )
    assert response.status_code == 400
    assert "expired" in response.get_json()["error"]


def test_a_scan_of_a_stale_code_says_so_on_the_page(client, db):
    session = prepare(client, db)
    response = client.get(f"/scan?s={session['id']}&w=1&sig=abc")
    assert b"expired" in response.data.lower()


def test_a_closed_roll_call_cannot_be_marked(client, db):
    session = prepare(client, db)
    token = client.get(f"/api/session/{session['id']}/token").get_json()
    client.post(f"/session/{session['id']}/close")

    response = client.post(
        "/api/mark",
        json={
            "session_id": session["id"],
            "window": token["window"],
            "signature": token["signature"],
            "roll_no": "BCOE23AI001",
        },
    )
    assert response.status_code == 400
    assert "closed" in response.get_json()["error"]


def test_an_unknown_roll_number_is_refused_with_a_clear_message(client, db):
    session = prepare(client, db)
    token = client.get(f"/api/session/{session['id']}/token").get_json()
    response = client.post(
        "/api/mark",
        json={
            "session_id": session["id"],
            "window": token["window"],
            "signature": token["signature"],
            "roll_no": "BCOE23AI999",
        },
    )
    assert response.status_code == 400
    assert "register" in response.get_json()["error"]


# --------------------------------------------------------------------------
# verification endpoints
# --------------------------------------------------------------------------
def test_the_chain_check_passes(client, db):
    prepare(client, db)
    body = client.post("/api/verify", json={}).get_json()
    assert body["ok"] is True
    assert "intact" in body["message"]


def test_a_proof_for_an_unsealed_student_explains_why(client, db):
    prepare(client, db)
    response = client.get("/api/prove?roll_no=BCOE23AI001")
    assert response.status_code == 404
    assert "sealed" in response.get_json()["error"].lower()


def test_the_proof_endpoint_needs_a_roll_number(client, db):
    prepare(client, db)
    assert client.get("/api/prove?roll_no=").status_code == 400


def test_health_reports_the_storage_backend(client, db):
    prepare(client, db)
    body = client.get("/api/health").get_json()
    assert body["ok"] is True
    assert body["storage"] == "local-json"
    assert body["stats"]["students"] == 2


def test_the_chain_page_lists_blocks_after_sealing(client, db):
    session = prepare(client, db)
    db.mark(session["id"], "BCOE23AI001")
    client.post(f"/session/{session['id']}/close")

    page = client.get("/chain")
    assert b"Block #1" in page.data
    assert b"Block #0" in page.data
