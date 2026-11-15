# Odoo App Store: publishing and troubleshooting
**ZivoIT Fuel Management System** · technical name `zivoit_fuel_management` · version **19.0.2.0.1**

## Why the app is in Git but not updating on the App Store
The Odoo App Store scanner is strict. These are the usual causes, most common first:

| # | Cause | How to check | Fix |
|---|---|---|---|
| 1 | **Wrong repository layout.** The module is inside a sub-folder (for example `ZivoIT_Fuel_Management_System/01_Odoo_Module/zivoit_fuel_management/`), or a zip file was committed instead of the folder | The repository root on GitHub must show the folder `zivoit_fuel_management/` directly, with `__manifest__.py` inside it | Push the ready-made repository in `02_App_Store_Git_Repo` (structure below) |
| 2 | **Branch name is not exactly `19.0`.** For example `main`, `master`, `v19` or `19` | The GitHub branch selector must show `19.0` | `git push origin 19.0` (see the commands below) |
| 3 | **Version number not increased.** The scanner only publishes an update when the manifest `version` is higher than the published one | Compare `version` in `__manifest__.py` with the version on the app page | This release is **19.0.2.0.1**. Increase it for every future update |
| 4 | **Technical name changed.** The earlier package was `bpcl_petrol_pump`. Renaming it to `zivoit_fuel_management` creates a **new app**, so the old listing will never update | apps.odoo.com → My Apps: you will see either two apps or the new one missing | Treat `zivoit_fuel_management` as the app to publish. Unpublish the old `bpcl_petrol_pump` listing and remove that folder from the 19.0 branch |
| 5 | **Repository not registered, or not rescanned** | apps.odoo.com → My Apps → Repositories: the URL must end with `#19.0` | Register `ssh://git@github.com/<org>/<repo>.git#19.0`, or press **Scan / Rescan** |
| 6 | **Private repository without the deploy key** | The scan log shows an access or permission error | Copy the SSH deploy key that Odoo shows into GitHub → Settings → Deploy keys (read access) |
| 7 | **Scan error** (manifest, missing file, duplicate module) | apps.odoo.com → My Apps → Repositories → **Logs** | Send the log text; this release passes the same structure and install checks locally |
| 8 | **Paid app, vendor profile incomplete** | My Apps shows the app but "Published" is off | Complete the vendor payout details (bank or PayPal) in your Odoo apps vendor account, then publish |

## Required repository structure (branch `19.0`)
```
<your-repo>/              <- branch 19.0
├── README.md
├── .gitignore
└── zivoit_fuel_management/
    ├── __manifest__.py   (name, version 19.0.2.0.1, price 1599 USD, license OPL-1, images)
    ├── __init__.py
    ├── LICENSE  COPYRIGHT  README.md  CHANGELOG.md  TEST_REPORT.md
    ├── controllers/ data/ demo/ i18n/ models/ report/ security/ static/ tests/ views/ wizard/
    └── static/description/  index.html  banner.png  icon.png  screenshots/
```

## Push commands
Option A: use the ready repository (it already contains the commit on branch 19.0).
```bash
cd 02_App_Store_Git_Repo/zivoit-odoo-apps
git remote add origin git@github.com:<org>/<repo>.git
git push -u origin 19.0 --force      # --force replaces the old layout on this branch
```

Option B: update your existing repository.
```bash
git checkout 19.0                       # or: git checkout -b 19.0
git rm -r --cached . -q && rm -rf ./*   # clear the old layout (keep .git)
cp -r <folder>/01_Odoo_Module/zivoit_fuel_management .
git add -A && git commit -m "zivoit_fuel_management 19.0.2.0.1"
git push origin 19.0
```

Then go to apps.odoo.com → **My Apps** → your repository → **Scan**. Within a few minutes the app should appear with the cover, the description and the USD 1,599 price. If not, open **Logs** and share the message.

## Listing facts
| Field | Value |
|---|---|
| Name | ZivoIT Fuel Management System |
| Price / licence | USD 1,599 / OPL-1 |
| Cover / icon | `static/description/banner.png` (1568×784) / `icon.png` (256×256) |
| Description | `static/description/index.html`: inline styles only, no scripts, every image local |
| Dependencies | base, mail, portal, web, hr, fleet (all Community) |
| Odoo editions | Community and Enterprise; On-premise and Odoo.sh |
