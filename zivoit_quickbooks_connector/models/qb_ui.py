# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
"""Show QuickBooks errors to users as clean Odoo messages.

The Odoo web client only renders the friendly "Invalid Operation" dialog for
``odoo.exceptions.UserError`` itself (it matches the exception by its exact
name). Every button (``action_*`` method) of the connector is therefore wrapped
so a QuickBooks/network error reaches the user as a plain UserError."""
import functools

from odoo.exceptions import UserError

from ..lib.qbo_client import QBOError


def _wrap(method):
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except QBOError as exc:
            raise UserError(str(exc.args[0]) if exc.args else str(exc)) from exc
    return wrapper


def friendly_errors(cls):
    """Class decorator: wrap every ``action_*`` method defined on ``cls``."""
    for name, value in list(vars(cls).items()):
        if name.startswith("action_") and callable(value):
            setattr(cls, name, _wrap(value))
    return cls
