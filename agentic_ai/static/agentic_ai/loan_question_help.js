(function (root) {
    'use strict';
    function render(host, groups, input) {
        host.replaceChildren();
        host.hidden = !Array.isArray(groups) || groups.length === 0;
        if (host.hidden) return;
        const doc = host.ownerDocument;
        const hint = doc.createElement('p');
        hint.textContent = 'প্রশ্নে ক্লিক করুন, নিজের তথ্য দিয়ে সম্পাদনা করুন, তারপর Ask assistant চাপুন।';
        host.append(hint);
        groups.forEach(group => {
            const heading = doc.createElement('h4');
            heading.textContent = group.title;
            const list = doc.createElement('ul');
            group.questions.forEach(question => {
                const item = doc.createElement('li');
                const button = doc.createElement('button');
                button.type = 'button';
                button.className = 'copy-button';
                button.textContent = question;
                button.addEventListener('click', () => {
                    if (input.disabled) return;
                    input.value = question;
                    input.focus();
                    input.scrollIntoView({behavior: 'smooth', block: 'center'});
                });
                item.append(button);
                list.append(item);
            });
            host.append(heading, list);
        });
    }
    if (typeof module !== 'undefined') module.exports = {render};
    else root.BuroLoanQuestionHelp = {render};
})(typeof window !== 'undefined' ? window : globalThis);
