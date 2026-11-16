// Run with: node agentic_ai/tests/test_report_service_actions.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor() { this.dataset = {}; this.children = []; this.hidden = false; this.value = ''; }
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  addEventListener() {}
  querySelectorAll() { return []; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
}
const ids = new Map();
const get = id => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
get('report-templates').dataset.default = 'table';
get('generate-superset-report').dataset.studioUrl = '/agentic/analytics/';
const context = { document: {getElementById: get, createElement: () => new Element()}, window: {print() {}},
  fetch() { throw new Error('Showing actions must never send or publish'); } };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/agentic_ai/report_templates.js'), 'utf8'), context);
const templates = context.window.BuroReportTemplates;
const report = {columns: ['BRANCHCODE'], rows: [{BRANCHCODE: '0253'}]};
templates.show(report, {summary_token: 'selected-report'});
assert.equal(get('generate-superset-report').href, '/agentic/analytics/?report=selected-report');
assert.equal(get('generate-superset-report').hidden, false);
templates.reset();
assert.equal(get('generate-superset-report').hidden, true);
assert.equal(get('generate-superset-report').href, undefined);
templates.show(report, {});
assert.equal(get('generate-superset-report').hidden, true, 'Unsupported snapshots cannot reuse a previous report token');
assert.match(get('superset-result-status').textContent, /no supported export snapshot/);
console.log('Report service action checks passed.');
