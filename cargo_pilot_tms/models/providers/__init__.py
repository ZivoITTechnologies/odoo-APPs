# -*- coding: utf-8 -*-
from .base import CourierError, CourierProvider, guess_status, parse_dt, FLOW, TERMINAL  # noqa: F401
from .bluedart import BlueDartProvider
from .clickpost import ClickPostProvider
from .delhivery import DelhiveryProvider
from .dtdc import DtdcProvider
from .manual import DemoProvider, ManualProvider
from .shiprocket import ShiprocketProvider
from .xpressbees import XpressbeesProvider

PROVIDERS = {cls.code: cls for cls in (
    DelhiveryProvider, BlueDartProvider, DtdcProvider, ShiprocketProvider, XpressbeesProvider,
    ClickPostProvider, ManualProvider, DemoProvider,
)}

PROVIDER_SELECTION = [
    ('delhivery', 'Delhivery'),
    ('bluedart', 'Blue Dart'),
    ('dtdc', 'DTDC'),
    ('shiprocket', 'Shiprocket (25+ couriers)'),
    ('xpressbees', 'Xpressbees'),
    ('clickpost', 'ClickPost (Shree Maruti, Professional, Trackon...)'),
    ('manual', 'Counter / phone booking (any courier)'),
    ('demo', 'Demo courier (simulator)'),
]


def get_provider(account):
    return PROVIDERS[account.provider](account)
