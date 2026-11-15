/* Local educational content. Never calls a model, sends a query, or estimates progress. */
(() => {
    'use strict';
    const root = document.getElementById('wait-learning');
    if (!root) return;
    const byId = name => document.getElementById('wait-' + name);
    const lessons = [
        {en: ['AGENTIC AI', 'From a question to an action', 'An AI agent can interpret a request, select an available tool and use its result to answer. The tools and permissions available determine what it can do.', 'State the task, the information to use and the output you want.'],
         bn: ['এজেন্টিক AI', 'প্রশ্ন থেকে কাজে', 'একটি AI এজেন্ট অনুরোধ বুঝে উপলব্ধ টুল বেছে নিতে পারে এবং তার ফল থেকে উত্তর তৈরি করতে পারে। কী কাজ করা সম্ভব, তা উপলব্ধ টুল ও অনুমতির ওপর নির্ভর করে।', 'কাজটি কী, কোন তথ্য লাগবে এবং কী ধরনের ফল চান—স্পষ্ট করে বলুন।']},
        {en: ['AI CONCEPT · RAG', 'Give the model information to work with', 'Retrieval-augmented generation (RAG) brings relevant source material into a model’s context before it answers. This helps ground an answer, but the source and interpretation still need checking.', 'Use document retrieval for source-based answers and a reporting tool for calculated business figures.'],
         bn: ['AI ধারণা · RAG', 'উত্তরের আগে প্রাসঙ্গিক তথ্য', 'Retrieval-augmented generation বা RAG-এ উত্তর তৈরির আগে প্রাসঙ্গিক উৎসের তথ্য মডেলকে দেওয়া হয়। এতে উত্তর তথ্যভিত্তিক হতে সাহায্য করে, তবে উৎস ও ব্যাখ্যা যাচাই করা জরুরি।', 'উৎসভিত্তিক উত্তরের জন্য ডকুমেন্ট অনুসন্ধান এবং হিসাব করা ব্যবসায়িক অঙ্কের জন্য রিপোর্টিং টুল ব্যবহার করুন।']},
        {en: ['BEST USES', 'Start with one well-defined task', 'Agents can help retrieve information, summarize documents and prepare comparisons when suitable tools are available. A clear goal is easier to check than a broad request to do everything.', 'Ask for one report first. Check its date and scope, then request a summary or comparison if supported.'],
         bn: ['কার্যকর ব্যবহার', 'একটি নির্দিষ্ট কাজ দিয়ে শুরু করুন', 'উপযুক্ত টুল থাকলে এজেন্ট তথ্য খোঁজা, ডকুমেন্ট সংক্ষেপ করা এবং তুলনা তৈরিতে সাহায্য করতে পারে। সবকিছু একসঙ্গে চাওয়ার চেয়ে নির্দিষ্ট কাজের ফল যাচাই করা সহজ।', 'আগে একটি রিপোর্ট চান। তারিখ ও পরিধি যাচাই করে সমর্থিত হলে সারসংক্ষেপ বা তুলনা চান।']},
        {en: ['BETTER QUESTIONS', 'Give the report a clear boundary', 'A report date and a disbursement period answer different questions. Include both when relevant, along with an exact permitted branch code.', '“As of [report date], show loans disbursed from [start] to [end] in branch [your permitted code].”'],
         bn: ['ভালো প্রশ্ন', 'রিপোর্টের সীমা স্পষ্ট করুন', 'রিপোর্টের তারিখ ও ঋণ বিতরণের সময়সীমা আলাদা বিষয়। প্রযোজ্য হলে দুটোই লিখুন এবং অনুমোদিত শাখার সঠিক কোড দিন।', '“[রিপোর্টের তারিখ] অনুযায়ী [শুরুর তারিখ] থেকে [শেষ তারিখ] পর্যন্ত [অনুমোদিত শাখা কোড]-এ বিতরণ করা ঋণ দেখাও।”']},
        {en: ['RANKING', 'Top loans and top branches are different', 'Ranking loans compares individual loans. Ranking branches compares grouped branch totals. Name the level, measure, direction and number of results.', '“Show the top 10 loans by overdue amount, highest first, in my permitted branch.”'],
         bn: ['র‍্যাঙ্কিং', 'সেরা ঋণ ও সেরা শাখা এক নয়', 'ঋণভিত্তিক র‍্যাঙ্কিং পৃথক ঋণ তুলনা করে। শাখাভিত্তিক র‍্যাঙ্কিং শাখার সমষ্টি তুলনা করে। স্তর, পরিমাপ, ক্রম ও ফলের সংখ্যা লিখুন।', '“আমার অনুমোদিত শাখায় বকেয়ার পরিমাণ বেশি থেকে কম সাজিয়ে প্রথম ১০টি ঋণ দেখাও।”']},
        {en: ['TRUST & ACCESS', 'A question cannot expand your access', 'Tool permission controls which capability you can use. Data scope controls which records it may retrieve. Both can apply to the same request.', 'If access is blocked, ask your administrator to review your assigned tool and branch or zone scope.'],
         bn: ['আস্থা ও অনুমতি', 'প্রশ্ন লিখে অনুমতি বাড়ানো যায় না', 'টুলের অনুমতি ঠিক করে কোন কাজ করতে পারবেন। ডেটার পরিধি ঠিক করে কোন তথ্য দেখা যাবে। একই অনুরোধে দুটোই প্রযোজ্য হতে পারে।', 'অ্যাক্সেস বন্ধ থাকলে প্রশাসককে আপনার টুল এবং শাখা বা জোনের অনুমতি যাচাই করতে বলুন।']},
        {en: ['READING RESULTS', 'A top-10 list is a slice of the data', 'A ranked list may show only the returned rows, rather than the full portfolio. Check the date, scope, sorting measure and any truncation notice before using it.', 'Verify important totals against the authoritative report before making a financial decision.'],
         bn: ['ফল বোঝা', 'প্রথম ১০টি ফল পুরো তথ্য নয়', 'র‍্যাঙ্কিংয়ে পুরো পোর্টফোলিওর বদলে শুধু ফেরত আসা সারি থাকতে পারে। ব্যবহারের আগে তারিখ, পরিধি, সাজানোর মান ও সীমিত ফলের নোট দেখুন।', 'আর্থিক সিদ্ধান্তের আগে গুরুত্বপূর্ণ অঙ্ক মূল অনুমোদিত রিপোর্টের সঙ্গে মিলিয়ে নিন।']},
        {en: ['RELIABLE WORKFLOWS', 'Waiting is not a reason to submit twice', 'A long-running request may still be processing. Submitting again can start another report. If a timeout occurs, check the attempt history before retrying.', 'Keep this page open while waiting. Read the returned status before deciding what to do next.'],
         bn: ['নির্ভরযোগ্য কাজ', 'অপেক্ষা মানেই আবার পাঠাতে হবে না', 'দীর্ঘ অনুরোধ তখনও চলতে পারে। আবার পাঠালে আরেকটি রিপোর্ট শুরু হতে পারে। টাইমআউট হলে পুনরায় চেষ্টা করার আগে অনুরোধের ইতিহাস দেখুন।', 'অপেক্ষার সময় পৃষ্ঠাটি খোলা রাখুন। পরের পদক্ষেপের আগে ফেরত আসা স্ট্যাটাস পড়ুন।']}
    ];
    const copy = {
        en: {heading:'WHILE YOU WAIT', state:'Request in progress', title:'A small idea. A better next question.', intro:'Explore a quick lesson while your request runs.', hide:'Hide tips', show:'Show tips', pause:'Pause tips', resume:'Resume tips', example:'Try this approach', plan:'Understand', tool:'Use a tool', check:'Check the result', disclaimer:'Learning tips, not a live progress tracker. Your result will appear as soon as it is ready.', previous:'Previous lesson', next:'Next lesson'},
        bn: {heading:'অপেক্ষায় একটু শেখা', state:'অনুরোধ চলছে', title:'ছোট্ট একটি ধারণা। আরও ভালো প্রশ্ন।', intro:'আপনার অনুরোধ চলার সময় নতুন কিছু শিখুন।', hide:'টিপস লুকান', show:'টিপস দেখুন', pause:'টিপস থামান', resume:'টিপস চালু করুন', example:'এভাবে চেষ্টা করুন', plan:'অনুরোধ বোঝা', tool:'টুল ব্যবহার', check:'ফল যাচাই', disclaimer:'এগুলো শেখার টিপস, আপনার অনুরোধের সরাসরি অগ্রগতি নয়। ফল প্রস্তুত হলেই দেখানো হবে।', previous:'আগের পাঠ', next:'পরের পাঠ'}
    };
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let active = false, index = 0, language = 'en', paused = reduced.matches, hidden = false;
    let revealTimer = null, rotation = null;
    const read = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
    const save = (key, value) => { try { localStorage.setItem(key, value); } catch (_) {} };
    function render() {
        const text = copy[language], lesson = lessons[index][language];
        root.lang = language;
        byId('learning-heading').textContent = text.heading;
        byId('state-label').textContent = text.state;
        byId('intro-title').textContent = text.title;
        byId('intro-copy').textContent = text.intro;
        byId('topic').textContent = lesson[0];
        byId('count').textContent = (index + 1) + ' / ' + lessons.length;
        byId('tip-title').textContent = lesson[1];
        byId('tip-body').textContent = lesson[2];
        byId('tip-example').textContent = lesson[3];
        byId('example-label').textContent = text.example;
        byId('concept-plan').textContent = text.plan;
        byId('concept-tool').textContent = text.tool;
        byId('concept-check').textContent = text.check;
        byId('disclaimer').textContent = text.disclaimer;
        byId('toggle').textContent = hidden ? text.show : text.hide;
        byId('toggle').setAttribute('aria-expanded', String(!hidden));
        byId('lesson').hidden = hidden;
        byId('pause').textContent = paused ? text.resume : text.pause;
        byId('pause').setAttribute('aria-pressed', String(paused));
        byId('prev').setAttribute('aria-label', text.previous);
        byId('next').setAttribute('aria-label', text.next);
        byId('language').value = language;
    }
    function schedule() {
        clearInterval(rotation);
        rotation = null;
        if (active && !hidden && !paused && !document.hidden) {
            rotation = setInterval(() => { index = (index + 1) % lessons.length; render(); }, 12000);
        }
    }
    function stop() {
        active = false;
        clearTimeout(revealTimer);
        clearInterval(rotation);
        revealTimer = rotation = null;
        const restoreFocus = root.contains(document.activeElement);
        root.hidden = true;
        if (restoreFocus) {
            const title = document.getElementById('result-title');
            title.setAttribute('tabindex', '-1');
            title.focus({preventScroll: true});
        }
    }
    function start(question) {
        stop();
        active = true;
        index = 0;
        const savedLanguage = read('buro-wait-language');
        language = ['en','bn'].includes(savedLanguage) ? savedLanguage : (/[\u0980-\u09ff]/.test(question) ? 'bn' : 'en');
        hidden = read('buro-wait-hidden') === 'true';
        paused = reduced.matches;
        render();
        revealTimer = setTimeout(() => { if (active) { root.hidden = false; schedule(); } }, 700);
    }
    byId('language').addEventListener('change', event => {
        language = event.target.value === 'bn' ? 'bn' : 'en';
        save('buro-wait-language', language); render();
    });
    byId('toggle').addEventListener('click', () => {
        hidden = !hidden; save('buro-wait-hidden', String(hidden)); render(); schedule();
    });
    byId('pause').addEventListener('click', () => { paused = !paused; render(); schedule(); });
    for (const [id, offset] of [['prev', -1], ['next', 1]]) {
        byId(id).addEventListener('click', () => { index = (index + offset + lessons.length) % lessons.length; paused = true; render(); schedule(); });
    }
    // Keep content stable while someone is reading with keyboard or assistive technology.
    root.addEventListener('focusin', event => { if (event.target !== byId('pause') && !paused) { paused = true; render(); schedule(); } });
    document.addEventListener('visibilitychange', schedule);
    reduced.addEventListener('change', () => { if (reduced.matches) { paused = true; render(); schedule(); } });
    window.addEventListener('pagehide', stop);
    window.BuroWaitLearning = {start, stop};
})();
