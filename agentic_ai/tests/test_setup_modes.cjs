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
const end = template.indexOf("document.querySelectorAll('[data-channel-action]')", start);
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

async function checkManualDiscovery() {
  const elements = new Map();
  const getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, { disabled: false, textContent: '' });
    return elements.get(id);
  };
  let finish;
  const reply = new Promise((resolve) => { finish = resolve; });
  let calls = 0;
  const discovery = vm.createContext({
    busy: false, findingTools: false, settingsVersion: 0, gatewayRefreshId: 0,
    catalogLoaded: false, catalog: [], statusBox: getElementById('setup-status'),
    document: { getElementById, querySelectorAll: () => [] },
    requestSetup: async (payload) => {
      calls++;
      assert.equal(payload.action, 'discover');
      assert.equal(payload.background, true);
      return reply;
    },
    showHealth() {}, checkFailure() {},
    renderTools: (tools) => assert.equal(tools[0].name, 'find-buro-training-locations'),
  });
  const fragment = (start, end) => template.slice(template.indexOf(start), template.indexOf(end));
  vm.runInContext(fragment('function refreshButtons()', 'async function requestSetup(')
    + fragment('async function refreshGateway(', 'async function refreshHealth(')
    + fragment("document.getElementById('discover').onclick", "document.getElementById('check-model').onclick"), discovery);

  const button = getElementById('discover');
  const pending = button.onclick();
  assert.equal(button.disabled, true);
  assert.match(getElementById('setup-status').textContent, /Looking for tools/);
  await button.onclick();
  await discovery.refreshGateway(0, false);
  assert.equal(calls, 1);
  finish({ tools: [{ name: 'find-buro-training-locations' }], roles: [], missing: [], message: 'Found 1 tool.' });
  await pending;
  assert.equal(button.disabled, false);
  assert.equal(getElementById('setup-status').textContent, 'Found 1 tool.');

  discovery.requestSetup = async () => { throw new Error('Gateway unavailable'); };
  await button.onclick();
  assert.equal(button.disabled, false);
  assert.equal(getElementById('setup-status').textContent, 'Gateway unavailable');
}

checkManualDiscovery().then(() => console.log('Manual discovery checks passed.')).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
