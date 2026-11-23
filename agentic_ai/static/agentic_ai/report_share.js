(() => {
    const root = document.getElementById('report-share');
    const csrf = document.querySelector('#agent-query-form [name=csrfmiddlewaretoken]');
    const channel = document.getElementById('report-share-channel');
    const panels = [...root.querySelectorAll('[data-share-channel]')];
    let result = {}, generation = 0;
    const tokenFor = key => result[key] || (key !== 'whatsapp' && key !== 'telegram' ? result.other : null);

    for (const panel of panels) {
        const key = panel.dataset.shareChannel;
        const recipient = panel.querySelector('select');
        const status = panel.querySelector('[data-share-status]');
        const preview = panel.querySelector('[data-share-preview-panel]');
        const message = panel.querySelector('[data-share-message]');
        const destination = panel.querySelector('[data-share-destination]');
        const controls = [...panel.querySelectorAll('button, select')];
        let pending = null, busy = false, previewButton = null;

        function clearPreview() {
            pending = null;
            preview.hidden = true;
            message.textContent = '';
            destination.textContent = '';
        }
        function showSelected() {
            panel.hidden = channel.value !== key || !tokenFor(key);
            clearPreview();
            if (!busy) status.textContent = '';
        }
        channel.addEventListener('change', showSelected);
        recipient.addEventListener('change', () => {
            clearPreview();
            status.textContent = '';
        });
        panel.querySelector('[data-share-cancel]').addEventListener('click', () => {
            clearPreview();
            status.textContent = 'Preview cancelled.';
            previewButton?.focus();
        });

        async function request(payload, onSuccess) {
            if (busy || !tokenFor(key)) return;
            busy = true;
            const version = generation;
            channel.disabled = true;
            controls.forEach(control => { control.disabled = true; });
            status.textContent = payload.action === 'send' ? 'Sending…' : 'Preparing preview…';
            try {
                const response = await fetch(panel.dataset.shareEndpoint, {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf.value},
                    body: JSON.stringify(payload),
                });
                if (!response.headers.get('content-type')?.includes('application/json')) {
                    throw new Error(payload.action === 'send' ? 'Delivery status unavailable. Check the gateway before retrying.' : 'Preview unavailable. Try again.');
                }
                const data = await response.json();
                if (version !== generation) return;
                if (!data.ok && !data.approval_required) throw new Error(data.error || data.answer || 'Delivery unavailable.');
                onSuccess(data);
            } catch (error) {
                if (version === generation) status.textContent = error.message || 'Delivery unavailable.';
            } finally {
                busy = false;
                channel.disabled = false;
                controls.forEach(control => { control.disabled = false; });
                if (version !== generation) status.textContent = '';
            }
        }

        for (const button of panel.querySelectorAll('[data-share-preview]')) {
            button.addEventListener('click', () => {
                previewButton = button;
                clearPreview();
                const payload = {action: 'preview', result_id: tokenFor(key), recipient: recipient.value, mode: button.dataset.sharePreview};
                request(payload, data => {
                    pending = payload;
                    destination.textContent = 'Review message for ' + recipient.selectedOptions[0].textContent.trim();
                    message.textContent = (data.subject ? 'Subject: ' + data.subject + '\n\n' : '') + data.text;
                    if (data.report_url) message.textContent += '\n\nOpen full report: ' + data.report_url;
                    if (data.includes_chart) message.textContent += '\nA chart image will be attached.';
                    preview.hidden = false;
                    destination.focus();
                    status.textContent = '';
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
        result = value || {};
        generation++;
        for (const option of channel.options) option.disabled = !tokenFor(option.value);
        const selected = [...channel.options].find(option => !option.disabled && option.value === channel.value)
            || [...channel.options].find(option => !option.disabled);
        root.hidden = !selected;
        channel.value = selected?.value || '';
        channel.dispatchEvent(new Event('change'));
    }};
})();
