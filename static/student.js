const $ = id => document.getElementById(id);
let token = '', expiresAt = 0, clockOffset = 0, refreshing = false;
async function get(path) {
  const response = await fetch(path);
  if (response.status === 401) {location.href='/student'; throw new Error('Please sign in again.');}
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || 'Connection interrupted.');
  return data;
}
async function refreshQR() {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await get('/api/student/qr');
    clockOffset = data.server_time * 1000 - Date.now(); expiresAt = data.expires_at * 1000;
    if (token !== data.token) {
      token = data.token; $('student-qr').hidden = true; $('qr-loading').hidden = false;
      $('student-qr').src = '/api/student/qr.svg?token=' + encodeURIComponent(token);
    }
    $('student-error').textContent = '';
  } catch (error) { $('student-error').textContent = 'Unable to refresh your QR. Check your connection and keep this page open.'; }
  finally {refreshing=false;}
}
$('student-qr').onload = () => {if (Date.now()+clockOffset < expiresAt) { $('student-qr').hidden=false; $('qr-loading').hidden=true; }};
$('student-qr').onerror = () => {token=''; expiresAt=0; $('qr-loading').textContent='Refreshing your QR…';};
function tick(){const remaining=Math.max(0,Math.ceil((expiresAt-Date.now()-clockOffset)/1000));$('qr-countdown').textContent=remaining+'s';$('student-progress').style.width=remaining/60*100+'%';if(!remaining){$('student-qr').hidden=true;$('qr-loading').hidden=false;refreshQR();}}
async function status(){try{const data=await get('/api/student/status');$('class-type').textContent=data.session.type+' · '+data.session.date;$('attendance-status').textContent=data.present?'✓ You are present for this '+data.session.type.toLowerCase()+'.':'Show this code to your teacher to be marked present.';$('attendance-status').classList.toggle('is-present',data.present);}catch{}finally{setTimeout(status,3000);}}
$('logout').onclick=async()=>{try{const response=await fetch('/api/logout',{method:'POST',headers:{'X-Requested-With':'Provex'}});if(!response.ok)throw new Error();location.href='/student';}catch{$('student-error').textContent='Could not sign out. Please retry.';}};
document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick();});
refreshQR();status();setInterval(tick,1000);
