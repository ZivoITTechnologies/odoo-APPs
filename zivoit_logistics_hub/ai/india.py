# -*- coding: utf-8 -*-
"""India postal intelligence: pincode -> state(s), courier zones, metro detection.

India Post assigns PIN codes by region; the first two/three digits identify the postal circle.
The tables below use Odoo's ``res.country.state`` codes for India.
"""
import re

# 2-digit prefix -> candidate states (some circles span several states/UTs)
PREFIX2 = {
    '11': {'DL'}, '12': {'HR'}, '13': {'HR', 'PB'}, '14': {'PB'}, '15': {'PB'}, '16': {'PB', 'CH', 'HR'},
    '17': {'HP'}, '18': {'JK'}, '19': {'JK'},
    '20': {'UP'}, '21': {'UP'}, '22': {'UP'}, '23': {'UP'}, '24': {'UP', 'UK'}, '25': {'UP'}, '26': {'UP', 'UK'},
    '27': {'UP'}, '28': {'UP'},
    '30': {'RJ'}, '31': {'RJ'}, '32': {'RJ'}, '33': {'RJ'}, '34': {'RJ'},
    '36': {'GJ', 'DD'}, '37': {'GJ'}, '38': {'GJ'}, '39': {'GJ', 'DN', 'DD'},
    '40': {'MH', 'GA'}, '41': {'MH'}, '42': {'MH'}, '43': {'MH'}, '44': {'MH'},
    '45': {'MP'}, '46': {'MP'}, '47': {'MP'}, '48': {'MP'}, '49': {'CG'},
    '50': {'TS'}, '51': {'AP', 'TS'}, '52': {'AP'}, '53': {'AP'},
    '56': {'KA'}, '57': {'KA'}, '58': {'KA'}, '59': {'KA'},
    '60': {'TN', 'PY'}, '61': {'TN'}, '62': {'TN'}, '63': {'TN'}, '64': {'TN'},
    '67': {'KL', 'PY'}, '68': {'KL', 'LD'}, '69': {'KL'},
    '70': {'WB'}, '71': {'WB'}, '72': {'WB'}, '73': {'WB', 'SK'}, '74': {'WB', 'AN'},
    '75': {'OD'}, '76': {'OD'}, '77': {'OD'},
    '78': {'AS'}, '79': {'AR', 'ML', 'MN', 'MZ', 'NL', 'TR', 'AS'},
    '80': {'BR'}, '81': {'BR', 'JH'}, '82': {'BR', 'JH'}, '83': {'JH'}, '84': {'BR'}, '85': {'BR'},
}
# 3-digit refinements
PREFIX3 = {
    '160': {'CH', 'PB'}, '194': {'JK'}, '403': {'GA'}, '605': {'PY', 'TN'}, '609': {'PY', 'TN'},
    '673': {'KL', 'PY'}, '682': {'KL', 'LD'}, '533': {'AP', 'PY'}, '737': {'SK'}, '744': {'AN'},
    '790': {'AR'}, '791': {'AR'}, '792': {'AR'}, '793': {'ML'}, '794': {'ML'}, '795': {'MN'}, '796': {'MZ'},
    '797': {'NL'}, '798': {'NL'}, '799': {'TR'}, '396': {'DN', 'DD', 'GJ'}, '362': {'DD', 'GJ'},
    '246': {'UK'}, '247': {'UK', 'UP'}, '248': {'UK'}, '249': {'UK'}, '262': {'UK', 'UP'}, '263': {'UK'},
}
METRO_PREFIXES = {'110': 'Delhi', '400': 'Mumbai', '700': 'Kolkata', '600': 'Chennai', '560': 'Bengaluru',
                  '500': 'Hyderabad', '411': 'Pune', '380': 'Ahmedabad', '201': 'Noida', '122': 'Gurugram'}
SPECIAL_STATES = {'JK', 'AN', 'LD', 'AR', 'AS', 'ML', 'MN', 'MZ', 'NL', 'TR', 'SK'}
PIN_RE = re.compile(r'^[1-9][0-9]{5}$')


def valid_pincode(zip_code):
    return bool(PIN_RE.match(str(zip_code or '').strip()))


def states_for_pincode(zip_code):
    zip_code = str(zip_code or '').strip()
    if not valid_pincode(zip_code):
        return set()
    return PREFIX3.get(zip_code[:3]) or PREFIX2.get(zip_code[:2]) or set()


def is_metro(zip_code):
    return str(zip_code or '')[:3] in METRO_PREFIXES


def zone_for(origin_zip, dest_zip):
    """Indian courier zone model: local < regional < metro < national < special."""
    origin_zip, dest_zip = str(origin_zip or ''), str(dest_zip or '')
    dest_states = states_for_pincode(dest_zip)
    if dest_states and dest_states <= SPECIAL_STATES:
        return 'special'
    if origin_zip[:3] and origin_zip[:3] == dest_zip[:3]:
        return 'local'
    if states_for_pincode(origin_zip) & dest_states:
        return 'regional'
    if is_metro(origin_zip) and is_metro(dest_zip):
        return 'metro'
    return 'national'


def state_matches(zip_code, state_code):
    """True / False, or None when it cannot be determined."""
    states = states_for_pincode(zip_code)
    if not states or not state_code:
        return None
    return state_code.upper() in states
