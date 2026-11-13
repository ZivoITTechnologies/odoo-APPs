#!/usr/bin/env python3
"""End-to-end offline test of the ZivoIT Field Tracker web app (real Chrome).

Scenario: salesperson starts the day online, loses all network, keeps moving,
re-opens the app with no network, checks in / out at a customer offline, then
the network comes back. Verifies that every point and the visit reach Odoo
exactly once, flagged as captured offline, and that nothing is uploaded while
offline.

Usage: python3 offline_e2e.py http://localhost:8069 DB LOGIN PASSWORD [screenshot_dir]
Requires: pip install playwright; a Chrome/Chromium binary.
"""
import sys
import time
import xmlrpc.client

from playwright.sync_api import sync_playwright

BASE, DB, LOGIN, PASSWORD = sys.argv[1:5]
SHOTS = sys.argv[5] if len(sys.argv) > 5 else None
CHROME = '/usr/bin/google-chrome'

common = xmlrpc.client.ServerProxy(BASE + '/xmlrpc/2/common')
models = xmlrpc.client.ServerProxy(BASE + '/xmlrpc/2/object')
ADMIN = common.authenticate(DB, 'admin', 'admin', {})


def rpc(model, method, *args, **kw):
    return models.execute_kw(DB, ADMIN, 'admin', model, method, list(args), kw)


tester = rpc('res.users', 'search', [('login', '=', LOGIN)])[0]


def server_points():
    return rpc('zivo.track.point', 'search_read', [('user_id', '=', tester)],
               fields=['client_uid', 'captured_offline', 'delay_minutes', 'latitude'])


def check(cond, msg):
    print(('PASS ' if cond else 'FAIL ') + msg)
    if not cond:
        raise SystemExit(1)


# start from a clean slate for the tester
old = rpc('zivo.track.point', 'search', [('user_id', '=', tester)])
if old:
    rpc('zivo.track.point', 'unlink', old)
rpc('zivo.track.visit', 'unlink', rpc('zivo.track.visit', 'search', [('user_id', '=', tester)]))

lat, lon = 17.4375, 78.4483  # Ameerpet, Hyderabad


def pending(page):
    return int(page.inner_text('#zv-pending'))


def wait_for(fn, timeout=20, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
    ctx = browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=2,
                              is_mobile=True, has_touch=True, permissions=['geolocation'],
                              geolocation={'latitude': lat, 'longitude': lon, 'accuracy': 8},
                              user_agent='Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 '
                                         '(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36')
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))

    # 1. log in and open the app online
    page.goto(BASE + '/web/login')
    page.fill('input[name=login]', LOGIN)
    page.fill('input[name=password]', PASSWORD)
    page.click('button[type=submit]')
    page.wait_for_url('**/odoo**')
    page.goto(BASE + '/zivoit_tracking/app')
    page.wait_for_selector('#zv-toggle')
    page.evaluate('navigator.serviceWorker.ready')
    page.reload()  # now controlled by the service worker
    page.wait_for_selector('#zv-toggle')
    check(page.evaluate('!!navigator.serviceWorker.controller'), 'service worker controls the app')
    check(wait_for(lambda: 'customers available offline' in page.inner_text('#zv-cust-info')),
          'customer list cached for offline use')

    # 2. start the day online - first point is uploaded live
    page.click('#zv-toggle')
    check(wait_for(lambda: len(server_points()) == 1), 'first point uploaded while online')
    check(not server_points()[0]['captured_offline'], 'online point is not flagged offline')
    if SHOTS:
        page.wait_for_timeout(3800)  # let the toast disappear
        page.screenshot(path=SHOTS + '/phone_online.png')

    # 3. network lost: keep moving for 12 fixes
    ctx.set_offline(True)
    page.wait_for_timeout(300)
    check('No network' in page.inner_text('#zv-net'), 'app shows "No network"')
    for i in range(12):
        lat += 0.0001   # ~11 m per 1.3 s = city driving speed
        lon -= 0.00004
        ctx.set_geolocation({'latitude': lat, 'longitude': lon, 'accuracy': 9})
        page.wait_for_timeout(1300)
    check(wait_for(lambda: pending(page) >= 12), 'points queued on the phone while offline (%s)' % pending(page))
    check(len(server_points()) == 1, 'nothing reached the server while offline')

    # 4. app closed and re-opened with NO network (served by the service worker)
    page.reload()
    page.wait_for_selector('#zv-toggle')
    check(page.inner_text('#zv-toggle') == 'End Day', 'app re-opens offline and resumes tracking')
    check(pending(page) >= 12, 'offline queue survived the reload (%s pending)' % pending(page))
    for i in range(3):
        lat += 0.0001
        ctx.set_geolocation({'latitude': lat, 'longitude': lon, 'accuracy': 9})
        page.wait_for_timeout(1300)

    # 5. customer check-in and check-out without network
    page.fill('#zv-search', 'Sri Venkateswara')
    page.click('#zv-customers li button')
    page.wait_for_selector('#zv-active-visit:not([hidden])')
    page.select_option('#zv-outcome', 'order')
    page.fill('#zv-note', 'Order for 40 cartons - recorded with no signal')
    if SHOTS:
        page.wait_for_timeout(3800)
        page.evaluate("document.getElementById('zv-visit-card').scrollIntoView({block: 'end'})")
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS + '/phone_offline_visit.png')
    page.click('#zv-checkout')
    page.wait_for_selector('#zv-customer-picker:not([hidden])')
    queued = pending(page)
    if SHOTS:
        page.fill('#zv-search', '')
        page.evaluate('window.scrollTo(0, 0)')
        page.wait_for_timeout(3800)
        page.screenshot(path=SHOTS + '/phone_offline.png')
    check(queued >= 16, 'points + visit + events queued: %s' % queued)
    check(not rpc('zivo.track.visit', 'search', [('user_id', '=', tester)]), 'visit not uploaded while offline')

    # 6. signal is back -> automatic upload
    page.wait_for_timeout(3000)
    ctx.set_offline(False)
    check(wait_for(lambda: pending(page) == 0, timeout=60), 'queue fully uploaded after reconnect')
    pts = server_points()
    uids = [x['client_uid'] for x in pts]
    check(len(uids) == len(set(uids)), 'no duplicate points on the server')
    offline_pts = [x for x in pts if x['captured_offline']]
    check(len(pts) >= 16, 'all points on the server: %s' % len(pts))
    check(len(offline_pts) >= 15, 'offline points flagged "captured offline": %s' % len(offline_pts))
    visit = rpc('zivo.track.visit', 'search_read', [('user_id', '=', tester)],
                fields=['state', 'outcome', 'captured_offline', 'note', 'within_geofence'])
    check(len(visit) == 1 and visit[0]['state'] == 'done' and visit[0]['outcome'] == 'order',
          'offline visit synced with check-out and outcome')
    session = rpc('zivo.track.session', 'search_read', [('user_id', '=', tester)],
                  fields=['point_count', 'offline_point_count', 'distance_km', 'start_time'])[0]
    check(session['start_time'], 'day start event synced')
    check(0.12 < session['distance_km'] < 0.35, 'distance computed from offline points: %.2f km' % session['distance_km'])
    print('INFO day log: %s' % session)
    if SHOTS:
        page.evaluate('window.scrollTo(0, 0)')
        page.wait_for_timeout(3800)
        page.screenshot(path=SHOTS + '/phone_synced.png')
    check(not errors, 'no JavaScript errors (%s)' % errors)
    browser.close()
print('ALL OFFLINE E2E CHECKS PASSED')
