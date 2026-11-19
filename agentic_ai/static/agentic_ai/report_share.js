(() => {
    const root = document.getElementById('report-share');
    const csrf = document.querySelector('#agent-query-form [name=csrfmiddlewaretoken]');
    let token = null, generation = 0;
    const resetters = [];
    for (const panel of root.querySelectorAll('[data-share-channel]')) {
        const recipient = panel.querySelector('select');
        const status = panel.querySelector('[data-share-status]');
        const preview = panel.querySelector('[data-share-preview-panel]');
        const message = panel.querySelector('[data-share-message]');
        const controls = [...panel.querySelectorAll('button, select')];
        let pending = null, busy = false;

        function clearPreview() {
            pending = null;
            preview.hidden = true;
            message.textContent = '';
        }
        resetters.push(() => {
            clearPreview();
            panel.open = false;
            status.textContent = '';
        });
        recipient.addEventListener('change', clearPreview);
        panel.querySelector('[data-share-cancel]').addEventListener('click', () => {
            clearPreview();
            status.textContent = 'Cancelled.';
        });

        async function request(payload, onSuccess) {
            if (busy || !token) return;
            busy = true;
            const version = generation;
            controls.forEach(control => { control.disabled = true; });
            status.textContent = payload.action === 'send' ? 'Sending…' : 'Preparing preview…';
            try {
                const response = await fetch(panel.dataset.shareEndpoint, {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf.value},
                    body: JSON.stringify(payload),
                });
                const data = await response.json();
                if (version !== generation) return;
                if (!data.ok && !data.approval_required) throw new Error(data.error || data.answer || 'Delivery unavailable.');
                onSuccess(data);
            } catch (error) {
                if (version === generation) status.textContent = error.message || 'Delivery unavailable.';
            } finally {
                busy = false;
                controls.forEach(control => { control.disabled = false; });
            }
        }

        for (const button of panel.querySelectorAll('[data-share-preview]')) {
            button.addEventListener('click', () => {
                clearPreview();
                const payload = {action: 'preview', result_id: token, recipient: recipient.value, mode: button.dataset.sharePreview};
                request(payload, data => {
                    pending = payload;
                    message.textContent = (data.subject ? 'Subject: ' + data.subject + '\n\n' : '') + data.text;
                    preview.hidden = false;
                    status.textContent = 'Review the message for ' + payload.recipient + '.';
                });
            });
        }
        panel.querySelector('[data-share-confirm]').addEventListener('click', () => {
            if (!pending) return;
            const payload = {...pending, action: 'send', confirmed: true};
            clearPreview();
            request(payload, data => { status.textContent = data.message || data.answer; });
        });
    }
    window.BuroReportShare = {setResult(value) {
        token = value;
        generation++;
        root.hidden = !value;
        resetters.forEach(reset => reset());
    }};
})();
