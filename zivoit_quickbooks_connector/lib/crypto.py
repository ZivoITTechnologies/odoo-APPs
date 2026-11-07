# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
"""Symmetric encryption of OAuth secrets at rest (Fernet / AES-128-CBC + HMAC).

The key is derived from the database UUID secret so a stolen SQL dump alone
cannot be used to call QuickBooks on the customer's behalf."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "zq1$"


def _fernet(secret):
    key = hashlib.sha256(("zivoit-qbo::%s" % secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(secret, value):
    if not value:
        return False
    return PREFIX + _fernet(secret).encrypt(value.encode()).decode()


def decrypt(secret, value):
    if not value:
        return False
    if not value.startswith(PREFIX):
        return value  # legacy / plain value
    try:
        return _fernet(secret).decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return False
