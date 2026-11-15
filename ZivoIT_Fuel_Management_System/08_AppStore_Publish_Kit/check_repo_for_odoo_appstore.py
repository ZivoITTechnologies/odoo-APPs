#!/usr/bin/env python3
"""Pre-flight check of a Git repository before registering it on apps.odoo.com.

Usage:  python check_repo_for_odoo_appstore.py <path-to-cloned-repo> [series]
        (series defaults to 19.0)
Checks: branch name, one module folder per app at the repository root, manifest
syntax/keys, version prefix, licence for paid apps, required images are real PNGs,
description page present, no zip files or nested module folders.
"""
import ast
import os
import subprocess
import sys

OK, BAD = '[ OK ]', '[FAIL]'


def png(path):
    with open(path, 'rb') as f:
        return f.read(8) == b'\x89PNG\r\n\x1a\n'


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else '.'
    series = sys.argv[2] if len(sys.argv) > 2 else '19.0'
    errors = 0

    def report(cond, msg):
        nonlocal errors
        print(OK if cond else BAD, msg)
        errors += 0 if cond else 1

    try:
        branch = subprocess.check_output(['git', '-C', repo, 'rev-parse', '--abbrev-ref', 'HEAD'], text=True).strip()
    except Exception:
        branch = '?'
    report(branch == series, 'Current branch is "%s" (must be exactly "%s")' % (branch, series))

    root = sorted(os.listdir(repo))
    zips = [f for f in root if f.lower().endswith(('.zip', '.rar', '.7z', '.tar.gz'))]
    report(not zips, 'No archive files at the repository root %s' % (zips or ''))
    modules = [d for d in root if os.path.isfile(os.path.join(repo, d, '__manifest__.py'))]
    report(bool(modules), 'Module folders found at the root: %s' % (modules or 'NONE'))
    nested = []
    for d, _dirs, files in os.walk(repo):
        if '.git' in d:
            continue
        if '__manifest__.py' in files and os.path.dirname(os.path.abspath(d)) != os.path.abspath(repo):
            nested.append(os.path.relpath(d, repo))
    report(not nested, 'No nested module folders (Odoo only scans the root) %s' % (nested or ''))

    for mod in modules:
        base = os.path.join(repo, mod)
        print('\n--- %s' % mod)
        try:
            m = ast.literal_eval(open(os.path.join(base, '__manifest__.py'), encoding='utf-8').read())
        except Exception as e:  # noqa: BLE001
            report(False, 'Manifest is a valid Python dict (%s)' % e)
            continue
        report(True, 'Manifest is a valid Python dict')
        report(str(m.get('version', '')).startswith(series + '.'), 'Version %s starts with %s.' % (m.get('version'), series))
        for key in ('name', 'author', 'license', 'depends'):
            report(bool(m.get(key)), 'Manifest key "%s" present' % key)
        if m.get('price'):
            report(m.get('currency') in ('EUR', 'USD'), 'Currency %s is EUR or USD' % m.get('currency'))
            report(m.get('license') in ('OPL-1', 'Other proprietary'), 'Licence %s allows selling' % m.get('license'))
        for f in m.get('data', []) + m.get('demo', []):
            if not os.path.exists(os.path.join(base, f)):
                report(False, 'Data file exists: %s' % f)
        icon = os.path.join(base, 'static/description/icon.png')
        report(os.path.exists(icon) and png(icon), 'static/description/icon.png exists and is a real PNG')
        for img in m.get('images', []):
            p = os.path.join(base, img)
            report(os.path.exists(p), 'Cover image exists: %s' % img)
            if os.path.exists(p) and img.lower().endswith('.png'):
                report(png(p), 'Cover image is a real PNG: %s' % img)
        report(os.path.exists(os.path.join(base, 'static/description/index.html')), 'static/description/index.html exists')

    print('\nRESULT:', 'READY to register on apps.odoo.com' if not errors else '%d problem(s) to fix' % errors)
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
