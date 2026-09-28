document.getElementById('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, button = document.getElementById('login-submit'), error = document.getElementById('form-error');
  button.disabled = true; error.textContent = '';
  try {
    const response = await fetch('/api/login/' + form.dataset.role, {method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'Provex'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || 'Unable to sign in.');
    location.href = data.redirect;
  } catch (err) {error.textContent = err.message;} finally {button.disabled = false;}
});
