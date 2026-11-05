#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZivoIT Tally Bridge Agent
==========================

Runs on (or next to) the computer where TallyPrime is open.  It makes only
OUTBOUND HTTPS calls to Odoo (no port forwarding, no VPN, no static IP),
fetches queued Tally requests, executes them against the local TallyPrime
XML server (default http://localhost:9000) and returns the responses.

Works with Odoo Online, Odoo.sh and on-premise Odoo 19 using the standard
JSON-2 API (Authorization: bearer <API key>).

Requirements: Python 3.8+ — standard library only.

Usage:
    python zivoit_tally_agent.py                 # run forever
    python zivoit_tally_agent.py --once          # one poll cycle (for testing)
    python zivoit_tally_agent.py --check         # diagnose Odoo + Tally reachability
    python zivoit_tally_agent.py --config other.json
"""
import argparse
import json
import logging
import logging.handlers
import os
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

VERSION = '19.0.1.0.0'
HERE = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, 'frozen', False) else __file__))
log = logging.getLogger('zivoit_tally_agent')
_running = True


def _stop(*_a):
    global _running
    _running = False


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.url = cfg['odoo_url'].rstrip('/')
        self.db = cfg.get('database') or ''
        self.api_key = cfg['api_key']
        self.token = cfg['connection_token']
        self.tally_host = cfg.get('tally_host') or 'localhost'
        self.tally_port = int(cfg.get('tally_port') or 9000)
        self.poll = int(cfg.get('poll_seconds') or 15)
        self.verify_tls = cfg.get('verify_tls', True)
        self.ctx = None if self.verify_tls else ssl._create_unverified_context()
        self.failures = 0

    # ---------------------------------------------------------- Odoo side
    def call(self, method, **kwargs):
        body = json.dumps(kwargs).encode('utf-8')
        headers = {'Content-Type': 'application/json; charset=utf-8',
                   'Authorization': 'bearer %s' % self.api_key,
                   'User-Agent': 'ZivoIT-Tally-Agent/%s' % VERSION}
        if self.db:
            headers['X-Odoo-Database'] = self.db
        req = urllib.request.Request('%s/json/2/tally.connection/%s' % (self.url, method),
                                     data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=90, context=self.ctx) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'ignore')[:500]
            raise RuntimeError('Odoo HTTP %s: %s' % (e.code, detail))

    # --------------------------------------------------------- Tally side
    def tally(self, xml, timeout=60):
        data = xml.encode('utf-8')
        req = urllib.request.Request('http://%s:%s' % (self.tally_host, self.tally_port),
                                     data=data, method='POST',
                                     headers={'Content-Type': 'text/xml; charset=utf-8'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
            return raw.decode('utf-16')
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return raw.decode('latin-1')

    def tally_alive(self):
        try:
            with socket.create_connection((self.tally_host, self.tally_port), timeout=3):
                return True
        except OSError:
            return False

    # ------------------------------------------------------------ cycle
    def cycle(self):
        alive = self.tally_alive()
        res = self.call('agent_poll', token=self.token, hostname=socket.gethostname(),
                        version=VERSION, tally_ok=alive)
        if res.get('host'):
            self.tally_host, self.tally_port = res['host'], int(res.get('port') or 9000)
        jobs = res.get('jobs') or []
        results = []
        for job in jobs:
            t0 = time.time()
            try:
                if not alive:
                    raise OSError('TallyPrime is not listening on %s:%s'
                                  % (self.tally_host, self.tally_port))
                text = self.tally(job['xml'], timeout=job.get('timeout') or 60)
                results.append({'id': job['id'], 'ok': True, 'response': text})
                log.info('job %s ok (%.1fs, %d bytes)', job['id'], time.time() - t0, len(text))
            except Exception as e:  # noqa: BLE001
                results.append({'id': job['id'], 'ok': False, 'error': str(e)})
                log.warning('job %s failed: %s', job['id'], e)
        if results:
            self.call('agent_submit', token=self.token, results=results)
        return res.get('poll_seconds') or self.poll, len(jobs)

    def run_forever(self):
        log.info('ZivoIT Tally Agent %s → %s (db %s), Tally %s:%s', VERSION, self.url,
                 self.db or 'default', self.tally_host, self.tally_port)
        while _running:
            try:
                wait, n = self.cycle()
                self.failures = 0
                if n:
                    continue  # more work may be waiting
            except Exception as e:  # noqa: BLE001
                self.failures += 1
                wait = min(300, 5 * 2 ** min(self.failures, 6))
                log.error('cycle failed (%s), retrying in %ss', e, wait)
            for _i in range(int(wait)):
                if not _running:
                    break
                time.sleep(1)
        log.info('agent stopped')

    def check(self):
        ok = True
        print('Tally %s:%s ... ' % (self.tally_host, self.tally_port), end='')
        if self.tally_alive():
            print('listening')
        else:
            ok = False
            print('NOT reachable — open TallyPrime, F1 > Settings > Connectivity, set '
                  '"TallyPrime acts as" = Server/Both and port %s' % self.tally_port)
        print('Odoo %s ... ' % self.url, end='')
        try:
            res = self.call('agent_poll', token=self.token, hostname=socket.gethostname(),
                            version=VERSION, tally_ok=self.tally_alive(), limit=0)
            print('OK — connection "%s", Tally company "%s"' % (res.get('connection'),
                                                               res.get('company')))
        except Exception as e:  # noqa: BLE001
            ok = False
            print('FAILED: %s' % e)
        return ok


def load_config(path):
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    for key in ('odoo_url', 'api_key', 'connection_token'):
        if not cfg.get(key) or str(cfg[key]).startswith('PASTE-'):
            raise SystemExit('agent_config.json: please set "%s"' % key)
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(description='ZivoIT Tally Bridge Agent')
    p.add_argument('--config', default=os.path.join(HERE, 'agent_config.json'))
    p.add_argument('--once', action='store_true')
    p.add_argument('--check', action='store_true')
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args(argv)
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.handlers.RotatingFileHandler(
            os.path.join(HERE, 'zivoit_tally_agent.log'), maxBytes=2_000_000, backupCount=3))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)
    agent = Agent(load_config(args.config))
    if args.check:
        return 0 if agent.check() else 1
    if args.once:
        wait, n = agent.cycle()
        log.info('processed %s job(s)', n)
        return 0
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    agent.run_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
