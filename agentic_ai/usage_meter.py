"""Provider-reported token accounting and explicitly configured price estimates."""
import json
import re
from decimal import Decimal, InvalidOperation


def begin_call(config):
    meter = config.get('usage_meter')
    if meter is None:
        return None
    entry = {'provider': config['provider'], 'model': config['model'],
             'input_tokens': None, 'output_tokens': None, 'cached_input_tokens': 0}
    meter.append(entry)
    return entry


def count(value):
    return value if type(value) is int and value >= 0 else None


def finish_call(entry, body):
    if entry is None or not isinstance(body, dict):
        return
    if entry['provider'] == 'ollama':
        inputs, outputs = count(body.get('prompt_eval_count')), count(body.get('eval_count'))
        cached = 0
    else:
        usage = body.get('usage') or {}
        if not isinstance(usage, dict):
            return
        inputs = count(usage.get('prompt_tokens', usage.get('input_tokens')))
        outputs = count(usage.get('completion_tokens', usage.get('output_tokens')))
        details = usage.get('prompt_tokens_details', usage.get('input_tokens_details')) or {}
        cached = count(details.get('cached_tokens', 0)) if isinstance(details, dict) else None
    if inputs is None or outputs is None or cached is None or cached > inputs:
        return
    entry.update(input_tokens=inputs, output_tokens=outputs, cached_input_tokens=cached)


def validate_rate(value):
    if isinstance(value, bool) or len(str(value)) > 64:
        raise ValueError('Invalid token price')
    try:
        rate = Decimal(str(value))
        if not rate.is_finite() or not 0 <= rate <= 1000000:
            raise ValueError('Invalid token price')
        if rate.normalize().as_tuple().exponent < -12:
            raise ValueError('Use at most 12 decimal places for token prices')
        return format(rate.normalize(), 'f')
    except (InvalidOperation, TypeError):
        raise ValueError('Invalid token price') from None


def validate_price(price):
    if not isinstance(price, dict):
        raise ValueError('Invalid price configuration')
    currency = price.get('currency', 'USD')
    if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
        raise ValueError('Use a three-letter uppercase currency code')
    result = {'currency': currency, 'input_per_million': validate_rate(price.get('input_per_million')),
              'output_per_million': validate_rate(price.get('output_per_million'))}
    if price.get('cached_input_per_million') is not None:
        result['cached_input_per_million'] = validate_rate(price['cached_input_per_million'])
    return result


def parse_prices(raw):
    try:
        prices = json.loads(raw or '{}')
        return prices if isinstance(prices, dict) else {}
    except (ValueError, TypeError):
        return {}


def summarize_usage(meter, prices):
    reported = [entry for entry in meter if entry['input_tokens'] is not None]
    totals = {'model_calls': len(meter), 'reported_calls': len(reported),
              'input_tokens': sum(entry['input_tokens'] for entry in reported),
              'output_tokens': sum(entry['output_tokens'] for entry in reported),
              'cached_input_tokens': sum(entry['cached_input_tokens'] for entry in reported),
              'complete': len(meter) == len(reported), 'calls': []}
    totals['total_tokens'] = totals['input_tokens'] + totals['output_tokens']
    amounts, currencies = [], set()
    for entry in meter:
        item = dict(entry)
        price = prices.get(entry['provider'] + ':' + entry['model'])
        try:
            price = validate_price(price)
        except ValueError:
            price = None
        if price is not None and entry['input_tokens'] is not None:
            inputs, cached, outputs = entry['input_tokens'], entry['cached_input_tokens'], entry['output_tokens']
            cost = ((inputs-cached)*Decimal(price['input_per_million'])
                    + cached*Decimal(price.get('cached_input_per_million', price['input_per_million']))
                    + outputs*Decimal(price['output_per_million'])) / Decimal(1000000)
            item.update(estimated_cost=format(cost, 'f'), currency=price['currency'], rates=price)
            amounts.append(cost)
            currencies.add(price['currency'])
        totals['calls'].append(item)
    if not meter:
        totals.update(cost_status='no_model_calls', estimated_cost='0', currency=None)
    elif len(currencies) > 1:
        totals.update(cost_status='mixed_currencies', estimated_cost=None, currency=None)
    elif amounts:
        totals.update(cost_status='estimated' if len(amounts)==len(meter) else 'partial_estimate',
                      estimated_cost=format(sum(amounts), 'f'), currency=next(iter(currencies)))
    else:
        totals.update(cost_status='unpriced', estimated_cost=None, currency=None)
    totals['note'] = ('Token counts come from model responses; missing usage is not estimated. '
        'Costs use administrator-configured per-million-token rates, not a provider invoice. '
        'Cached input uses its configured rate, or the ordinary input rate when no discount is configured. '
        'Infrastructure, NiFi/Oracle, embeddings, other tool-side models, taxes and subscription charges are excluded. '
        'A timeout can leave billable usage unreported.')
    return totals
