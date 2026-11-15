# Odoo App Store submission guide

## 1. Module facts
| Field | Value |
|---|---|
| Technical name | `zivoit_fuel_management` |
| Listing name | ZivoIT Fuel Management System |
| Version | 19.0.2.0.0 (Odoo 19) |
| Price | 1599.0 USD (`price` / `currency` in `__manifest__.py`) |
| License | OPL-1 (file `LICENSE`) |
| Cover image | `static/description/banner.png` (1568×784, 2:1) |
| Icon | `static/description/icon.png` (256×256) |
| Description page | `static/description/index.html` (inline styles only, relative image paths) |
| Language | English. `i18n/zivoit_fuel_management.pot` contains 1,244 strings, all English |
| Dependencies | base, mail, portal, web, hr, fleet |
| Tests | 28 automated tests: 0 failed, 0 errors, no warnings on install or tests |

## 2. Publish
Odoo publishes apps from a Git repository.

1. Create a Git repository (GitHub, GitLab or Bitbucket) with a branch named **19.0**.
2. Put the folder `zivoit_fuel_management/` at the **root** of that branch, and push.
3. Go to apps.odoo.com and sign in with ZivoIT's Odoo account.
4. Open **My Apps → Submit / Register a repository**. Paste the SSH URL with the branch, e.g. `ssh://git@github.com/zivoit/odoo-apps.git#19.0`.
5. If the repository is private, add the deploy key that Odoo shows.
6. Odoo scans the repository. The app appears with the price, cover and description taken from the manifest and `static/description`.
7. To publish an update: raise `version` in the manifest and push. Odoo rescans automatically.

## 3. Pre-submission checklist (done)
- [x] All user-visible strings are in English, with no Hindi or Telugu text or keywords.
- [x] Installing does not change the buyer's company logo or currency. Only the demo data brands the sample company.
- [x] No external scripts or fonts in `index.html`. All images resolve.
- [x] Paid-app licence (OPL-1) and `COPYRIGHT` file included.
- [x] Clean install with and without demo data; zero warnings in the log.
- [x] Portal security: customers see only their own data, and AI and ledger services refuse direct RPC calls from portal users.

## 4. Before going live (owner decision)
- **Trademark:** the listing text mentions BPCL/IOCL/HPCL descriptively. The demo company and screenshots show the BPCL logo. Selling an app that displays a third-party logo may require that company's permission. If in doubt, replace the screenshots with a neutral demo brand.
- **Live demo:** add a `live_test_url` to the manifest if you host one.
