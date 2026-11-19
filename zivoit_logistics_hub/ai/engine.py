# -*- coding: utf-8 -*-
"""Logistics AI engine (pure Python, deterministic, explainable).

These models run locally without any API key, learn from the company's own shipment history
(passed in as aggregated statistics) and always return human readable reasons. When an LLM
is configured, the Odoo layer uses it on top of these results (classification of unknown
statuses, structured address repair, customer messaging, natural language search) and falls
back to this engine whenever the LLM is unavailable.
"""
import math
import re
import statistics
from datetime import datetime, timedelta

from .india import SPECIAL_STATES, state_matches, states_for_pincode, valid_pincode

# ------------------------------------------------------------------ address intelligence

GIBBERISH_RE = re.compile(r'(asdf|qwer|zxcv|hjkl|test|xxx|abc\s*abc|na\b|n/a|null|none|dummy)', re.I)
REPEAT_RE = re.compile(r'(.)\1{4,}')
LANDMARK_RE = re.compile(r'\b(near|opp|opposite|behind|beside|next to|landmark|colony|nagar|road|rd|street|st|'
                         r'lane|sector|phase|block|layout|apartment|apts?|society|enclave|vihar|marg|chowk|'
                         r'main|cross|village|mandal|taluk|tehsil|district|dist|post|towers?|complex|plot)\b', re.I)
HOUSE_RE = re.compile(r'(\d|#|\bflat\b|\bh\.?\s?no\b|\bhouse\b|\bdoor\b|\bplot\b|\bshop\b|\bfloor\b)', re.I)
PO_BOX_RE = re.compile(r'\b(p\.?\s?o\.?\s?box|post box)\b', re.I)


def clean_phone(phone, country='IN'):
    digits = re.sub(r'\D', '', phone or '')
    if country == 'IN':
        if len(digits) == 12 and digits.startswith('91'):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith('0'):
            digits = digits[1:]
    return digits


def phone_is_valid(phone, country='IN'):
    digits = clean_phone(phone, country)
    if country != 'IN':
        return 7 <= len(digits) <= 15
    return bool(re.match(r'^[6-9]\d{9}$', digits)) and len(set(digits)) > 2 and digits not in ('9876543210', '9999999999')


def assess_address(addr):
    """Return {'score': int, 'issues': [(severity, message)], 'state_hint': set}.

    ``addr`` keys: name, street, street2, city, state_code, zip, phone, email, country_code.
    """
    issues, score = [], 100
    country = (addr.get('country_code') or 'IN').upper()
    street = ' '.join(x for x in (addr.get('street'), addr.get('street2')) if x).strip()
    zip_code = str(addr.get('zip') or '').strip()

    def hit(points, severity, message):
        nonlocal score
        score -= points
        issues.append((severity, message))

    if not street:
        hit(40, 'critical', 'Street address is missing.')
    else:
        if len(street) < 12:
            hit(20, 'warning', 'Street address is very short; couriers often fail to locate such addresses.')
        if not HOUSE_RE.search(street):
            hit(10, 'warning', 'No house / flat / plot number found.')
        if not LANDMARK_RE.search(street):
            hit(5, 'info', 'Add a landmark or locality (e.g. "near", "opp", colony/nagar) to improve first-attempt delivery.')
        if GIBBERISH_RE.search(street) or REPEAT_RE.search(street):
            hit(25, 'critical', 'Address looks like placeholder or junk text.')
        if PO_BOX_RE.search(street):
            hit(15, 'warning', 'Couriers do not deliver to PO boxes.')
    if country == 'IN':
        if not valid_pincode(zip_code):
            hit(35, 'critical', 'Invalid Indian PIN code "%s".' % zip_code)
        else:
            match = state_matches(zip_code, addr.get('state_code'))
            if match is False:
                hint = ', '.join(sorted(states_for_pincode(zip_code)))
                hit(20, 'warning', 'PIN code %s does not belong to the selected state (expected: %s).' % (zip_code, hint))
            elif not addr.get('state_code'):
                hit(5, 'info', 'State is missing.')
    elif not zip_code:
        hit(20, 'warning', 'Postal code is missing.')
    if not (addr.get('city') or '').strip():
        hit(10, 'warning', 'City is missing.')
    if not addr.get('phone'):
        hit(30, 'critical', 'Mobile number is missing; couriers need it for delivery and OTP.')
    elif not phone_is_valid(addr.get('phone'), country):
        hit(25, 'critical', 'Mobile number "%s" is not a valid %s mobile.' % (addr.get('phone'), 'Indian' if country == 'IN' else ''))
    name = (addr.get('name') or '').strip()
    if len(name) < 3 or GIBBERISH_RE.fullmatch(name or 'x'):
        hit(10, 'warning', 'Consignee name looks incomplete.')
    if not addr.get('email'):
        hit(0, 'info', 'No email: tracking notifications will be limited to SMS/WhatsApp.')
    return {'score': max(0, min(100, score)), 'issues': issues, 'state_hint': states_for_pincode(zip_code)}


# ------------------------------------------------------------------ RTO / NDR risk model

def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def rto_risk(f):
    """Interpretable logistic model for Return-To-Origin risk.

    ``f`` keys: is_cod, order_value, address_score, phone_valid, customer_delivered, customer_rto,
    pincode_rto_rate, pincode_samples, dest_state, order_hour, has_email.
    Returns {'score', 'level', 'reasons'}.
    """
    contributions = []

    def add(weight, reason):
        if abs(weight) > 1e-9:
            contributions.append((weight, reason))

    is_cod = bool(f.get('is_cod'))
    add(1.25 if is_cod else -0.4, 'Cash on Delivery order' if is_cod else 'Prepaid order')
    rto = int(f.get('customer_rto') or 0)
    delivered = int(f.get('customer_delivered') or 0)
    if rto:
        add(0.9 + 0.45 * min(rto, 4), 'Customer has %d previous RTO shipment(s)' % rto)
    if delivered:
        add(-0.3 * min(delivered, 6), 'Customer accepted %d previous shipment(s)' % delivered)
    elif is_cod:
        add(0.35, 'First COD order from this customer')
    samples = int(f.get('pincode_samples') or 0)
    if samples:
        # Bayesian smoothing towards Indian e-commerce average RTO (~12 %)
        smoothed = (float(f.get('pincode_rto_rate') or 0.0) * samples + 0.12 * 15) / (samples + 15)
        delta = smoothed - 0.12
        add(delta * 7.0, 'Destination PIN code RTO rate %.0f%% over %d shipments' % (100 * f.get('pincode_rto_rate', 0), samples))
    addr_score = float(f.get('address_score', 80))
    if addr_score < 85:
        add((85 - addr_score) * 0.03, 'Address quality score %d/100' % addr_score)
    if not f.get('phone_valid', True):
        add(0.8, 'Invalid or unreachable mobile number')
    value = float(f.get('order_value') or 0.0)
    if is_cod and value > 5000:
        add(0.35 + min((value - 5000) / 20000.0, 0.5), 'High COD value (%.0f)' % value)
    if f.get('dest_state') in SPECIAL_STATES:
        add(0.3, 'Remote / special zone destination')
    hour = f.get('order_hour')
    if hour is not None and 0 <= int(hour) < 5:
        add(0.25, 'Order placed between midnight and 5 AM')
    if not f.get('has_email', True):
        add(0.1, 'No email address on the order')
    z = -2.15 + sum(w for w, _r in contributions)
    score = int(round(_sigmoid(z) * 100))
    level = 'high' if score >= 60 else 'medium' if score >= 35 else 'low'
    reasons = [r for w, r in sorted(contributions, key=lambda c: -abs(c[0])) if w > 0][:5]
    return {'score': score, 'level': level, 'reasons': reasons}


# ------------------------------------------------------------------ carrier recommendation

STRATEGY_WEIGHTS = {
    'cheapest': (1.0, 0.0, 0.0),
    'fastest': (0.0, 1.0, 0.0),
    'reliable': (0.0, 0.0, 1.0),
    'ai_best': (0.45, 0.25, 0.30),
    'ai_best_cod': (0.30, 0.20, 0.50),
    'cheapest_on_time': (1.0, 0.0, 0.0),
}


def reliability(perf):
    """Bayesian reliability 0..1 from historical lane performance."""
    perf = perf or {}
    n = int(perf.get('samples') or 0)
    delivered = float(perf.get('delivered_rate') or 0.0) * n
    rto = float(perf.get('rto_rate') or 0.0) * n
    prior_n = 10.0
    delivered_rate = (delivered + 0.88 * prior_n) / (n + prior_n)
    rto_rate = (rto + 0.08 * prior_n) / (n + prior_n)
    delay = float(perf.get('avg_delay_days') or 0.0) * n / (n + prior_n)
    value = delivered_rate - 0.6 * rto_rate - 0.04 * max(delay, 0.0)
    return max(0.0, min(1.0, value))


def score_rates(options, strategy='ai_best', is_cod=False, deliver_within_days=0):
    """options: list of dicts with key, price, tat_days, perf. Returns the same dicts enriched with
    ``ai_score`` (0-100), ``rank`` and ``reasons``; sorted best first.

    ``deliver_within_days``: delivery promise. Options that arrive in time always rank above late ones
    (whatever the strategy); with ``cheapest_on_time`` the cheapest on-time option wins."""
    if not options:
        return []
    if strategy == 'ai_best' and is_cod:
        strategy = 'ai_best_cod'
    wp, ws, wr = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS['ai_best'])
    prices = [o['price'] for o in options if o.get('price') is not None]
    known_tats = [o['tat_days'] for o in options if o.get('tat_days')]
    default_tat = statistics.median(known_tats) if known_tats else 4.0
    pmin, pmax = (min(prices), max(prices)) if prices else (0, 0)
    tats = [o.get('tat_days') or default_tat for o in options]
    tmin, tmax = min(tats), max(tats)
    for o, tat in zip(options, tats):
        # ratio scoring: a carrier 10 % dearer loses ~10 % of the price score (min-max would exaggerate small gaps)
        price_s = 1.0 if not o['price'] or pmin <= 0 else max(pmin, 0.01) / max(o['price'], 0.01)
        speed_s = 1.0 if tmax == tmin else max(tmin, 0.5) / max(tat, 0.5)
        rel = reliability(o.get('perf'))
        o['reliability'] = round(rel * 100, 1)
        o['ai_score'] = round(100 * (wp * price_s + ws * speed_s + wr * rel), 1)
        reasons = []
        if o['price'] == pmin:
            reasons.append('lowest price')
        if tat == tmin:
            reasons.append('fastest (%g day%s)' % (tat, '' if tat == 1 else 's'))
        perf = o.get('perf') or {}
        if perf.get('samples'):
            reasons.append('%.0f%% delivered / %.0f%% RTO on this lane (%d shipments)' % (
                100 * perf.get('delivered_rate', 0), 100 * perf.get('rto_rate', 0), perf['samples']))
        else:
            reasons.append('no history on this lane yet')
        o['reasons'] = reasons
    for o, tat in zip(options, tats):
        o['on_time'] = True
        if deliver_within_days:
            o['on_time'] = tat <= deliver_within_days
            o['reasons'].insert(0, 'arrives within %g day%s' % (deliver_within_days, '' if deliver_within_days == 1 else 's')
                                if o['on_time'] else 'misses the delivery date by %g day%s' % (
                                    tat - deliver_within_days, '' if tat - deliver_within_days == 1 else 's'))
    ranked = sorted(options, key=lambda x: (not x['on_time'], -x['ai_score'], x['price'], x.get('tat_days') or default_tat))
    for i, o in enumerate(ranked, 1):
        o['rank'] = i
    return ranked


def apply_preference(ranked, preferred_ids, margin_amount=0.0, margin_pct=0.0):
    """ShipStation style service preference: promote the best option of a preferred carrier account when it costs
    at most ``margin_amount`` (or ``margin_pct`` %) more than the winner and is not later than the delivery promise.
    ``ranked`` items carry ``account_id`` (or ``key[0]``). Returns a new ranked list."""
    if not ranked or not preferred_ids:
        return ranked
    best = ranked[0]

    def acc(o):
        return o.get('account_id') or (o.get('key') or (None,))[0]
    if acc(best) in preferred_ids:
        return ranked
    allowed = max(float(margin_amount or 0.0), best['price'] * float(margin_pct or 0.0) / 100.0)
    for o in ranked[1:]:
        if acc(o) in preferred_ids and o['price'] <= best['price'] + allowed + 1e-6 and (o.get('on_time', True) or not best.get('on_time', True)):
            extra = o['price'] - best['price']
            o.setdefault('reasons', []).insert(0, 'preferred carrier (%s)' % (
                'same price' if abs(extra) < 0.005 else '%.2f more than the best option' % extra if extra > 0
                else '%.2f cheaper' % -extra))
            ranked = [o] + [x for x in ranked if x is not o]
            for i, x in enumerate(ranked, 1):
                x['rank'] = i
            return ranked
    return ranked


def savings(ranked, chosen=None):
    """Money saved by the chosen option against the alternatives quoted at the same time."""
    prices = [o['price'] for o in ranked or [] if o.get('price')]
    if not prices:
        return {'max_price': 0.0, 'avg_price': 0.0, 'min_price': 0.0, 'saved_vs_max': 0.0, 'saved_vs_avg': 0.0,
                'saved_pct': 0.0, 'options': 0}
    chosen_price = (chosen or ranked[0]).get('price') or 0.0
    max_price, avg_price = max(prices), sum(prices) / len(prices)
    saved = max(max_price - chosen_price, 0.0)
    return {'max_price': round(max_price, 2), 'avg_price': round(avg_price, 2), 'min_price': round(min(prices), 2),
            'saved_vs_max': round(saved, 2), 'saved_vs_avg': round(max(avg_price - chosen_price, 0.0), 2),
            'saved_pct': round(100.0 * saved / max_price, 1) if max_price else 0.0, 'options': len(prices)}


# ------------------------------------------------------------------ packaging & insurance

def suggest_box(volume_cm3, longest_cm, weight_kg, boxes, fill_factor=1.15, divisor=5000.0):
    """Pick the box that fits the goods with the lowest chargeable weight.

    boxes: list of dicts {id, name, length, width, height, max_weight, base_weight} (cm / kg).
    Returns dict {box, chargeable_kg, fill_pct} or None when nothing fits."""
    candidates = []
    for box in boxes or []:
        dims = sorted([box.get('length') or 0.0, box.get('width') or 0.0, box.get('height') or 0.0])
        if not all(dims):
            continue
        box_volume = dims[0] * dims[1] * dims[2]
        if box_volume < (volume_cm3 or 0.0) * fill_factor or dims[-1] < (longest_cm or 0.0):
            continue
        gross = (weight_kg or 0.0) + (box.get('base_weight') or 0.0)
        if box.get('max_weight') and gross > box['max_weight']:
            continue
        chargeable = max(gross, box_volume / (divisor or 5000.0))
        candidates.append((round(chargeable, 3), box_volume, box))
    if not candidates:
        return None
    chargeable, box_volume, box = min(candidates, key=lambda c: (c[0], c[1]))
    return {'box': box, 'chargeable_kg': chargeable,
            'fill_pct': round(100.0 * (volume_cm3 or 0.0) / box_volume, 1) if box_volume else 0.0}


def insurance_premium(value, rate_pct=0.0, minimum=0.0):
    if not value or value <= 0:
        return 0.0
    return round(max(value * (rate_pct or 0.0) / 100.0, minimum or 0.0), 2)


# ------------------------------------------------------------------ status classification

STATUS_RULES = [
    ('rto_delivered', r'\b(rto|return(ed)?)\b.*\b(delivered|received by (shipper|seller|origin))|returned to (shipper|origin|seller)|rto[ -]?dl'),
    ('rto_in_transit', r'\brto\b.*\b(transit|dispatched|in-?transit|ofd|out for delivery)'),
    ('rto_initiated', r'\b(rto|return to origin|marked for return|return initiated)\b'),
    ('cancelled', r'\bcancel+ed\b|\bcancel+ation\b'),
    ('lost', r'\b(lost|damaged|destroyed|shortage|missing)\b'),
    ('ndr', r'not delivered|undeliver|\bndr\b|refused|door locked|premises closed|consignee (not available|unavailable)|'
            r'customer (not available|unavailable|not reachable)|address (incomplete|incorrect|not found)|cod not ready|'
            r'reattempt|re-attempt|delivery (failed|attempted)|future delivery|rescheduled'),
    ('out_for_delivery', r'out for delivery|\bofd\b|(with|to) delivery (agent|executive|boy|associate)|handed over to (delivery|fe\b)|on vehicle for delivery'),
    ('delivered', r'\bdelivered\b|\bdlv?d?\b|proof of delivery'),
    ('picked_up', r'picked up|pickup (done|completed|successful)|shipment picked|received at origin'),
    ('pickup_scheduled', r'pickup (scheduled|pending|generated|queued|awaited)|out for pickup|not picked'),
    ('in_transit', r'transit|\bhub\b|reached|dispatched|arrived|connected|bagged|in-?scan|out-?scan|received at|departed|forwarded|shipped'),
    ('booked', r'manifest|booked|created|awb (assigned|generated)|label (generated|created)|soft ?data|order placed|^new$'),
    ('exception', r'exception|hold|misroute|delay|weather|strike|contraband|seized'),
]


def classify_status(text):
    text = (text or '').strip().lower()
    if not text:
        return None, 0.0
    for status, pattern in STATUS_RULES:
        if re.search(pattern, text):
            # "delivered" words are only conclusive when not negated
            return status, 0.75 if len(text) > 6 else 0.6
    return 'exception', 0.3


# ------------------------------------------------------------------ NDR assistant

NDR_RULES = [
    ('refused', r'refus|reject|denied|not interested|cancel(led)? by customer|does not want', 'rto',
     'Customer refused the shipment. Confirm intent once; if refused again, initiate RTO to save re-attempt cost.'),
    ('cod_not_ready', r'cod (amount )?not ready|cash not (ready|available)|no cash|payment not ready', 'reattempt',
     'Customer did not have cash ready. Share UPI / payment link or reschedule delivery.'),
    ('address_issue', r'address|incomplete|wrong|incorrect|not found|unable to locate|oda|out of delivery area|landmark', 'change_address',
     'Address could not be located. Collect a complete address with landmark and update it with the courier.'),
    ('phone_unreachable', r'phone|unreachable|not reachable|switched off|no response|not answering|call', 'contact_customer',
     'Customer could not be reached by phone. Contact on alternate number / WhatsApp before re-attempt.'),
    ('rescheduled', r'resched|future delivery|asked to deliver later|next day|deliver on', 'reattempt',
     'Customer requested delivery on another date. Schedule the re-attempt for the requested date.'),
    ('customer_unavailable', r'not available|unavailable|door locked|premises closed|nobody|no one|out of station', 'reattempt',
     'Customer was not available. Confirm a convenient time slot and request a re-attempt.'),
]

NDR_LABELS = {
    'refused': 'Refused', 'cod_not_ready': 'COD not ready', 'address_issue': 'Address issue',
    'phone_unreachable': 'Phone unreachable', 'rescheduled': 'Rescheduled',
    'customer_unavailable': 'Not available', 'other': 'Other',
}

CUSTOMER_TEMPLATES = {
    'refused': 'Hi {name}, our courier tried to deliver your order {ref} today but it was not accepted. '
               'If this was a mistake, reply to this message and we will re-attempt delivery.',
    'cod_not_ready': 'Hi {name}, our courier tried to deliver your order {ref} (amount due: {amount}). '
                     'Please keep the amount ready or reply to receive a UPI payment link; we will re-attempt delivery.',
    'address_issue': 'Hi {name}, our courier could not locate your address for order {ref}. '
                     'Please reply with your complete address, a nearby landmark and an alternate phone number.',
    'phone_unreachable': 'Hi {name}, our courier could not reach you on your phone for order {ref}. '
                         'Please reply with a convenient time and a reachable number.',
    'rescheduled': 'Hi {name}, as requested, delivery of your order {ref} will be re-attempted. '
                   'Reply with your preferred date if it has changed.',
    'customer_unavailable': 'Hi {name}, we missed you today while delivering order {ref}. '
                            'Please reply with a convenient time slot so we can re-attempt delivery.',
    'other': 'Hi {name}, there was a problem delivering your order {ref}. Please reply so we can help.',
}


def classify_ndr(reason):
    text = (reason or '').lower()
    for category, pattern, action, advice in NDR_RULES:
        if re.search(pattern, text):
            return {'category': category, 'action': action, 'advice': advice, 'confidence': 0.8}
    return {'category': 'other', 'action': 'contact_customer',
            'advice': 'Unclassified delivery failure. Contact the customer before deciding to re-attempt or RTO.',
            'confidence': 0.3}


def customer_message(category, name, ref, amount=''):
    template = CUSTOMER_TEMPLATES.get(category) or CUSTOMER_TEMPLATES['other']
    return template.format(name=(name or 'there').split(' ')[0], ref=ref, amount=amount or '-')


# ------------------------------------------------------------------ EDD prediction

def predict_tat(history_days, carrier_tat=0.0, zone='national'):
    """Return (predicted transit days, confidence 0..1, basis)."""
    zone_default = {'local': 1.5, 'regional': 2.5, 'metro': 3.0, 'national': 4.5, 'special': 7.0}.get(zone, 4.5)
    history = [d for d in history_days or [] if d is not None and 0 <= d < 60]
    if len(history) >= 5:
        history = sorted(history)
        # 75th percentile: a promise we keep 3 times out of 4
        p75 = history[min(len(history) - 1, int(math.ceil(0.75 * len(history))) - 1)]
        conf = min(0.95, 0.5 + len(history) / 100.0)
        return round(p75, 1), conf, 'p75 of %d delivered shipments on this lane' % len(history)
    if carrier_tat:
        return float(carrier_tat), 0.5, 'carrier promised TAT'
    return zone_default, 0.3, '%s zone default' % zone


def add_business_days(start, days, holidays=()):
    current, remaining = start, math.ceil(days)
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() != 6 and current.date() not in holidays:  # couriers deliver Mon-Sat
            remaining -= 1
    return current


# ------------------------------------------------------------------ weight reconciliation

def weight_dispute(declared_kg, charged_kg, slab=0.5, tolerance=0.1):
    """True when the courier billed a higher weight slab than declared (beyond tolerance)."""
    if not charged_kg or not declared_kg:
        return False
    declared_slab = math.ceil(round(declared_kg / slab, 6)) * slab
    return charged_kg > declared_slab * (1 + tolerance) and charged_kg - declared_slab >= slab - 1e-6


# ------------------------------------------------------------------ natural language search (fallback)

NL_STATUS_WORDS = {
    'delivered': ['delivered'], 'ndr': ['ndr', 'failed', 'undelivered', 'not delivered'],
    'rto': ['rto', 'returned', 'return to origin'], 'in_transit': ['in transit', 'on the way', 'moving'],
    'out_for_delivery': ['out for delivery', 'ofd'], 'cancelled': ['cancelled', 'canceled'],
    'booked': ['booked', 'not picked', 'pending pickup'],
}


def parse_nl_query(question, carrier_names=(), now=None):
    """Keyword parser -> filter dict (used when no LLM is configured)."""
    q = (question or '').lower()
    now = now or datetime.now()
    out = {'states': [], 'carriers': [], 'cod': None, 'delayed': False, 'high_risk': False,
           'weight_dispute': False, 'date_from': None, 'city': None}
    for status, words in NL_STATUS_WORDS.items():
        if any(re.search(r'\b%s\b' % re.escape(w), q) for w in words):
            if status == 'rto':
                out['states'] += ['rto_initiated', 'rto_in_transit', 'rto_delivered']
            elif status == 'delivered' and re.search(r'\b(not|un)\s*delivered', q):
                continue
            else:
                out['states'].append(status)
    if re.search(r'\b(delay|delayed|late|overdue|breach)', q):
        out['delayed'] = True
    if re.search(r'\bcod\b|cash on delivery', q):
        out['cod'] = True
    elif 'prepaid' in q:
        out['cod'] = False
    if re.search(r'high.?risk|risky|fraud', q):
        out['high_risk'] = True
    if re.search(r'weight|overcharg|dispute', q):
        out['weight_dispute'] = True
    for name in carrier_names:
        if name and name.lower() in q:
            out['carriers'].append(name)
    if 'today' in q:
        out['date_from'] = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif 'yesterday' in q:
        out['date_from'] = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif re.search(r'this week|last 7 days|past week', q):
        out['date_from'] = now - timedelta(days=7)
    elif re.search(r'this month|last 30 days|past month', q):
        out['date_from'] = now - timedelta(days=30)
    m = re.search(r'\b(?:to|in|for|from)\s+([a-z][a-z ]{2,30}?)(?:\s+(?:this|last|today|yesterday|with|via|by|that|which)\b|[?.!,]|$)', q)
    if m:
        city = m.group(1).strip()
        stop = {'transit', 'delivery', 'the', 'cod', 'rto', 'ndr', 'week', 'month', 'today', 'risk'}
        if city.split()[0] not in stop and not any(city == n.lower() for n in carrier_names):
            out['city'] = city
    out['states'] = list(dict.fromkeys(out['states']))
    return out
