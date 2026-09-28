const $ = id => document.getElementById(id);
let ledgerData = {chain: [], pending: [], session: {}}, records = [];
let seenRecords = new Set(), ledgerLoaded = false;
const motion = window.provexMotion;
const recordKey = r => r.session_id + ':' + r.student_id;
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const time = timestamp => new Date(timestamp * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',timeZone:'Asia/Kolkata'});
let toastTimer;
function toast(message) { $('toast').textContent=message; $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').classList.remove('show'),4500); }
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{'X-Requested-With':'Provex',...options.headers}});
  if(response.status===401){location.href='/admin/dashboard';throw new Error('Please sign in again.');}
  const data=await response.json();if(!response.ok)throw new Error(data.message||'Something went wrong. Please retry.');return data;
}
const post=(path,data)=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})});
const badge = pending => `<span class="pill ${pending ? 'amber-pill' : 'green'}">${pending ? '✓ Present · pending' : '✓ Present · sealed'}</span>`;
function student(record) {const initials=record.name.split(/\s+/).slice(0,2).map(s=>s[0]).join('');return `<div class="student-cell"><span class="student-avatar">${escapeHtml(initials)}</span><div>${escapeHtml(record.name)}<small>${escapeHtml(record.student_id)}</small></div></div>`;}
function renderAttendance() {
 const current = records.filter(r => r.session_id === ledgerData.session.id);
 $('recent-body').innerHTML = current.length ? current.slice(0,5).map(r => `<tr data-record="${escapeHtml(recordKey(r))}"><td>${student(r)}</td><td>${time(r.timestamp)}</td><td>${badge(r.pending)}</td></tr>`).join('') : '<tr><td colspan="3" class="empty"><span class="empty-icon">♙</span><strong>Your first check-in starts here</strong><p>Open the scanner and scan a personal student QR.<br>Present students will appear here in real time.</p></td></tr>';
 const query = $('search').value.toLowerCase(); const filtered = records.filter(r => `${r.name} ${r.student_id}`.toLowerCase().includes(query));
 $('attendance-body').innerHTML = filtered.length ? filtered.map(r => `<tr data-record="${escapeHtml(recordKey(r))}"><td>${student(r)}</td><td>${escapeHtml(r.student_id)}</td><td>${escapeHtml(r.session_type)}</td><td>${new Date(r.timestamp*1000).toLocaleDateString()} · ${time(r.timestamp)}</td><td>${badge(r.pending)}</td></tr>`).join('') : '<tr><td colspan="5" class="empty">No matching attendance records.</td></tr>';
}
async function fetchLedger() {
 const data = await api('/api/ledger'); ledgerData = data;
 records = [...data.pending.map(r=>({...r,pending:true})), ...data.chain.slice(1).flatMap(b=>b.transactions.map(r=>({...r,pending:false})))].sort((a,b)=>b.timestamp-a.timestamp);
 const present = records.filter(r => r.session_id === data.session.id).length;
 motion.count('total-count',present); motion.count('session-present',present); $('nav-count').textContent=records.length; $('recent-count').textContent=present; $('roster-count').textContent=data.student_count;
 $('active-class-type').textContent=data.session.type; $('active-class-date').textContent=data.session.date; $('hero-class').textContent=data.session.type.toUpperCase(); $('scanner-class').textContent=data.session.type.toLowerCase()+' · Blockchain & Technology';
 $('connection').innerHTML='<i></i> System operational';
 motion.count('pending-count',data.pending.length); motion.count('blocks-count',data.chain.length-1); $('mine-button').disabled=!data.pending.length;
 const linked=data.integrity_valid === true;
 $('integrity').textContent=linked?'Verified':'Warning'; $('integrity-note').textContent=linked?'All hashes and links verified':'Ledger integrity check failed';
 $('block-height').textContent='Chain height: '+(data.chain.length-1);
 $('ledger-body').innerHTML=data.chain.slice().reverse().map(b=>`<tr><td class="block-tag">⬡ #${String(b.index).padStart(3,'0')} ${b.index===0?'<span class="count-badge">Genesis</span>':''}</td><td><code title="${escapeHtml(b.hash)}">${escapeHtml(b.hash.slice(0,16))}…</code></td><td><code title="${escapeHtml(b.previous_hash)}">${escapeHtml(b.previous_hash.slice(0,16))}…</code></td><td>${b.index===0?'—':b.transactions.length+' records'}</td><td>${time(b.timestamp)}</td><td><span class="pill green">✓ Sealed</span></td></tr>`).join(''); renderAttendance();
 if(ledgerLoaded) document.querySelectorAll('[data-record]').forEach(row=>{if(!seenRecords.has(row.dataset.record))motion.present(row);});
 seenRecords=new Set(records.map(recordKey));ledgerLoaded=true;
}
async function pollLedger(){try{await fetchLedger();}catch{ $('connection').textContent='Connection interrupted'; }finally{setTimeout(pollLedger,4000);}}

function setView(next){
 const titles={overview:['Overview','University of Mumbai · SEM VII · Blockchain & Technology'],attendance:['Attendance','Every lecture and practical, in one place.'],ledger:['Blockchain ledger','A transparent trail, from the first block onward.'],students:['Student profiles','Their name. Their roll number. Their personal QR.']};
 $('page-title').textContent=titles[next][0];$('breadcrumb').textContent=titles[next][0];$('page-description').textContent=titles[next][1];
 $('hero').hidden=next!=='overview';$('overview-panels').hidden=next!=='overview';$('attendance-panel').hidden=next!=='attendance';$('students-panel').hidden=next!=='students';$('ledger-panel').hidden=!['overview','ledger'].includes(next);
 document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('active',n.dataset.view===next));
 document.querySelectorAll('.page-heading, main > section:not([hidden]), #overview-panels:not([hidden])').forEach(element=>motion.enter(element));
 if(next==='students')loadRoster().catch(error=>toast(error.message));
}
document.querySelectorAll('.nav').forEach(n=>n.addEventListener('click',()=>setView(n.dataset.view)));
$('view-all').onclick=()=>setView('attendance');$('search').oninput=renderAttendance;
$('mine-button').onclick=async()=>{const button=$('mine-button');button.disabled=true;try{const data=await post('/api/mine');toast(`Block #${data.block_index} sealed successfully.`);await fetchLedger();}catch(error){toast(error.message);}finally{button.disabled=!ledgerData.pending.length;}};
$('start-session').onclick=async()=>{
 const kind=$('session-type').value;
 if(!confirm('Start a new '+kind.toLowerCase()+'? This creates a fresh attendance session. Existing records are kept.'))return;
 $('start-session').disabled=true;
 try{await post('/api/class',{type:kind});await fetchLedger();toast(kind+' started. Scan each student once for this session.');}catch(error){toast(error.message);}finally{$('start-session').disabled=false;}
};
async function loadRoster(){const data=await api('/api/students');$('students-body').innerHTML=data.students.map(s=>`<tr><td class="block-tag">#${escapeHtml(s.student_id)}</td><td>${escapeHtml(s.name)}</td><td>VII</td><td><span class="pill green">Profile ready</span></td></tr>`).join('');}
$('student-form').onsubmit=async event=>{event.preventDefault();$('create-student').disabled=true;$('roster-error').textContent='';try{await post('/api/students',Object.fromEntries(new FormData(event.target)));event.target.reset();await loadRoster();toast('Student profile created. They can now sign in.');await fetchLedger();}catch(error){$('roster-error').textContent=error.message;}finally{$('create-student').disabled=false;}};
$('copy-student-link').onclick=async()=>{try{await navigator.clipboard.writeText(location.origin+'/student');toast('Student portal link copied.');}catch{toast('Student portal: '+location.origin+'/student');}};
$('logout').onclick=async()=>{try{const data=await post('/api/logout');location.href=data.redirect;}catch(error){toast(error.message);}};

let scanner=null, starting=null, stopping=null, scanBusy=false, scanComplete=false, scanGeneration=0;
let lastToken='', lastScan=0;
function openCheckIn(){
 scanGeneration++; scanComplete=false;lastToken='';
 $('scan-result').textContent='';$('scan-confirmation').hidden=true;$('scan-guide').hidden=true;
 $('check-in-dialog').showModal();
}
$('check-in-button').onclick=openCheckIn;$('attendance-check-in').onclick=openCheckIn;$('class-scan').onclick=openCheckIn;
$('close-dialog').onclick=()=>$('check-in-dialog').close();
async function stopCamera(){
 if(stopping)return stopping;
 stopping=(async()=>{
   if(starting){try{await starting;}catch{}}
   if(scanner?.isScanning){try{await scanner.stop();}catch{}}
   $('scan-guide').hidden=true;$('start-camera').disabled=false;$('start-camera').textContent='Enable camera';
 })();
 try{await stopping;}finally{stopping=null;}
}
$('check-in-dialog').addEventListener('close',()=>{scanGeneration++;scanComplete=false;stopCamera();});
document.addEventListener('visibilitychange',()=>{if(document.hidden && $('check-in-dialog').open)$('check-in-dialog').close();});
function getScanner(){
 if(!window.Html5Qrcode)throw new Error('Scanner unavailable. Reload this page and try again.');
 if(!scanner)scanner=new Html5Qrcode('camera-reader');return scanner;
}
async function submitToken(token){
 if(scanBusy || scanComplete || !$('check-in-dialog').open || (token===lastToken && Date.now()-lastScan<4000))return;
 const generation=scanGeneration;
 scanBusy=true;lastToken=token;lastScan=Date.now();
 $('scanner-stage').classList.add('is-verifying');$('scan-result').className='';$('scan-result').textContent='Verifying student QR…';
 try{
   const data=await post('/api/scan',{token});
   if(generation===scanGeneration && $('check-in-dialog').open){
     scanComplete=true;$('scan-guide').hidden=true;
     $('scan-student-name').textContent=data.student.name;
     $('scan-student-details').textContent='Roll No. '+data.student.student_id+' · '+data.session.type+' · SEM VII';
     $('scan-confirmation').hidden=false;
     $('scan-result').textContent=data.message;$('scan-result').className='scan-success';
     $('scan-next').focus({preventScroll:true});
   }
   toast(data.message);
   try{await fetchLedger();}catch{toast('Attendance saved. Dashboard will update when the connection returns.');}
 }catch(error){
   if(generation===scanGeneration){$('scan-result').textContent=error.message;$('scan-result').className='scan-error';motion.enter($('scan-result'));}
 }finally{scanBusy=false;$('scanner-stage').classList.remove('is-verifying');}
}
$('scan-next').onclick=()=>{
 scanComplete=false;lastToken='';$('scan-confirmation').hidden=true;$('scan-guide').hidden=false;
 $('scan-result').className='';$('scan-result').textContent='Ready for the next student. Hold their QR inside the frame.';
 $('close-dialog').focus({preventScroll:true});
};
$('start-camera').onclick=async()=>{
 if(starting)return;
 if(stopping)await stopping;
 const generation=scanGeneration;
 if(!$('check-in-dialog').open)return;
 $('start-camera').disabled=true;$('start-camera').textContent='Opening camera…';$('scan-result').textContent='';
 try{
   const reader=getScanner();
   starting=reader.start({facingMode:'environment'},{fps:10,qrbox:(w,h)=>{const size=Math.floor(Math.min(w,h)*.75);return {width:size,height:size};}},decoded=>{if(generation===scanGeneration)submitToken(decoded);},()=>{});
   await starting;
   if(generation===scanGeneration && $('check-in-dialog').open){
     $('scan-guide').hidden=false;$('start-camera').textContent='Camera live · Ready to scan';
     $('scan-result').textContent='Hold the student’s live QR inside the frame.';
   }
 }catch{
   if(generation===scanGeneration){$('scan-result').textContent='Camera unavailable. Open this HTTPS page directly in your browser, allow camera access, then retry.';$('scan-result').className='scan-error';$('start-camera').disabled=false;$('start-camera').textContent='Retry camera';}
 }finally{starting=null;}
};
$('export-button').onclick=()=>{
 if(!records.length){toast('No attendance records to export yet.');return;}
 const cell=value=>'"'+String(value).replace(/^[\s]*[=+@-]|^[\t\r\n]/,"'$&").replace(/"/g,'""')+'"';
 const csv=[['Roll number','Name','Subject','Semester','Class','Session ID','Checked in (UTC)','Attendance','Ledger status'],...records.map(r=>[r.student_id,r.name,r.subject,r.semester,r.session_type,r.session_id,new Date(r.timestamp*1000).toISOString(),'Present',r.pending?'Pending':'Sealed'])].map(row=>row.map(cell).join(',')).join('\r\n');
 const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download='bcoe-sem-vii-attendance.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Attendance export downloaded.');
};
pollLedger();
