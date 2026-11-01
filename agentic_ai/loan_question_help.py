"""Deterministic Bangla examples from the current authorized MCP loan schemas."""
import re


def is_loan_question_help(query):
    text = query.casefold().strip()
    text = re.sub(r'\b(?:queestions?|quesstions?|quessions?)\b', 'questions', text)
    # Require a request for question examples, never intercept a financial query
    # merely because it contains words such as loan, report or show.
    return bool(
        re.search(r'(?:what|which|all|possible|example|sample|list|supported).{0,70}'
                  r'ques?tions?\b|\bques?tions?\b.{0,70}(?:can i|can we|ask|possible|supported)', text)
        or re.search(r'(?:কী কী|কি কি|কোন কোন|কী ধরনের|কি ধরনের|সব|সকল|সম্ভাব্য).{0,60}প্রশ্ন'
                     r'|প্রশ্ন.{0,60}(?:তালিকা|উদাহরণ|করতে পারি|করা যায়|করা যায়)', text)
    )


def loan_question_help(tools, report_names, examples=None):
    """`tools` must come from authorized, fresh gateway discovery."""
    from .native_loan_query import supports_native_query
    from .loan_report_contract import supports_ranking

    sections = {}
    sources = []

    def add(title, question):
        questions = sections.setdefault(title, [])
        if question not in questions:
            questions.append(question)

    for tool in tools:
        if tool.name not in report_names:
            continue
        props = tool.inputSchema.get('properties', {})
        native = supports_native_query(tool, report_names)
        legacy = (tool.inputSchema.get('type') == 'object' and
                  all(props.get(k, {}).get('type') == 'string' for k in ('branchCode', 'reportDate')))
        if not native and not legacy:
            continue
        sources.append(tool.name)
        date = '2026-09-30'
        base = f'{date} তারিখ অনুযায়ী'
        if legacy:
            add('শাখার রিপোর্ট', f'{base} 0253 শাখার ঋণ ও বকেয়া রিপোর্ট দেখাও।')
            for fields in ('ঋণপণ্য ও বিতরণকৃত টাকার পরিমাণ', 'অবশিষ্ট আসল ও বকেয়া',
                           'বিতরণের তারিখ ও মেয়াদপূর্তির তারিখ', 'বকেয়া দিনের সংখ্যা ও বকেয়া কিস্তির সংখ্যা',
                           'কিস্তির পরিমাণ ও পরিশোধের সময়সূচির ধরন'):
                add('সদস্যের রিপোর্টের কলাম', f'{base} 0253 শাখার সদস্য 000123-এর {fields} দেখাও।')
        if supports_ranking(tool, report_names):
            for field, label, value in (('zoneCode', 'কোড', '07'), ('zoneName', 'নাম', '[জোনের সঠিক নাম]')):
                if props.get(field, {}).get('type') != 'string':
                    continue
                for count in (5, 10, 20):
                    if count <= props['limit'].get('maximum', 100):
                        add('জোনে সদস্যের বকেয়া র‍্যাঙ্কিং',
                            f'{base} জোন {label} {value}-এর অনুমোদিত শাখাগুলোতে মোট বকেয়া সবচেয়ে বেশি এমন {count} জন সদস্য দেখাও।')
        if not native:
            continue
        levels = props['level'].get('enum', [])
        sorts = props['sort_by'].get('enum', [])
        orders = props.get('sort_order', {}).get('enum', ['DESC'])
        metrics = {'DUEAMOUNT': 'বকেয়ার পরিমাণ', 'OUTSTANDINGP': 'অবশিষ্ট আসল',
                   'OUTSTANDING_PI': 'আসল ও সুদসহ মোট অবশিষ্ট', 'DUE_DAYS': 'বকেয়া দিনের মান',
                   'LOAN_COUNT': 'রিপোর্টে অন্তর্ভুক্ত ঋণের সংখ্যা'}
        labels = {'LOAN': 'ঋণ', 'MEMBER': 'সদস্য', 'BRANCH': 'শাখা', 'ZONE': 'জোন',
                  'AREA': 'এরিয়া', 'DIVISION': 'ডিভিশন', 'PRODUCT': 'ঋণপণ্য', 'EMPLOYEE': 'কর্মী'}
        for level, label in labels.items():
            if level not in levels:
                continue
            scope = '0253 শাখায়' if level in {'LOAN', 'MEMBER', 'PRODUCT', 'EMPLOYEE'} else 'আমার অনুমোদিত তথ্যের মধ্যে'
            for metric, metric_label in metrics.items():
                if metric not in sorts or (level == 'LOAN' and metric == 'LOAN_COUNT'):
                    continue
                for order, order_label in (('DESC', 'বেশি থেকে কম'), ('ASC', 'কম থেকে বেশি')):
                    if order not in orders:
                        continue
                    add(f'{label}ভিত্তিক সাজানো ফল',
                        f'{base} {scope} {label}ভিত্তিক {metric_label} {order_label} সাজিয়ে প্রথম ১০টি ফল দেখাও।')
        # Filter examples use only explicitly declared fields and loan-level sorting.
        if 'LOAN' not in levels or 'DUEAMOUNT' not in sorts or 'DESC' not in orders:
            continue
        filters = []
        def string_field(key):
            return props.get(key, {}).get('type') == 'string'
        if string_field('member_id'):
            filters.append('সদস্য 000123-এর')
        if string_field('product_name'):
            filters.append('[পণ্যের সঠিক নাম] পণ্যের')
        if string_field('employee_code'):
            filters.append('EMP001 কর্মীর আওতাভুক্ত')
        if all(string_field(k) for k in ('disbursed_from', 'disbursed_to')):
            filters.append('2025-01-01 থেকে 2025-12-31 পর্যন্ত বিতরণ করা')
        for key, label, values in (('min_due', 'টাকা', (1000, 5000, 10000)),
                                   ('min_days', 'দিন', (30, 60, 90, 180))):
            if props.get(key, {}).get('type') in {'number', 'integer'}:
                filters.extend(f'কমপক্ষে {value} {label} বকেয়া থাকা' for value in values)
        for phrase in filters:
            add('নির্দিষ্ট ফিল্টার', f'{base} 0253 শাখায় {phrase} ঋণগুলোর মধ্যে সর্বোচ্চ বকেয়ার ১০টি দেখাও।')
        if string_field('zone_code'):
            add('জোনের ঋণ', f'{base} 07 জোনের অনুমোদিত শাখাগুলোতে সর্বোচ্চ বকেয়া থাকা ১০টি ঋণ দেখাও।')

    if not sources:
        return {'ok': True, 'kind': 'loan_question_help', 'tool_executed': False,
                'help_tools': [], 'question_groups': [],
                'answer': 'আপনার জন্য বর্তমানে সমর্থিত কোনো ঋণ রিপোর্ট টুল পাওয়া যায়নি। প্রশাসকের সঙ্গে যোগাযোগ করুন।'}
    groups = [{'title': title, 'questions': questions} for title, questions in sections.items()]
    lines = ['ঋণ ও বকেয়া রিপোর্টে কী কী প্রশ্ন করতে পারেন',
             'আপনার অনুমোদিত টুলের বর্তমান ঘোষিত সুবিধা অনুযায়ী নিচের উদাহরণগুলো তৈরি হয়েছে।',
             'কোড, সদস্য আইডি, পণ্যের নাম ও তারিখ নিজের তথ্য দিয়ে বদলাবেন; শুরুর শূন্য রাখবেন।',
             'তারিখগুলো উদাহরণ। ফল অনুমোদিত তথ্যের মধ্যে সীমিত এবং সর্বোচ্চ ১০০ সারি প্রদর্শিত হতে পারে।',
             'সদস্যের অতিরিক্ত কলাম মূল রিপোর্টে থাকলেই পাওয়া যাবে। ঋণের সংখ্যা বলতে রিপোর্টে অন্তর্ভুক্ত ঋণ বোঝায়।',
             'এটি প্রশ্নের নির্দেশিকা; backend-এর স্বাস্থ্য বা পুরো জোনের তথ্যের নিশ্চয়তা নয়।',
             'সাধারণ ব্যবহারযোগ্য ধরন দেখানো হয়েছে; কোড, তারিখ, সংখ্যা ও সমর্থিত ফিল্টার বদলে আরও প্রশ্ন করা যায়।']
    count = 0
    for group in groups:
        lines.extend(['', group['title']])
        for question in group['questions']:
            count += 1
            lines.append(f'{count}. {question}')
    lines.extend(['', 'এই তালিকা তৈরি করতে কোনো ঋণ রিপোর্ট চালানো বা মডেল কল করা হয়নি।',
                  'দুই তারিখের তুলনা, ভবিষ্যৎ পূর্বাভাস বা বকেয়ার কারণ এই নির্দেশিকায় সমর্থিত দাবি করা হচ্ছে না।'])
    def personalize(text):
        if examples and examples.get('example_branch'):
            text = re.sub(r'(?<![A-Za-z0-9])(0253|07)(?![A-Za-z0-9])',
                          lambda match: examples['example_branch'] if match[0] == '0253' else examples['example_zone'], text)
        return text
    for group in groups:
        group['questions'] = [personalize(question) for question in group['questions']]
    return {'ok': True, 'kind': 'loan_question_help', 'tool_executed': False,
            'help_tools': sorted(set(sources)), 'question_groups': groups,
            'answer': personalize('\n'.join(lines))}
