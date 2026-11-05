# -*- coding: utf-8 -*-
"""Stdlib-only HTTP transport to the TallyPrime XML server (default port 9000).

Shared with the bridge agent, so it must not depend on Odoo or `requests`.
"""
import socket
import time
import urllib.error
import urllib.request


class TallyConnectionError(Exception):
    pass


def post_xml(host, port, xml, timeout=60, retries=2, backoff=1.5, use_https=False):
    """POST an envelope to Tally and return raw response bytes.

    Retries only on network-level failures (Tally busy / restarting); never on a
    successful HTTP exchange, so imports are not duplicated.
    """
    url = '%s://%s:%s' % ('https' if use_https else 'http', host or 'localhost', int(port or 9000))
    data = xml.encode('utf-8') if isinstance(xml, str) else xml
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method='POST', headers={
            'Content-Type': 'text/xml; charset=utf-8',
            'Content-Length': str(len(data)),
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise TallyConnectionError('Tally HTTP error %s: %s' % (e.code, e.reason))
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise TallyConnectionError(
        'Cannot reach Tally at %s (%s). Check that TallyPrime is running, a company is '
        'open and F1 > Settings > Connectivity > "TallyPrime acts as" is Server/Both on port %s.'
        % (url, last_err, port))
