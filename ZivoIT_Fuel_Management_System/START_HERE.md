# ZivoIT Fuel Management System — Odoo 19 (release 19.0.2.0.1)

## If the app is in Git but not on the App Store
Read `04_Documentation/APP_STORE_SUBMISSION.md`. In short:
- **Layout:** the folder `zivoit_fuel_management/` must sit at the **root** of a branch named exactly **`19.0`**.
- **Version:** it must increase with every update (this release is `19.0.2.0.1`).
- **Rescan:** press **Scan** in apps.odoo.com → My Apps.

The repository in `02_App_Store_Git_Repo` already has exactly that structure; just push it.

| Folder | Contents |
|---|---|
| `01_Odoo_Module/zivoit_fuel_management` | Module source |
| `02_App_Store_Git_Repo/zivoit-odoo-apps` | Ready Git repository (branch `19.0`, one commit). Also available as a `.bundle` file, plus `HOW_TO_PUSH.md` |
| `03_App_Store_Upload_Zip` | Module zip, for Odoo.sh or on-premise installs |
| `04_Documentation` | README, TEST_REPORT, CHANGELOG, APP_STORE_SUBMISSION (publishing and troubleshooting) |
| `05_App_Store_Listing` | Cover (1568×784), icon (256×256), description page preview |
| `06_Screenshots` | 16 screenshots |
| `07_Sample_Outputs` | Credit bill and statement PDFs, ledger and slips Excel |

## Push in three commands
```bash
cd 02_App_Store_Git_Repo/zivoit-odoo-apps
git remote add origin git@github.com:<org>/<repo>.git
git push -u origin 19.0 --force
```
Then go to apps.odoo.com → My Apps → Repository → **Scan**.

## Tested
- Fresh clone of branch 19.0: 28 automated tests, 0 failed, 0 errors.
- Install log: 0 warnings, 0 errors.
- Uninstall with live data: works.
- Browser check of admin and customer portal: no console errors.
- All strings in English.

**Demo logins:** `admin` / `admin`, `konark` / `Konark@123`, `vayu` / `Vayu@123`
**Support:** info@zivoit.com · www.zivoit.com
