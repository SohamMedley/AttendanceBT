const $ = id => document.getElementById(id);
let currentToken = '', ledgerData = {chain: [], pending: []}, records = [], view = 'overview';
let scannedToken = new URLSearchParams(location.search).get('token');
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const time = timestamp => new Date(timestamp * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
let toastTimer;
function toast(message) { $('toast').textContent = message; $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4500); }
async function api(path, options) {const response = await fetch(path, options); const data = await response.json(); if(!response.ok) throw new Error(data.message || 'Something went wrong. Please try again.'); return data;}
async function fetchEpoch() {
 try { const data = await api('/api/qr-epoch'); if(data.token !== currentToken) { currentToken = data.token; $('qr-image').src = '/api/qr.svg?epoch=' + encodeURIComponent(currentToken); $('token-label').textContent = currentToken; }
 $('epoch-counter').textContent = data.ttl_seconds + 's'; $('progress-bar').style.width = data.ttl_seconds / 15 * 100 + '%'; $('connection').innerHTML = '<i></i> System operational';
 } catch { $('connection').textContent = 'Connection interrupted'; $('epoch-counter').textContent = 'Offline'; } finally {setTimeout(fetchEpoch,1000);}
}
const badge = pending => `<span class="pill ${pending ? 'amber-pill' : 'green'}">${pending ? '◷ Pending' : '✓ Sealed'}</span>`;
function student(record) {const initials=record.name.split(/\s+/).slice(0,2).map(s=>s[0]).join('');return `<div class="student-cell"><span class="student-avatar">${escapeHtml(initials)}</span><div>${escapeHtml(record.name)}<small>${escapeHtml(record.student_id)}</small></div></div>`;}
function renderAttendance() {
 $('recent-body').innerHTML = records.length ? records.slice(0,5).map(r => `<tr><td>${student(r)}</td><td>${time(r.timestamp)}</td><td>${badge(r.pending)}</td></tr>`).join('') : '<tr><td colspan="3" class="empty"><span class="empty-icon">♙</span><strong>Your first check-in starts here</strong><p>Share the session QR code with your students.<br>Their attendance will appear here in real time.</p></td></tr>';
 const query = $('search').value.toLowerCase(); const filtered = records.filter(r => `${r.name} ${r.student_id}`.toLowerCase().includes(query));
 $('attendance-body').innerHTML = filtered.length ? filtered.map(r => `<tr><td>${student(r)}</td><td>${escapeHtml(r.student_id)}</td><td>${new Date(r.timestamp*1000).toLocaleDateString()} · ${time(r.timestamp)}</td><td>${badge(r.pending)}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">No matching attendance records.</td></tr>';
}
async function fetchLedger() {
 const data = await api('/api/ledger'); ledgerData = data;
 records = [...data.pending.map(r=>({...r,pending:true})), ...data.chain.slice(1).flatMap(b=>b.transactions.map(r=>({...r,pending:false})))].sort((a,b)=>b.timestamp-a.timestamp);
 $('total-count').textContent = records.length; $('nav-count').textContent=records.length; $('recent-count').textContent=records.length;
 $('pending-count').textContent=data.pending.length; $('blocks-count').textContent=data.chain.length-1; $('mine-button').disabled=!data.pending.length;
 const linked=data.integrity_valid === true;
 $('integrity').textContent=linked?'Verified':'Warning'; $('integrity-note').textContent=linked?'All hashes and links verified':'Ledger integrity check failed';
 $('block-height').textContent='Chain height: '+(data.chain.length-1);
 $('ledger-body').innerHTML=data.chain.slice().reverse().map(b=>`<tr><td class="block-tag">⬡ #${String(b.index).padStart(3,'0')} ${b.index===0?'<span class="count-badge">Genesis</span>':''}</td><td><code title="${escapeHtml(b.hash)}">${escapeHtml(b.hash.slice(0,16))}…</code></td><td><code title="${escapeHtml(b.previous_hash)}">${escapeHtml(b.previous_hash.slice(0,16))}…</code></td><td>${b.index===0?'—':b.transactions.length+' records'}</td><td>${time(b.timestamp)}</td><td><span class="pill green">✓ Sealed</span></td></tr>`).join(''); renderAttendance();
}
async function pollLedger(){try{await fetchLedger();}catch{ $('connection').textContent='Connection interrupted'; }finally{setTimeout(pollLedger,4000);}}
function setView(next){view=next; const titles={overview:['Overview','Less roll call. More peace of mind.'],attendance:['Attendance','Every check-in, in one place.'],ledger:['Blockchain ledger','A transparent trail. From the first block onward.']}; $('page-title').textContent=titles[next][0]; $('breadcrumb').textContent=titles[next][0]; $('page-description').textContent=titles[next][1];$('hero').hidden=next!=='overview';$('overview-panels').hidden=next!=='overview';$('attendance-panel').hidden=next!=='attendance';$('ledger-panel').hidden=next==='attendance';document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('active',n.dataset.view===next));}
document.querySelectorAll('.nav').forEach(n=>n.addEventListener('click',()=>setView(n.dataset.view)));$('view-all').onclick=()=>setView('attendance');$('search').oninput=renderAttendance;
$('mine-button').onclick=async()=>{const button=$('mine-button');button.disabled=true;try{const data=await api('/api/mine',{method:'POST'});toast(`Block #${data.block_index} sealed successfully.`);await fetchLedger();}catch(error){toast(error.message);}finally{button.disabled=!ledgerData.pending.length;}};
function openCheckIn(){ $('form-error').textContent='';$('scan-token').value=scannedToken||currentToken;$('check-in-dialog').showModal(); }
$('check-in-button').onclick=openCheckIn;$('attendance-check-in').onclick=openCheckIn;$('close-dialog').onclick=()=>$('check-in-dialog').close();
$('check-in-form').onsubmit=async event=>{event.preventDefault();const button=$('submit-check-in');button.disabled=true;$('form-error').textContent='';try{const data=await api('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...Object.fromEntries(new FormData(event.target)), token: scannedToken ? $('scan-token').value : (await api('/api/qr-epoch')).token})});$('check-in-dialog').close();event.target.reset();scannedToken=null;history.replaceState(null,'',location.pathname);toast(data.message);await fetchLedger();}catch(error){$('form-error').textContent=error.message;}finally{button.disabled=false;}};
$('export-button').onclick=()=>{if(!records.length){toast('No attendance records to export yet.');return;}const cell=value=>'"'+String(value).replace(/^[\s]*[=+@-]|^[\t\r\n]/,"'$&").replace(/"/g,'""')+'"';const csv=[['Student ID','Name','Checked in (UTC)','Status'],...records.map(r=>[r.student_id,r.name,new Date(r.timestamp*1000).toISOString(),r.pending?'Pending':'Sealed'])].map(row=>row.map(cell).join(',')).join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download='provex-attendance.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Attendance export downloaded.');};
fetchEpoch();pollLedger();if(scannedToken)openCheckIn();
