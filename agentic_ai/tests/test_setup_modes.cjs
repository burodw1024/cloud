// Run with: node agentic_ai/tests/test_setup_modes.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.listeners = {}; this.style = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  change(value) {
    this.value = value;
    if (this.onchange) this.onchange();
    if (this.listeners.change) this.listeners.change();
  }
}
const template = fs.readFileSync(path.join(__dirname, '../templates/agentic_ai/setup.html'), 'utf8');
const start = template.indexOf('function renderTools(');
const end = template.indexOf("document.getElementById('check-whatsapp')", start);
const context = {
  catalog: [],
  document: { createElement: () => new Element(), getElementById: () => new Element() },
  checkbox: (label, checked) => ({ label: new Element(), input: Object.assign(new Element(), { checked }) }),
};
vm.createContext(context);
vm.runInContext(template.slice(start, end), context);

for (const target of ['UI', 'APP']) {
  context.renderTools([{
    name: 'loan-due-report', enabled: true, roles: ['manager'],
    loan_access_mode: 'FIXED', fixed_branch_codes: ['0253', '0254'],
    requires_loan_scope: true,
  }], ['manager']);
  const tool = context.catalog[0];
  assert.equal(tool.branches.value, '0253,0254', 'Discovery preserves existing fixed scope');
  tool.mode.change(target);
  assert.equal(tool.branches.value, '', 'Application mode must not submit the hidden fixed allowlist');
  assert.equal(tool.loanScope.checked, true);
  assert.equal(tool.loanScope.disabled, true);
  tool.mode.change('FIXED');
  assert.equal(tool.branches.value, '', 'Returning to fixed mode requires an explicit allowlist');
}
console.log('Setup mode transition checks passed.');
