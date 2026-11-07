# -*- coding: utf-8 -*-
"""Shared selections used across Cargo Pilot models."""

SHIPMENT_MODES = [
    ('ftl', 'Full truck (FTL)'),
    ('ltl', 'Part load (LTL)'),
]

CHANNELS = [
    ('marketplace', 'Marketplace'),
    ('modern_retail', 'Modern retail'),
    ('offline', 'Offline / distributor'),
    ('d2c', 'Own website (D2C)'),
    ('stock_transfer', 'Warehouse transfer'),
    ('inbound', 'Inbound from OEM / plant'),
    ('import', 'Import (port / CFS)'),
    ('return', 'Return'),
]

TRACKING_METHODS = [
    ('driver', 'Driver phone'),
    ('gps', 'GPS device'),
    ('sim', 'SIM (consent)'),
    ('fastag', 'FASTag toll events'),
    ('carrier', 'Carrier updates (LTL docket)'),
    ('manual', 'Manual updates'),
]


def split_serials(text):
    """Serial numbers typed or scanned one per line, or separated by commas / spaces."""
    if not text:
        return []
    raw = text.replace(',', '\n').replace(';', '\n').replace('\t', '\n')
    seen, result = set(), []
    for token in raw.split():
        token = token.strip().upper()
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result
