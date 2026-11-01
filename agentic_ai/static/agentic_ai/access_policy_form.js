// Irrelevant selections are cleared when the administrator changes access mode.
// The server independently validates the same constraints and all permissions.
(function () {
    const mode = document.getElementById('id_mode');
    if (!mode) return;
    const form = mode.form;
    const filters = [];
    ['zones', 'branches'].forEach(name => {
        const select = form.elements[name];
        if (!select) return;
        const search = document.createElement('input');
        search.type = 'search';
        search.placeholder = 'Find ' + name + ' by code or name…';
        search.setAttribute('aria-label', 'Filter ' + name + ' by code or name');
        search.className = 'access-option-search';
        const status = document.createElement('small');
        status.setAttribute('role', 'status');
        select.before(search);
        select.after(status);
        function filter() {
            const term = search.value.trim().toLocaleLowerCase();
            let matching = 0, selected = 0;
            [...select.options].forEach(option => {
                const matches = option.textContent.toLocaleLowerCase().includes(term);
                option.hidden = !matches && !option.selected;
                if (matches) matching++;
                if (option.selected) selected++;
            });
            status.textContent = matching + ' matching · ' + selected + ' selected. Selected entries stay visible.';
        }
        search.addEventListener('input', filter);
        select.addEventListener('change', filter);
        filters.push(filter);
    });
    function update() {
        ['zones', 'branches'].forEach(name => {
            const input = form.elements[name];
            const group = form.querySelector('[data-access-field="' + name + '"]');
            const selected = mode.value === 'SELECTED';
            if (group) group.hidden = !selected;
            if (!selected && input) [...input.options].forEach(option => { option.selected = false; });
        });
        const expiry = form.elements.expires_at;
        const group = form.querySelector('[data-access-field="expires_at"]');
        if (group) group.hidden = mode.value === 'NONE';
        if (mode.value === 'NONE' && expiry) expiry.value = '';
        filters.forEach(filter => filter());
    }
    mode.addEventListener('change', update);
    update();
})();
