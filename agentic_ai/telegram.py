"""Direct Telegram delivery of authorized report links and aggregate previews."""

import json
import os
import re
from decimal import Decimal
from http.client import HTTPSConnection, HTTPException
from io import BytesIO
from urllib.parse import urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from PIL import Image, ImageDraw, ImageFont

from .api import _can_replay_result
from .models import ReportRun
from .share_channels import can_share

METRICS = {
    'LOAN_COUNT': ('ঋণের সংখ্যা', 'Loan count'),
    'DUEAMOUNT': ('বকেয়া', 'Due amount'),
    'OUTSTANDINGP': ('অপরিশোধিত মূলধন', 'Outstanding principal'),
    'OUTSTANDING_PI': ('সুদসহ অপরিশোধিত স্থিতি', 'Outstanding including interest'),
}
GROUP_CODES = {'BRANCH': 'BRANCHCODE', 'ZONE': 'ENTITY_CODE'}


def configuration(user):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    origin = os.environ.get('TELEGRAM_REPORT_ORIGIN', '')
    try:
        url = urlsplit(origin)
        port = url.port
        recipients = json.loads(os.environ.get('TELEGRAM_USER_CHATS', '{}'))
        chat_id = recipients.get(str(user.pk), '') if isinstance(recipients, dict) else ''
        if (re.fullmatch(r'[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,100}', token)
                and url.scheme == 'https' and url.hostname and not url.username and not url.password
                and not url.query and not url.fragment and url.path in ('', '/')
                and (port is None or port > 0)
                and not re.search(r'\s|[\\]', origin)
                and isinstance(chat_id, str) and re.fullmatch(r'[1-9][0-9]{0,15}', chat_id)
                and list(recipients.values()).count(chat_id) == 1):
            return token, origin.rstrip('/'), chat_id
    except ValueError:
        pass
    return None


def describe(user):
    detail = {'key': 'telegram', 'label': 'Telegram', 'tool': 'telegram_bot', 'ready': False, 'recipients': []}
    if not can_share(user, 'telegram'):
        return {**detail, 'status': 'Your account does not have permission to share through Telegram.'}
    config = configuration(user)
    if not config:
        return {**detail, 'status': 'Ask an administrator to configure your private Telegram chat.'}
    return {**detail, 'ready': True, 'recipients': [config[2]],
            'status': 'Private chat configured. Open the bot and tap Start before sharing.'}


def chart_data(report):
    code_column = GROUP_CODES.get(report.get('level'))
    if not code_column:
        return None
    columns = report['columns']
    metric = next((column for column in METRICS if column in columns), None)
    if not metric:
        return None
    entries = []
    for row in report['rows'][:8]:
        code = str(row.get(code_column, ''))
        value = str(row.get(metric, ''))
        if re.fullmatch(r'[0-9]{1,20}', code) and re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,6})?', value):
            entries.append((code, value))
    return (report['level'], metric, entries) if entries else None


def preview(user, result, recipient):
    config = configuration(user)
    if not can_share(user, 'telegram') or not config or config[2] != recipient:
        raise ValueError('Your private Telegram chat is unavailable.')
    run = ReportRun.objects.filter(user=user, pk=result.get('report_run_id')).first()
    if not run or not _can_replay_result(user, run.source):
        raise ValueError('This report is unavailable. Run the report again before sharing.')
    saved = cache.get('agentic:summary:' + run.snapshot_token)
    chart = chart_data(saved['report']) if saved and saved['user_id'] == user.pk else None
    text = 'BURO Insight AI\nআপনার প্রতিবেদন প্রস্তুত। সম্পূর্ণ প্রতিবেদন দেখতে BURO-তে সাইন ইন করুন।'
    if chart:
        level, metric, entries = chart
        group = 'শাখা' if level == 'BRANCH' else 'জোন'
        text += f'\n\n{group} অনুযায়ী {METRICS[metric][0]} (চার্টে দেখানো সারি):'
        text += ''.join(f'\n{code}: {value}' for code, value in entries)
    url = config[1] + reverse('agentic_ai:report_run', args=[run.pk])
    return text, url, chart


def chart_image(chart):
    level, metric, entries = chart
    image = Image.new('RGB', (860, 84 + len(entries) * 43), '#f3f7fb')
    draw = ImageDraw.Draw(image)
    draw.text((30, 22), f'{METRICS[metric][1]} by {level.lower()}', fill='#16304a',
              font=ImageFont.load_default(size=25))
    font = ImageFont.load_default(size=17)
    maximum = max(Decimal(value) for _, value in entries)
    for index, (code, value) in enumerate(entries):
        y = 76 + index * 43
        draw.text((30, y), code, fill='#16304a', font=font)
        if maximum and Decimal(value):
            width = max(1, int(365 * Decimal(value) / maximum))
            draw.rectangle((245, y, 245 + width, y + 21), fill='#16766d')
        draw.text((630, y), value, fill='#16304a', font=font)
    buffer = BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


def bot_request(token, method, **kwargs):
    # HTTPX formats multipart requests. The standard library performs the
    # request without logging the URL, following redirects or using a proxy.
    request = httpx.Request('POST', f'https://api.telegram.org/bot{token}/{method}', **kwargs)
    connection = HTTPSConnection('api.telegram.org', timeout=10)
    try:
        connection.request('POST', request.url.raw_path.decode('ascii'), request.read(), dict(request.headers))
        response = connection.getresponse()
        body = json.loads(response.read(65537))
        if response.status == 200 and isinstance(body, dict) and body.get('ok') is True:
            return body.get('result')
    finally:
        connection.close()
    return None


def dispatch(user, result, recipient, expected):
    fresh = get_user_model().objects.filter(pk=user.pk, is_active=True).first()
    denied = {'ok': False, 'http_status': 403, 'error': 'Sharing access changed. Nothing was sent.'}
    if not fresh:
        return denied
    try:
        payload = preview(fresh, result, recipient)
    except ValueError:
        return denied
    if payload != expected:
        return {'ok': False, 'http_status': 409, 'error': 'Report preview changed. Nothing was sent.'}
    text, url, chart = payload
    image = chart_image(chart) if chart else None
    config = configuration(fresh)
    if not config:
        return denied
    token = config[0]
    markup = {'inline_keyboard': [[{'text': 'Open full report', 'url': url}]]}
    sending = False
    try:
        metadata = bot_request(token, 'getChat', json={'chat_id': recipient})
        if not isinstance(metadata, dict) or metadata.get('type') != 'private' or str(metadata.get('id')) != recipient:
            return {'ok': False, 'error': 'Telegram could not verify this private chat. Open the bot and tap Start.'}
        fresh = get_user_model().objects.filter(pk=user.pk, is_active=True).first()
        try:
            if not fresh or configuration(fresh) != config or preview(fresh, result, recipient) != expected:
                return denied
        except ValueError:
            return denied
        sending = True
        if image:
            message = bot_request(token, 'sendPhoto',
                data={'chat_id': recipient, 'caption': text, 'reply_markup': json.dumps(markup)},
                files={'photo': ('report.png', image, 'image/png')})
        else:
            message = bot_request(token, 'sendMessage', json={'chat_id': recipient, 'text': text,
                'reply_markup': markup, 'link_preview_options': {'is_disabled': True}})
        if isinstance(message, dict) and type(message.get('message_id')) is int:
            return {'ok': True, 'message': 'Telegram accepted the report message.'}
    except (OSError, HTTPException, ValueError):
        pass
    return {'ok': False, 'error': ('Telegram acceptance could not be verified. Check the chat before retrying.'
                                  if sending else 'Telegram chat verification failed. Nothing was sent.')}
