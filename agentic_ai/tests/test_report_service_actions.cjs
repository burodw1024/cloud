// Run with: node agentic_ai/tests/test_report_service_actions.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor() { this.dataset = {}; this.children = []; this.hidden = false; this.value = ''; }
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  addEventListener(event, listener) { this[event] = listener; }
  querySelectorAll() { return []; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
}
const ids = new Map();
const get = id => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
get('report-templates').dataset.default = 'table';
get('generate-superset-report').dataset.studioUrl = '/agentic/analytics/';
get('quick-superset-publish').dataset.endpoint = '/agentic/api/dashboards/';
const requests = [];
const context = { document: {getElementById: get, createElement: () => new Element(),
  querySelector: () => ({value: 'csrf-token'})}, window: {print() {}}, URL,
  async fetch(url, options) {
    requests.push([url, JSON.parse(options.body), options.headers['X-CSRFToken']]);
    return {ok: true, async json() { return {ok: true, url: 'https://superset.internal/superset/dashboard/9/'}; }};
  } };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/agentic_ai/report_templates.js'), 'utf8'), context);
const templates = context.window.BuroReportTemplates;
const report = {columns: ['BRANCHCODE'], rows: [{BRANCHCODE: '0253'}]};
templates.show(report, {summary_token: 'selected-report'});
assert.equal(get('generate-superset-report').href, '/agentic/analytics/?report=selected-report');
assert.equal(get('generate-superset-report').hidden, false);
assert.equal(get('report-ready-prompt').hidden, false);
assert.equal(get('prompt-preview-report').href, '/agentic/analytics/?report=selected-report');
get('dismiss-report-ready').click();
assert.equal(get('report-ready-prompt').hidden, true);
assert.equal(get('quick-superset-publish').hidden, false);
templates.reset();
assert.equal(get('generate-superset-report').hidden, true);
assert.equal(get('generate-superset-report').href, undefined);
assert.equal(get('report-ready-prompt').hidden, true);
templates.show(report, {});
assert.equal(get('generate-superset-report').hidden, true, 'Unsupported snapshots cannot reuse a previous report token');
assert.match(get('superset-result-status').textContent, /no exportable snapshot/);
assert.equal(requests.length, 0, 'Displaying results never publishes');
(async () => {
  templates.show(report, {summary_token: 'selected-report'});
  assert.equal(get('report-ready-prompt').hidden, true, 'The same result does not reopen the prompt');
  const button = get('quick-superset-publish');
  assert.equal(button.hidden, false);
  await Promise.all([button.click(), button.click()]);
  assert.deepEqual(requests, [['/agentic/api/dashboards/',
    {action: 'quick_publish', report_token: 'selected-report'}, 'csrf-token']]);
  assert.equal(button.hidden, true);
  assert.equal(get('open-superset-result').href, 'https://superset.internal/superset/dashboard/9/');
  templates.reset();
  assert.equal(get('open-superset-result').hidden, true);
  console.log('Report service action checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
