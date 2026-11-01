const {test} = require('node:test');
const assert = require('node:assert/strict');
const {render} = require('../../static/agentic_ai/loan_question_help.js');

function element(tag) {
    return {tag, children: [], hidden: false, handlers: {},
        append(...items) { this.children.push(...items); },
        replaceChildren() { this.children = []; },
        addEventListener(event, handler) { this.handlers[event] = handler; }};
}

test('click prepares an editable question without submitting or parsing markup', () => {
    const host = element('section');
    host.ownerDocument = {createElement: element};
    let focused = false;
    const input = {disabled: false, value: '', focus() { focused = true; }, scrollIntoView() {}};
    const question = '0253 শাখার <img src=x onerror=alert(1)> প্রশ্ন';
    render(host, [{title: 'ঋণ', questions: [question]}], input);
    const button = host.children[2].children[0].children[0];
    assert.equal(input.value, '');
    assert.equal(button.type, 'button');
    assert.equal(button.textContent, question);
    assert.equal(button.innerHTML, undefined);
    button.handlers.click();
    assert.equal(input.value, question);
    assert.equal(focused, true);
    input.value = 'edited question';
    assert.equal(input.value, 'edited question');
    input.disabled = true;
    button.handlers.click();
    assert.equal(input.value, 'edited question');
});

test('empty or replacement results clear previous suggestions', () => {
    const host = element('section');
    host.ownerDocument = {createElement: element};
    host.children.push('old');
    render(host, [], {});
    assert.equal(host.hidden, true);
    assert.deepEqual(host.children, []);
});
