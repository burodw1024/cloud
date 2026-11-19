(() => {
  const form = document.getElementById('history-export-actions');
  const status = document.getElementById('history-export-status');
  form.querySelectorAll('[data-open-export]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    status.textContent = 'Checking dashboard access…';
    try {
      const response = await fetch(form.dataset.endpoint, {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': form.elements.csrfmiddlewaretoken.value},
        body: JSON.stringify({action: 'open', export_id: button.dataset.openExport}),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'Dashboard unavailable.');
      const url = new URL(result.url);
      if (!['https:', 'http:'].includes(url.protocol)) throw new Error('Invalid dashboard address.');
      window.location.assign(url.href);
    } catch (error) {
      status.textContent = error.message || 'Dashboard unavailable.';
      button.disabled = false;
    }
  }));
})();
