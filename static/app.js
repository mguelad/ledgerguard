'use strict';
const showNotice = (message, error = false) => {
  const notice = document.querySelector('#notice');
  notice.textContent = message;
  notice.classList.toggle('error', error);
  notice.hidden = false;
};
for (const node of document.querySelectorAll('[data-json-source]')) {
  node.textContent = JSON.stringify(JSON.parse(document.getElementById(node.dataset.jsonSource).textContent), null, 2);
}
for (const form of document.querySelectorAll('form[data-api]')) {
  let idempotencyKey = crypto.randomUUID();
  let previousBody = null;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const body = {};
    for (const input of form.elements) {
      if (!input.name || input.name === 'csrfmiddlewaretoken' || input.disabled) continue;
      if (input.dataset.list) { body[input.name] ??= []; if (input.checked) body[input.name].push(input.value); }
      else if (input.type === 'checkbox') body[input.name] = input.checked;
      else if ((input.type === 'number' || input.dataset.type === 'number')) body[input.name] = Number(input.value);
      else if (input.type === 'datetime-local') body[input.name] = `${input.value}:00Z`;
      else body[input.name] = input.value;
    }
    const serialized = JSON.stringify(body);
    if (previousBody !== null && previousBody !== serialized) idempotencyKey = crypto.randomUUID();
    previousBody = serialized;
    const button = form.querySelector('button');
    if (button) button.disabled = true;
    try {
      const headers = {'Content-Type': 'application/json', 'X-CSRFToken': form.querySelector('[name="csrfmiddlewaretoken"]').value, 'Idempotency-Key': idempotencyKey};
      if (form.dataset.version) headers['If-Match'] = `"${form.dataset.version}"`;
      const response = await fetch(form.dataset.api, {method: 'POST', headers, body: JSON.stringify(body), credentials: 'same-origin'});
      const result = await response.json();
      if (!response.ok) { showNotice(result.detail || 'The request was rejected.', true); if (response.status < 500) idempotencyKey = crypto.randomUUID(); return; }
      if (form.dataset.follow && result.location?.startsWith('/') && !result.location.startsWith('//')) { location.assign(result.location); return; }
      if (form.dataset.reload) { location.reload(); return; }
      const output = form.querySelector('output');
      if (output) output.textContent = result.pairing_code ? `Pairing code: ${result.pairing_code}\nExpires: ${result.expires_at}` : result.invitation_token ? `Invitation token: ${result.invitation_token}\nExpires: ${result.expires_at}` : JSON.stringify(result, null, 2);
      showNotice('Saved.'); idempotencyKey = crypto.randomUUID();
    } catch { showNotice('Connection interrupted. Retry the request to recover its result.', true); }
    finally { if (button) button.disabled = false; }
  });
}
