# -*- coding: utf-8 -*-
"""Pure-python helpers for tender parsing.

Kept free of ORM dependencies so they can be unit-tested in isolation and
reused by every connector (RSS, HTML, JSON, e-mail, AI web search).
"""
import hashlib
import re
from datetime import datetime, timedelta

LAKH = 100000.0
CRORE = 10000000.0

# ---------------------------------------------------------------------------
# Reference dictionaries
# ---------------------------------------------------------------------------
RAILWAY_ZONES = {
    'SCR': ('South Central Railway', ['south central railway', 'secunderabad', 's.c.railway', 'sc railway', 'scr']),
    'SR': ('Southern Railway', ['southern railway']),
    'CR': ('Central Railway', ['central railway']),
    'WR': ('Western Railway', ['western railway']),
    'ER': ('Eastern Railway', ['eastern railway']),
    'NR': ('Northern Railway', ['northern railway']),
    'NER': ('North Eastern Railway', ['north eastern railway']),
    'NFR': ('Northeast Frontier Railway', ['northeast frontier railway', 'n.f.railway']),
    'NWR': ('North Western Railway', ['north western railway']),
    'NCR': ('North Central Railway', ['north central railway']),
    'ECR': ('East Central Railway', ['east central railway']),
    'ECOR': ('East Coast Railway', ['east coast railway']),
    'SER': ('South Eastern Railway', ['south eastern railway']),
    'SECR': ('South East Central Railway', ['south east central railway']),
    'SWR': ('South Western Railway', ['south western railway', 'hubballi']),
    'WCR': ('West Central Railway', ['west central railway']),
    'SCOR': ('South Coast Railway', ['south coast railway', 'visakhapatnam zone']),
    'KR': ('Konkan Railway', ['konkan railway', 'krcl']),
    'MTP': ('Metro Railway Kolkata', ['metro railway kolkata']),
    'RVNL': ('Rail Vikas Nigam Ltd', ['rail vikas nigam', 'rvnl']),
    'IRCON': ('IRCON International', ['ircon']),
    'RITES': ('RITES Ltd', ['rites ltd', 'rites limited']),
    'DFCCIL': ('Dedicated Freight Corridor', ['dfccil', 'dedicated freight corridor']),
}

# Odoo state codes for India (base data uses these codes)
INDIAN_STATES = {
    'Andhra Pradesh': ['andhra pradesh', 'andhra', ' ap ', 'appwd', 'amaravati', 'vijayawada', 'visakhapatnam', 'vizag'],
    'Telangana': ['telangana', 'telengana', 'hyderabad', 'secunderabad', 'ghmc', 'hmda', 'warangal', ' ts ', 'tgr&b', 'ts r&b'],
    'Karnataka': ['karnataka', 'bengaluru', 'bangalore', 'bbmp', 'mysuru'],
    'Tamil Nadu': ['tamil nadu', 'chennai', 'tnpwd', 'coimbatore'],
    'Kerala': ['kerala', 'thiruvananthapuram', 'kochi'],
    'Maharashtra': ['maharashtra', 'mumbai', 'pune', 'nagpur', 'mmrda', 'pwd maharashtra'],
    'Gujarat': ['gujarat', 'ahmedabad', 'surat', 'vadodara'],
    'Rajasthan': ['rajasthan', 'jaipur', 'jodhpur'],
    'Madhya Pradesh': ['madhya pradesh', 'bhopal', 'indore'],
    'Uttar Pradesh': ['uttar pradesh', 'lucknow', 'noida', 'kanpur', 'varanasi'],
    'Bihar': ['bihar', 'patna'],
    'West Bengal': ['west bengal', 'kolkata'],
    'Odisha': ['odisha', 'orissa', 'bhubaneswar'],
    'Chhattisgarh': ['chhattisgarh', 'raipur'],
    'Jharkhand': ['jharkhand', 'ranchi'],
    'Punjab': ['punjab', 'ludhiana', 'amritsar'],
    'Haryana': ['haryana', 'gurugram', 'gurgaon', 'faridabad'],
    'Delhi': ['delhi', 'new delhi', 'dda', 'cpwd delhi'],
    'Assam': ['assam', 'guwahati'],
    'Uttarakhand': ['uttarakhand', 'dehradun'],
    'Himachal Pradesh': ['himachal pradesh', 'shimla'],
    'Goa': ['goa', 'panaji'],
}

STATE_ABBREVIATIONS = {
    'ts': 'Telangana', 'tg': 'Telangana', 'ap': 'Andhra Pradesh', 'tn': 'Tamil Nadu',
    'ka': 'Karnataka', 'kl': 'Kerala', 'mh': 'Maharashtra', 'gj': 'Gujarat', 'rj': 'Rajasthan',
    'mp': 'Madhya Pradesh', 'up': 'Uttar Pradesh', 'wb': 'West Bengal', 'od': 'Odisha',
    'cg': 'Chhattisgarh', 'jh': 'Jharkhand', 'pb': 'Punjab', 'hr': 'Haryana', 'dl': 'Delhi',
}

# Default sector synonyms (also stored on zivoit.tender.sector, editable by users)
SECTOR_KEYWORDS = {
    'roads': ['road', 'roads', 'highway', 'highways', 'nh-', 'national highway', 'state highway', 'widening',
              'carriageway', 'bt road', 'cc road', 'pavement', 'resurfacing', 'four lane', 'two lane',
              '4-lane', '2-lane', 'r&b', 'nhai', 'morth', 'pmgsy', 'black topping', 'bituminous'],
    'buildings': ['building', 'buildings', 'construction of quarters', 'quarters', 'hostel', 'school building',
                  'hospital building', 'office building', 'auditorium', 'residential', 'complex', 'g+',
                  'staff quarters', 'housing', 'dwelling', 'civil works building', 'cpwd'],
    'bridges': ['bridge', 'bridges', 'rob', 'rub', 'flyover', 'fly over', 'viaduct', 'culvert',
                'road over bridge', 'road under bridge', 'limited height subway', 'lhs'],
    'railways': ['railway', 'railways', 'rail', 'track', 'doubling', 'third line', 'new line',
                 'gauge conversion', 'station building', 'platform', 'foot over bridge', 'fob',
                 'ballast', 'sleeper', 'yard remodelling', 'ireps', 'earthwork in formation'],
    'water': ['water supply', 'sewerage', 'sewer', 'drainage', 'storm water', 'pipeline', 'stp', 'wtp',
              'reservoir', 'overhead tank', 'jal jeevan', 'amrut'],
    'irrigation': ['irrigation', 'canal', 'dam', 'barrage', 'lift irrigation', 'check dam', 'tank bund'],
    'electrical': ['electrical', 'electrification', 'ohe', 'substation', 'transmission line', 'lighting',
                   'solar', 'traction'],
    'metro': ['metro', 'mrts', 'rapid transit'],
    'real_estate': ['real estate', 'township', 'apartment', 'villas', 'commercial complex', 'mall',
                    'land development', 'layout development', 'plotted development'],
    'maintenance': ['maintenance', 'repairs', 'special repairs', 'annual maintenance', 'amc', 'renovation',
                    'painting', 'patch work'],
}

GENERIC_WORDS = {
    'tender', 'tenders', 'bid', 'bids', 'bidding', 'open', 'opened', 'active', 'live', 'new', 'latest',
    'hot', 'current', 'in', 'for', 'of', 'the', 'and', 'or', 'a', 'an', 'at', 'on', 'to', 'with', 'from',
    'construction', 'constructions', 'works', 'work', 'civil', 'contract', 'contracts', 'projects',
    'project', 'all', 'any', 'show', 'me', 'find', 'list', 'get', 'search', 'please', 'today', 'this',
    'week', 'month', 'state', 'zone', 'division', 'department', 'govt', 'government', 'public',
    'value', 'worth', 'crore', 'crores', 'cr', 'lakh', 'lakhs', 'rs', 'inr', 'rupees', 'above', 'below',
    'over', 'under', 'more', 'less', 'than', 'between', 'upto', 'up', 'closing', 'within', 'days',
    'day', 'next', 'e-tender', 'etender', 'e-procurement', 'nit', 'rfp', 'rfq', 'eoi', 'notice',
    'inviting', 'related', 'industry', 'sector', 'type', 'like', 'which', 'are', 'is', 'that',
}

WORK_TYPE_PATTERNS = [
    ('epc', r'\bepc\b|engineering,? procurement'),
    ('ham', r'\bham\b|hybrid annuity'),
    ('bot', r'\bbot\b|build,? operate'),
    ('lump_sum', r'lump[\s-]?sum'),
    ('percentage', r'percentage rate'),
    ('item_rate', r'item rate'),
    ('supply', r'\bsupply of\b'),
    ('consultancy', r'consultan|\bdpr\b|supervision'),
]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def clean_text(value):
    if value is None or value is False:
        return ''
    value = re.sub(r'<[^>]+>', ' ', str(value))
    value = value.replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', value).strip()


def normalize(value):
    return re.sub(r'[^a-z0-9]+', ' ', clean_text(value).lower()).strip()


def fingerprint(reference, title, organisation=''):
    """Stable hash used to detect the same tender coming from several sources."""
    ref = normalize(reference)
    if ref and len(ref) >= 5:
        base = 'ref:' + ref
    else:
        base = 'txt:' + normalize(title)[:180] + '|' + normalize(organisation)[:80]
    return hashlib.sha1(base.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Amount / date / duration parsing
# ---------------------------------------------------------------------------
_AMOUNT_RE = re.compile(
    r'(?:(?:rs\.?|inr|₹)\s*)?'
    r'(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)'
    r'\s*(?P<unit>crores?|cr\.?(?![a-z])|lakhs?|lacs?|lac(?![a-z])|l(?![a-z])|million|mn(?![a-z])|billion|bn(?![a-z])|thousand|k(?![a-z]))?',
    re.IGNORECASE)


def parse_indian_amount(text):
    """Return an INR amount from strings like 'Rs. 12.5 Crore', '₹ 3,45,00,000', '45 Lakhs'."""
    if text is None or text is False:
        return 0.0
    if isinstance(text, (int, float)):
        return float(text)
    text = clean_text(text)
    if not text:
        return 0.0
    bare = re.fullmatch(r'(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:/-)?', text, re.I)
    if bare:
        # a field that holds only a number (structured connectors, spreadsheets) is the amount itself
        return float(bare.group(1).replace(',', ''))
    best = 0.0
    for match in _AMOUNT_RE.finditer(text):
        num = match.group('num').replace(',', '')
        try:
            value = float(num)
        except ValueError:
            continue
        unit = (match.group('unit') or '').lower().rstrip('.')
        has_currency = bool(re.match(r'(rs\.?|inr|₹)', match.group(0).strip(), re.I))
        if unit.startswith('cr'):
            value *= CRORE
        elif unit.startswith('la') or unit == 'l':
            value *= LAKH
        elif unit in ('million', 'mn'):
            value *= 1000000.0
        elif unit in ('billion', 'bn'):
            value *= 1000000000.0
        elif unit in ('thousand', 'k'):
            value *= 1000.0
        elif not has_currency and value < 100000 and ',' not in match.group('num'):
            # A bare number (year, tender no.) with no unit and no currency is not an amount
            continue
        best = max(best, value)
    return best


_DATE_FORMATS = [
    '%d-%b-%Y %I:%M %p', '%d-%b-%Y %H:%M', '%d-%b-%Y', '%d-%B-%Y',
    '%d %b %Y %I:%M %p', '%d %b %Y %H:%M', '%d %b %Y', '%d %B %Y', '%d %B, %Y', '%B %d, %Y', '%b %d, %Y',
    '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y %I:%M %p', '%d/%m/%Y',
    '%d.%m.%Y %H:%M', '%d.%m.%Y', '%d-%m-%Y %H:%M', '%d-%m-%Y',
    '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d',
    '%a, %d %b %Y %H:%M:%S', '%a, %d %b %Y %H:%M',
]


def parse_date(text):
    """Parse the many date formats used by Indian e-procurement portals. Returns naive datetime or None."""
    if not text:
        return None
    if isinstance(text, datetime):
        return text.replace(tzinfo=None)
    value = clean_text(text)
    value = re.sub(r'(\d)(st|nd|rd|th)\b', r'\1', value)
    value = re.sub(r'\s*(IST|GMT|UTC|hrs?\.?|hours)\s*$', '', value, flags=re.I).strip()
    value = re.sub(r'([+-]\d{2}:?\d{2}|Z)$', '', value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    # last resort: find a date-like token inside a sentence
    match = re.search(r'(\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]\d{4}(?:\s+\d{1,2}:\d{2}(?:\s*[AP]M)?)?)', value)
    if match and match.group(1) != value:
        return parse_date(match.group(1))
    match = re.search(r'(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)', value)
    if match and match.group(1) != value:
        return parse_date(match.group(1))
    return None


def parse_duration_months(text):
    """'18 Months' -> 18, '540 days' -> 18, '2 years' -> 24, '12' -> 12."""
    if not text:
        return 0.0
    if isinstance(text, (int, float)):
        return float(text)
    value = clean_text(text).lower()
    match = re.search(r'(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|mons?|days?|weeks?)?', value)
    if not match:
        return 0.0
    num = float(match.group(1))
    unit = match.group(2) or 'month'
    if unit.startswith('y'):
        return num * 12
    if unit.startswith('d'):
        return round(num / 30.0, 1)
    if unit.startswith('w'):
        return round(num / 4.345, 1)
    return num


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def detect_zone(text):
    low = ' %s ' % clean_text(text).lower()
    for code, (_name, aliases) in RAILWAY_ZONES.items():
        for alias in aliases:
            if len(alias) <= 4:
                if re.search(r'(?<![a-z])%s(?![a-z])' % re.escape(alias), low):
                    return code
            elif alias in low:
                return code
    return False


def detect_state(text):
    low = ' %s ' % re.sub(r'[^a-z0-9&]+', ' ', clean_text(text).lower()) + ' '
    for state, aliases in INDIAN_STATES.items():
        for alias in aliases:
            if alias in low:
                return state
    return False


def detect_sectors(text, sector_keywords=None):
    sector_keywords = sector_keywords or SECTOR_KEYWORDS
    low = ' %s ' % clean_text(text).lower()
    found = []
    for code, words in sector_keywords.items():
        for word in words:
            word = word.strip().lower()
            if not word:
                continue
            if len(word) <= 4:
                if re.search(r'(?<![a-z])%s(?![a-z])' % re.escape(word), low):
                    found.append(code)
                    break
            elif word in low:
                found.append(code)
                break
    return found


def detect_work_type(text):
    low = clean_text(text).lower()
    for code, pattern in WORK_TYPE_PATTERNS:
        if re.search(pattern, low):
            return code
    return False


# ---------------------------------------------------------------------------
# Natural language query parsing (rule-based; the AI provider can refine it)
# ---------------------------------------------------------------------------
_MONEY = r'(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|lac|l)\b'


def _money(num, unit):
    num = float(num)
    unit = unit.lower()
    return num * (CRORE if unit.startswith('cr') else LAKH)


def parse_query(query, sector_keywords=None):
    """Turn 'open tenders in railways construction in SCR above 5 crore closing in 15 days'
    into structured filters.
    """
    result = {
        'keywords': [], 'exclude_keywords': [], 'states': [], 'zones': [], 'sectors': [],
        'organisations': [], 'min_value': 0.0, 'max_value': 0.0, 'open_only': True,
        'deadline_days': 0, 'published_days': 0,
    }
    if not query:
        return result
    text = ' %s ' % clean_text(query).lower()

    # value ranges
    m = re.search(r'between\s+' + _MONEY + r'\s+(?:and|to|-)\s+' + _MONEY, text)
    if not m:
        m2 = re.search(r'between\s+(\d+(?:\.\d+)?)\s+(?:and|to|-)\s+' + _MONEY, text)
        if m2:
            result['min_value'] = _money(m2.group(1), m2.group(3))
            result['max_value'] = _money(m2.group(2), m2.group(3))
            text = text.replace(m2.group(0), ' ')
    else:
        result['min_value'] = _money(m.group(1), m.group(2))
        result['max_value'] = _money(m.group(3), m.group(4))
        text = text.replace(m.group(0), ' ')
    m = re.search(r'(?:above|over|more than|greater than|minimum|min|>=?)\s*(?:rs\.?|inr|₹)?\s*' + _MONEY, text)
    if m:
        result['min_value'] = _money(m.group(1), m.group(2))
        text = text.replace(m.group(0), ' ')
    m = re.search(r'(?:below|under|less than|upto|up to|maximum|max|<=?)\s*(?:rs\.?|inr|₹)?\s*' + _MONEY, text)
    if m:
        result['max_value'] = _money(m.group(1), m.group(2))
        text = text.replace(m.group(0), ' ')

    # deadline / publication windows
    m = re.search(r'(?:closing|due|ending|deadline|submission)\s+(?:in|within)\s+(\d+)\s+days?', text)
    if m:
        result['deadline_days'] = int(m.group(1))
        text = text.replace(m.group(0), ' ')
    elif re.search(r'closing (?:this|in a) week|due this week', text):
        result['deadline_days'] = 7
    m = re.search(r'(?:published|posted|released|new|latest)\s+(?:in|within)?\s*(?:the\s+)?(?:last|past)\s+(\d+)\s+days?', text)
    if m:
        result['published_days'] = int(m.group(1))
        text = text.replace(m.group(0), ' ')
    elif re.search(r'\b(today|today\'s)\b', text):
        result['published_days'] = 1
    elif re.search(r'\b(new|latest|fresh|hot)\b', text):
        result['published_days'] = 7
    if re.search(r'\b(closed|archived|past|expired|awarded)\b', text):
        result['open_only'] = False

    # exclusions: "excluding maintenance", "not painting", "-repairs"
    for m in re.finditer(r'(?:excluding|except|without|not)\s+([a-z&\- ]+?)(?=,| and | but |$| in | above| below)', text):
        word = m.group(1).strip()
        if word:
            result['exclude_keywords'].append(word)
        text = text.replace(m.group(0), ' ')
    for m in re.finditer(r'\s-([a-z]+)', text):
        result['exclude_keywords'].append(m.group(1))
        text = text.replace(m.group(0), ' ')

    # organisations commonly searched by name
    for org in ['nhai', 'morth', 'cpwd', 'pwd', 'r&b', 'ghmc', 'hmda', 'nbcc', 'rvnl', 'ircon', 'aai',
                'nhidcl', 'pmgsy', 'mes', 'dda', 'ntpc', 'bhel', 'hudco', 'tsiic', 'tgiic', 'apiic']:
        if re.search(r'(?<![a-z])%s(?![a-z])' % re.escape(org), text):
            result['organisations'].append(org.upper())

    # railway zones
    zone = None
    for code, (_name, aliases) in RAILWAY_ZONES.items():
        for alias in aliases:
            pattern = r'(?<![a-z])%s(?![a-z])' % re.escape(alias)
            if re.search(pattern, text):
                if code not in result['zones']:
                    result['zones'].append(code)
                text = re.sub(pattern, ' ', text)
                zone = code
    if zone and 'railways' not in result['sectors']:
        result['sectors'].append('railways')

    # states (full names first, then abbreviations as whole tokens)
    for state, aliases in INDIAN_STATES.items():
        for alias in aliases:
            alias = alias.strip()
            if len(alias) <= 3:
                continue
            if re.search(r'(?<![a-z])%s(?![a-z])' % re.escape(alias), text):
                if state not in result['states']:
                    result['states'].append(state)
                text = re.sub(r'(?<![a-z])%s(?![a-z])' % re.escape(alias), ' ', text)
    for abbr, state in STATE_ABBREVIATIONS.items():
        if re.search(r'(?<![a-z])%s(?![a-z])' % abbr, text) and re.search(
                r'\b(in|at|of)\s+%s\b' % abbr, text):
            if state not in result['states']:
                result['states'].append(state)
            text = re.sub(r'(?<![a-z])%s(?![a-z])' % abbr, ' ', text)

    # sectors
    sector_keywords = sector_keywords or SECTOR_KEYWORDS
    for code in detect_sectors(text, sector_keywords):
        if code not in result['sectors']:
            result['sectors'].append(code)

    # remaining meaningful words become keywords (only if they are not sector/stop words)
    sector_words = set()
    for words in sector_keywords.values():
        for w in words:
            sector_words.update(w.lower().split())
    tokens = re.findall(r'[a-z][a-z0-9&\-]+', text)
    for token in tokens:
        if token in GENERIC_WORDS or token in sector_words or len(token) < 3:
            continue
        if token.upper() in result['organisations']:
            continue
        if token not in result['keywords']:
            result['keywords'].append(token)
    return result


def keyword_list(value):
    """Split a comma / newline separated keyword field into clean lowercase phrases."""
    if not value:
        return []
    parts = re.split(r'[,\n;|]+', value)
    return [p.strip().lower() for p in parts if p.strip()]


def relative_days(dt, now=None):
    if not dt:
        return None
    now = now or datetime.now()
    return (dt - now).total_seconds() / 86400.0


def days_ago(days, now=None):
    return (now or datetime.now()) - timedelta(days=days)
