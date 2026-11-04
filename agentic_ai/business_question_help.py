"""Read-only question guides from authorized tools and published business modules."""
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone


def is_business_question_help(query):
    return bool(re.search(r'\b(?:sample|example|supported)\b.{0,50}\bquer(?:y|ies)\b'
                          r'|(?:নমুনা|উদাহরণ).{0,40}(?:প্রশ্ন|কুয়েরি|কুয়েরি)', query, re.I))


def question_domain(query):
    savings = bool(re.search(r'\bsavings?\b|সঞ্চ[য়য়]|সেভিং', query, re.I))
    loans = bool(re.search(r'\bloans?\b|ঋণ|বকেয়া|বকেয়া', query, re.I))
    if savings and not loans:
        return 'savings'
    if loans and not savings:
        return 'loans'
    return 'business'


def is_savings_report_request(query):
    # Loan member reports can contain savings balance columns. Do not route
    # those existing inquiries to the deposits/withdrawals transaction endpoint.
    return question_domain(query) == 'savings' and bool(re.search(
        r'\b(?:deposits?|withdrawals?|net\s+movement|savings?[- ]reports?)\b'
        r'|জমা|উত্তোলন|নিট পরিবর্তন|সঞ্চয়ের প্রতিবেদন|সঞ্চয় রিপোর্ট', query, re.I))


SAVINGS_OPERATIONS = {'period-summary', 'branch-summary', 'zone-summary',
                      'zone-branches', 'branch-product-summary', 'zone-product-summary'}


def is_savings_report_tool(tool):
    props = tool.inputSchema.get('properties', {})
    return bool(re.search(r'saving|সঞ্চ|সেভিং', tool.name + ' ' + (tool.description or ''), re.I)
                and tool.inputSchema.get('type') == 'object'
                and all(props.get(key, {}).get('type') == 'string'
                        for key in ('operation', 'from_date', 'to_date_exclusive'))
                and any(isinstance(op, str) and op in SAVINGS_OPERATIONS
                        for op in props['operation'].get('enum', [])))


def savings_question_help(tools, today=None):
    today = today or timezone.localdate(timezone=ZoneInfo('Asia/Dhaka'))
    end = today.replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    period = f'{start.isoformat()} থেকে {end.isoformat()} পর্যন্ত (শেষ তারিখ বাদ দিয়ে)'
    sections, sources = [], []
    for tool in tools:
        if not is_savings_report_tool(tool):
            continue
        props = tool.inputSchema.get('properties', {})
        operations = props['operation'].get('enum', [])
        questions = []
        def has(field):
            return props.get(field, {}).get('type') == 'string'
        def example_code(field, fallback):
            values = props.get(field, {}).get('enum')
            return values[0] if isinstance(values, list) and values and isinstance(values[0], str) else fallback
        branch = example_code('branch_code', '[অনুমোদিত শাখা কোড]')
        zone = example_code('zone_code', '[অনুমোদিত জোন কোড]')
        if 'period-summary' in operations and has('branch_code'):
            questions.append(f'{period} {branch} শাখার সঞ্চয় পণ্যভিত্তিক জমা, উত্তোলন ও নিট পরিবর্তন দেখাও।')
        if 'branch-summary' in operations:
            questions.append(f'{period} অনুমোদিত শাখাগুলোর সঞ্চয় জমা, উত্তোলন ও নিট পরিবর্তন শাখাভিত্তিক দেখাও।')
        if 'zone-summary' in operations:
            questions.append(f'{period} অনুমোদিত শাখার তথ্যের মধ্যে জোনভিত্তিক সঞ্চয় জমা, উত্তোলন ও নিট পরিবর্তন দেখাও।')
        if 'zone-branches' in operations and has('zone_code'):
            questions.append(f'{period} {zone} জোনের অনুমোদিত শাখাগুলোর সঞ্চয় জমা ও উত্তোলন শাখাভিত্তিক দেখাও।')
        if 'branch-product-summary' in operations:
            questions.append(f'{period} অনুমোদিত তথ্যের মধ্যে শাখা ও সঞ্চয় পণ্যভিত্তিক জমা, উত্তোলন ও নিট পরিবর্তন দেখাও।')
        if 'zone-product-summary' in operations:
            questions.append(f'{period} অনুমোদিত তথ্যের মধ্যে জোন ও সঞ্চয় পণ্যভিত্তিক জমা, উত্তোলন ও নিট পরিবর্তন দেখাও।')
        if questions:
            sources.append(tool.name)
            sections.append({'title': 'সঞ্চয়ের ব্যবসায়িক প্রতিবেদন', 'questions': questions})
    return sections, sources


def business_question_help(user, query, tools, report_names):
    from .models import AgentTool, BusinessModule
    from .mcp_gateway import _allowed
    from .loan_question_help import loan_question_help
    from .loan_access import own_access_summary

    # Recheck account, publication and roles after discovery.
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=getattr(user, 'pk', None), is_active=True).first()
    records = list(AgentTool.objects.filter(name__in=[tool.name for tool in tools]).select_related('module'))
    permitted = {record.name for record in records if _allowed(record, user)} if user else set()
    tools = [tool for tool in tools if tool.name in permitted]
    domain = question_domain(query)
    modules = list(BusinessModule.objects.filter(is_active=True, tools__name__in=permitted).distinct())
    requested = [module for module in modules if any(
        re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', query, re.I)
        for term in (module.name, module.slug))]
    if requested:
        modules = requested
        module_tools = {record.name for record in records if record.module_id in {m.pk for m in modules} and record.name in permitted}
        tools = [tool for tool in tools if tool.name in module_tools]
    groups, sources = savings_question_help(tools)
    if domain == 'business':
        loan = loan_question_help(tools, report_names, own_access_summary(user))
        groups += loan['question_groups']; sources += loan['help_tools']
    else:
        modules = [module for module in modules if any(record.module_id == module.pk and record.name in sources for record in records)]
    for module in modules:
        questions = [q.strip() for q in module.starter_questions.splitlines() if q.strip()][:20]
        if questions:
            groups.append({'title': module.name, 'questions': questions})
            sources += [record.name for record in records if record.module_id == module.pk and record.name in permitted]
    title = 'সঞ্চয় নিয়ে কী কী প্রশ্ন করতে পারেন' if domain == 'savings' else 'ব্যবসার কাজে কী কী প্রশ্ন করতে পারেন'
    intro = [title, 'বর্তমানে অনুমোদিত টুলের ঘোষিত সুবিধা ও প্রকাশিত মডিউলের উদাহরণ অনুযায়ী এই নির্দেশিকা।',
             'বন্ধনীর কোড নিজের অনুমোদিত শাখা/জোন দিয়ে বদলান; শুরুর শূন্য রাখুন। তারিখ উদাহরণ এবং শেষ তারিখ বাদ যাবে।',
             'উদাহরণে ক্লিক করলে শুধু প্রশ্ন প্রস্তুত হবে; সম্পাদনা করে Ask assistant চাপুন।']
    if domain == 'savings':
        intro.append('এটি জমা, উত্তোলন ও নিট পরিবর্তনের নির্দেশিকা; হিসাবের স্থিতি, মেয়াদপূর্তি, পূর্বাভাস বা বকেয়া ঋণের রিপোর্ট নয়।')
    if not groups:
        intro.append('আপনার জন্য এই বিষয়ে সমর্থিত প্রশ্নের উদাহরণ পাওয়া যায়নি। প্রশাসককে tool ও module configuration যাচাই করতে বলুন।')
    lines = ['\n'.join(intro)]
    for group in groups:
        lines += [group['title'], *group['questions']]
    return {'ok': True, 'kind': 'business_question_help', 'tool_executed': False,
            'help_tools': sorted(set(sources)), 'question_groups': groups,
            'answer': '\n\n'.join(lines)}
