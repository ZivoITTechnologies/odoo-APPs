#!/usr/bin/env bash
# Publish zivoit_fuel_management to GitHub in the layout Odoo Apps expects.
# Usage: ./publish_to_github.sh <github-org-or-user> <repo-name> [path-to-module-folder]
set -euo pipefail
ORG="${1:?GitHub user/organisation}"; REPO="${2:?repository name}"
MODULE_DIR="${3:-$(dirname "$0")/../01_Odoo_Module/zivoit_fuel_management}"
SERIES="19.0"
WORK="$(mktemp -d)"
echo ">> Building a clean repository in $WORK"
git -C "$WORK" init -q -b "$SERIES"
cp -r "$MODULE_DIR" "$WORK/zivoit_fuel_management"
find "$WORK" -name __pycache__ -prune -exec rm -rf {} +
printf '__pycache__/\n*.pyc\n' > "$WORK/.gitignore"
printf '# ZivoIT Odoo Apps (%s)\n\n- zivoit_fuel_management: ZivoIT Fuel Management System\n' "$SERIES" > "$WORK/README.md"
git -C "$WORK" add -A
git -C "$WORK" -c user.name="ZivoIT Technologies" -c user.email="info@zivoit.com" commit -q -m "zivoit_fuel_management for Odoo $SERIES"
REMOTE="${REMOTE_URL:-git@github.com:$ORG/$REPO.git}"
git -C "$WORK" remote add origin "$REMOTE" 2>/dev/null || git -C "$WORK" remote set-url origin "$REMOTE"
python3 "$(dirname "$0")/check_repo_for_odoo_appstore.py" "$WORK" "$SERIES"
echo ">> Pushing branch $SERIES to $REMOTE (replaces the old content of that branch)"
git -C "$WORK" push -u origin "$SERIES" --force
echo
echo "================ NEXT STEPS ================"
echo "1. Private repo? GitHub > $ORG/$REPO > Settings > Collaborators > add user  online-odoo  (Read)."
echo "2. apps.odoo.com > My Apps > Register a repository, paste exactly:"
echo "      ssh://git@github.com/$ORG/$REPO.git#$SERIES"
echo "3. Press Scan. If an error appears, open the repository Logs and compare with ODOO_APPSTORE_ERRORS_AND_FIXES.md"
