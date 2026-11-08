(() => {
    const form = document.getElementById('permission-form');
    if (!form) return;
    document.querySelectorAll('[data-filter]').forEach(search => {
        search.addEventListener('input', () => {
            const term = search.value.trim().toLocaleLowerCase();
            document.getElementById(search.dataset.filter).querySelectorAll('.choice-card').forEach(row => {
                row.hidden = !row.textContent.toLocaleLowerCase().includes(term) && !row.querySelector('input').checked;
            });
        });
    });
    document.querySelectorAll('[data-reason]').forEach(button => {
        button.addEventListener('click', () => {
            form.elements.reason.value = button.dataset.reason;
            form.elements.reason.dispatchEvent(new Event('input', {bubbles: true}));
        });
    });
    function update() {
        const mode = form.querySelector('[name=mode]:checked')?.value;
        document.getElementById('scope-picker').hidden = mode !== 'SELECTED';
        document.getElementById('all-scope-note').hidden = mode !== 'ALL';
        const count = name => form.querySelectorAll(`[name=${name}]:checked`).length;
        document.getElementById('selection-count').textContent = `${count('tools')} tools selected · ` +
            (mode === 'ALL' ? 'All active branches' : mode === 'NONE' ? 'No financial data' : `${count('branches')} branches and ${count('zones')} zones selected`);
    }
    form.addEventListener('change', update);
    // Keep an older server preview from appearing to describe unsaved edits.
    form.addEventListener('input', () => {
        const preview = document.getElementById('access-preview');
        if (preview) preview.hidden = true;
    });
    update();
})();
