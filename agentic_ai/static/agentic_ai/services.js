(() => {
    'use strict';
    const form = document.getElementById('whatsapp-service-check');
    if (!form) return;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button');
        if (button.disabled) return;
        const status = document.getElementById('service-check-status');
        button.disabled = true; status.textContent = 'Checking gateway and message schema…';
        try {
            const response = await fetch(form.action, {method: 'POST', credentials: 'same-origin',
                headers: {'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value}});
            if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('Session expired or service unavailable. Refresh and sign in again.');
            const body = await response.json();
            status.textContent = body.message || body.error || 'The check could not complete.';
        } catch (error) { status.textContent = error.message || 'The check could not complete.'; }
        finally { button.disabled = false; }
    });
})();
