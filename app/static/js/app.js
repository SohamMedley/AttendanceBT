/* ============================================================================
   The only JavaScript in the project.

   Four small jobs, each used on exactly one page:
     startSessionPage()  rotate the QR code on the projector, refresh the list
     startScanPage()     read the QR code with the phone camera
     startVerifyPage()   ask the server to check the chain / prove a record
     startSearch()       filter the register table

   Everything else is server-rendered HTML.
   ========================================================================== */

function el(id) { return document.getElementById(id); }

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}


/* ----------------------------------------------- the projector page -------- */
/*
 * The token changes every QR_TOKEN_TTL seconds. Rather than reloading the page
 * (which would flash the slides), we ask for the next one slightly early and
 * replace the SVG. The list of who has marked refreshes at the same time.
 */
function startSessionPage(sessionId) {
  const box = el("qr");
  const countdown = el("countdown");
  const list = el("list");
  const count = el("count");
  let secondsLeft = parseInt((countdown || {}).textContent || "30", 10);

  async function nextToken() {
    try {
      const data = await getJSON(`/api/session/${encodeURIComponent(sessionId)}/token`);
      if (box && data.qr) box.innerHTML = data.qr;
      secondsLeft = data.ttl;
    } catch (error) {
      if (countdown) countdown.textContent = "?";
    }
  }

  async function refreshList() {
    try {
      const data = await getJSON(`/api/session/${encodeURIComponent(sessionId)}/records`);
      if (count) count.textContent = data.count;
      if (!list || !data.records.length) return;
      list.innerHTML =
        "<table><thead><tr><th>Roll no.</th><th>Name</th><th>Marked at</th></tr></thead><tbody>" +
        data.records.map((r) =>
          `<tr><td class="mono">${escapeHtml(r.roll_no)}</td>` +
          `<td>${escapeHtml(r.name)}</td>` +
          `<td>${new Date(r.marked_at * 1000).toLocaleTimeString()}</td></tr>`
        ).join("") +
        "</tbody></table>";
    } catch (error) { /* the page keeps working with what it already has */ }
  }

  setInterval(() => {
    secondsLeft -= 1;
    if (countdown) countdown.textContent = Math.max(0, secondsLeft);
    if (secondsLeft <= 2) { nextToken(); refreshList(); }
  }, 1000);
}


/* ------------------------------------------------------- the student page -- */
function startScanPage() {
  const video = el("scanner");
  const form = el("mark-form");
  const result = el("result");
  const rollInput = el("roll_no");
  let lastRead = "";

  function show(html, kind) {
    if (!result) return;
    result.innerHTML = `<p class="note ${kind || ""}">${html}</p>`;
  }

  async function mark(rollNo) {
    if (!rollNo) { show("Enter your roll number first.", "bad"); return; }
    try {
      const data = await postJSON("/api/mark", {
        session_id: form.session_id.value,
        window: form.window.value,
        signature: form.signature.value,
        roll_no: rollNo,
      });
      show(`${escapeHtml(data.message)} The mark is stored and will be sealed into a block when the lecture ends.`, "ok");
      form.style.display = "none";
      if (video && video.srcObject) video.srcObject.getTracks().forEach((t) => t.stop());
    } catch (error) {
      show(escapeHtml(error.message), "bad");
    }
  }

  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      mark(rollInput.value.trim());
    });
  }

  // The camera is optional: if it is refused or unavailable, the form is enough.
  if (!video || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.jsQR) return;

  navigator.mediaDevices
    .getUserMedia({ video: { facingMode: "environment" } })
    .then((stream) => {
      video.srcObject = stream;
      video.play();
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("2d");

      setInterval(() => {
        if (!video.videoWidth || form.style.display === "none") return;
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        const image = context.getImageData(0, 0, canvas.width, canvas.height);
        const found = window.jsQR(image.data, image.width, image.height);
        if (!found || found.data === lastRead) return;

        lastRead = found.data;
        // The QR code is the link to this page, so take its token and mark with
        // whatever roll number is already typed in.
        try {
          const scanned = new URL(found.data);
          form.session_id.value = scanned.searchParams.get("s") || form.session_id.value;
          form.window.value = scanned.searchParams.get("w") || form.window.value;
          form.signature.value = scanned.searchParams.get("sig") || form.signature.value;
        } catch (error) { /* not a URL: fall through and use the form's token */ }

        if (rollInput.value.trim()) mark(rollInput.value.trim());
        else show("Code read. Type your roll number and press Mark me present.");
      }, 700);
    })
    .catch(() => { /* no camera: the form still works */ });
}


/* --------------------------------------------------------- the verify page -- */
function startVerifyPage() {
  const button = el("check-chain");
  const result = el("check-result");
  const form = el("prove-form");
  const out = el("prove-result");

  if (button && result) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      result.innerHTML = "<p class=\"muted\">Checking...</p>";
      try {
        const data = await postJSON("/api/verify", {});
        result.innerHTML = `<p class="note ${data.ok ? "ok" : "bad"}">${escapeHtml(data.message)}</p>`;
      } catch (error) {
        result.innerHTML = `<p class="note bad">${escapeHtml(error.message)}</p>`;
      }
      button.disabled = false;
    });
  }

  if (form && out) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const rollNo = el("prove-roll").value.trim();
      out.innerHTML = "<p class=\"muted\">Looking in the chain...</p>";
      try {
        const data = await getJSON(`/api/prove?roll_no=${encodeURIComponent(rollNo)}`);
        const proof = data.proof;
        out.innerHTML = `
          <table class="kv">
            <tr><th>Student</th><td>${escapeHtml(proof.record.name)} (${escapeHtml(proof.record.roll_no)})</td></tr>
            <tr><th>Subject</th><td>${escapeHtml(proof.record.subject_code)} &mdash; ${escapeHtml(proof.record.subject_name)}</td></tr>
            <tr><th>Status</th><td>${escapeHtml(proof.record.status)}</td></tr>
            <tr><th>Block</th><td>#${proof.block_index}</td></tr>
            <tr><th>Record hash</th><td class="mono break">${proof.record_hash}</td></tr>
            <tr><th>Merkle root of the block</th><td class="mono break">${proof.root}</td></tr>
            <tr><th>Hashes in the proof</th><td>${proof.proof.length}</td></tr>
          </table>
          <p class="note ${proof.proof_valid ? "ok" : "bad"}">
            ${proof.proof_valid
              ? "The proof checks out: this mark is inside block #" + proof.block_index + ", and the block hash is " + proof.block_hash.slice(0, 16) + "..."
              : "The proof does not match the block. The stored data has been changed."}
          </p>`;
      } catch (error) {
        out.innerHTML = `<p class="note bad">${escapeHtml(error.message)}</p>`;
      }
    });
    if (el("prove-roll").value.trim()) form.dispatchEvent(new Event("submit"));
  }
}


/* ------------------------------------------------------------ register ----- */
function startSearch(inputSelector, tableSelector) {
  const input = document.querySelector(inputSelector);
  const table = document.querySelector(tableSelector);
  if (!input || !table) return;
  const rows = Array.from(table.tBodies[0].rows);
  const shown = el("shown");

  input.addEventListener("input", () => {
    const term = input.value.trim().toLowerCase();
    let visible = 0;
    rows.forEach((row) => {
      const match = !term || (row.dataset.search || "").includes(term);
      row.hidden = !match;
      if (match) visible += 1;
    });
    if (shown) shown.textContent = `${visible} of ${rows.length} shown`;
  });
}
