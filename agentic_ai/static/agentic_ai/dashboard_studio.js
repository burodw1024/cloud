(() => {
  'use strict';
  const form = document.getElementById('dashboard-design');
  const status = document.getElementById('studio-status');
  const publish = document.getElementById('studio-publish');
  const preview = document.getElementById('studio-preview');
  const options = document.getElementById('studio-options');
  const dimension = document.getElementById('studio-dimension');
  const secondary = document.getElementById('studio-secondary');
  const currentTemplate = () => form.elements['studio-template'].value;
  const metrics = document.getElementById('studio-metrics');
  let exportId = null, lastTemplate = null;
  let busy = false;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  async function api(payload) {
    const response = await fetch(form.dataset.endpoint, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value},
      body: JSON.stringify(payload)
    });
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || 'Dashboard operation failed.');
    return body;
  }
  async function perform(message, action) {
    if (busy) return;
    busy = true; status.textContent = message; status.dataset.error = 'false';
    document.querySelector('.studio-preview').setAttribute('aria-busy', 'true');
    const controls = [...form.querySelectorAll('input:not([type="hidden"]), select'),
      ...document.querySelectorAll('button')].filter(control => !control.disabled);
    controls.forEach(control => { control.disabled = true; });
    try { await action(); }
    catch (error) { status.textContent = error.message || 'Dashboard service unavailable.'; status.dataset.error = 'true'; }
    finally {
      busy = false;
      document.querySelector('.studio-preview').setAttribute('aria-busy', 'false');
      controls.forEach(control => { control.disabled = false; });
      publish.disabled = !exportId || publish.dataset.unavailable === 'true';
    }
  }
  function invalidate() { exportId = null; publish.disabled = true; }
  form.addEventListener('input', invalidate);
  form.addEventListener('change', event => {
    invalidate();
    if (event.target.name === 'studio-template') {
      options.hidden = true;
      preview.hidden = true;
      status.textContent = 'Generate a preview for the selected template.';
    }
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    perform('Designing your dashboard…', async () => {
      invalidate();
      const payload = {action: 'preview', title: document.getElementById('dashboard-title').value, template:currentTemplate()};
      if (form.dataset.reportToken) payload.report_token = form.dataset.reportToken;
      if (!options.hidden && currentTemplate() === lastTemplate) {
        payload.dimension = dimension.value;
        if (currentTemplate() === 'heatmap') payload.secondary_dimension = secondary.value;
        payload.metrics = [...metrics.querySelectorAll('input:checked')].map(input => input.value);
      }
      const result = await api(payload), spec = result.specification;
      exportId = result.export_id;lastTemplate = spec.template;
      document.getElementById('preview-title').textContent = spec.title;
      document.getElementById('studio-notice').textContent = spec.notice;
      document.getElementById('studio-meta').replaceChildren(
        node('span', spec.rows + ' displayed rows', 'studio-pill'),
        node('span', spec.report_date || 'Report date unavailable', 'studio-pill'),
        node('span', spec.truncated ? 'Limited source result' : 'Displayed result snapshot', 'studio-pill'));
      dimension.replaceChildren(node('option', 'Row number'));
      dimension.firstChild.value = '';
      spec.dimensions.forEach(field => { const option = node('option', spec.labels[field] || field); option.value = field; dimension.append(option); });
      dimension.value = spec.dimension || '';
      secondary.replaceChildren(...spec.dimensions.map(field => {
        const option = node('option', spec.labels[field] || field);
        option.value = field;
        return option;
      }));
      secondary.value = spec.secondary_dimension || '';
      document.getElementById('studio-secondary-field').hidden = spec.template !== 'heatmap';
      document.getElementById('studio-measure-field').hidden = spec.metric_limit === 0;
      document.getElementById('studio-measure-legend').textContent = spec.template === 'satellite'
        ? 'Marker size (optional)' : 'Measures · choose up to ' + spec.metric_limit;
      metrics.replaceChildren();
      spec.measures.forEach(field => {
        const label = node('label'), input = node('input');
        input.type = spec.metric_limit === 1 && spec.template !== 'satellite' ? 'radio' : 'checkbox';
        input.name = 'studio-measure';
        input.value = field;
        input.checked = spec.metrics.includes(field);
        if (spec.template === 'satellite') input.addEventListener('change', () => {
          if (input.checked) metrics.querySelectorAll('input:checked').forEach(other => {
            if (other !== input) other.checked = false;
          });
        });
        label.append(input, document.createTextNode(' ' + (spec.labels[field] || field)));
        metrics.append(label);
      });
      const charts = document.getElementById('studio-chart-cards');
      const selected = form.querySelector('[name="studio-template"]:checked').closest('label');
      const card = node('div', undefined, 'studio-chart design-summary');
      card.append(selected.querySelector('.design-swatch').cloneNode(true));
      const details = node('div');
      details.append(node('strong', selected.querySelector('strong').textContent),
        node('p', spec.charts.length + ' dashboard panels · source table included'));
      card.append(details);
      charts.replaceChildren(card);
      const table = node('table'), header = node('tr');
      const fields = spec.template === 'table' ? Object.keys(spec.labels)
        : [...new Set([spec.dimension, spec.secondary_dimension, ...Object.values(spec.map_columns),
          ...spec.metrics].filter(Boolean))];
      if (!fields.length) fields.push(...Object.keys(spec.labels));
      fields.forEach(field => header.append(node('th', spec.labels[field] || field))); table.append(header);
      result.sample.forEach(row => { const tr = node('tr'); fields.forEach(field => tr.append(node('td', String(row[field] ?? '—')))); table.append(tr); });
      document.getElementById('studio-sample').replaceChildren(table);
      options.hidden = false; preview.hidden = false;
      document.getElementById('studio-open').hidden = true;
      status.textContent = 'Preview ready. Adjust labels and measures, or create your private dashboard.';
    });
  });
  function showLink(url) {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol)) throw new Error('Invalid dashboard URL.');
    const link = document.getElementById('studio-open'); link.href = parsed.href; link.hidden = false;
  }
  publish.addEventListener('click', () => {
    if (!exportId) return;
    const identifier = exportId; invalidate();
    perform('Creating dataset, charts and dashboard in Superset…', async () => {
      const result = await api({action: 'publish', export_id: identifier});
      showLink(result.url); status.textContent = result.message + ' Reload to update your export library.';
    });
  });
  document.querySelectorAll('[data-export]').forEach(button => button.addEventListener('click', () => {
    const action = button.dataset.action;
    if (action === 'delete' && !window.confirm('Remove this dashboard, its charts, dataset and stored snapshot?')) return;
    perform(action === 'delete' ? 'Removing export…' : 'Checking current access…', async () => {
      const result = await api({action, export_id: button.dataset.export});
      if (result.url) showLink(result.url);
      else { button.closest('tr').remove(); status.textContent = result.message; }
    });
  }));
})();

