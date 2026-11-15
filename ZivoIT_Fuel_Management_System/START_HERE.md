# ZivoIT Fuel Management System — Odoo 19 (release 19.0.2.0.2)

## Fixing the App Store registration error
The module itself is verified: 28/28 tests pass, it installs cleanly from a fresh Git clone, and its images are real PNG files.
The error comes from how the repository is connected to apps.odoo.com. Do these three things:

1. **Push** with `08_AppStore_Publish_Kit/publish_to_github_windows.bat <github-user> <repo>` (or the `.sh` version on Mac/Linux).
2. **Private repository:** on GitHub, open Settings → Collaborators and add **`online-odoo`** with **Read** access.
3. **Register** in apps.odoo.com → My Apps with exactly: `ssh://git@github.com/<github-user>/<repo>.git#19.0`, then press **Scan**.

For each specific error message, see `04_Documentation/ODOO_APPSTORE_ERRORS_AND_FIXES.md`.

| Folder | Contents |
|---|---|
| `01_Odoo_Module/zivoit_fuel_management` | Module source (version 19.0.2.0.2) |
| `02_App_Store_Git_Repo/zivoit-odoo-apps` | Ready repository: branch `19.0`, module at the root |
| `03_App_Store_Upload_Zip` | Module zip, for Odoo.sh or on-premise installs |
| `04_Documentation` | README, TEST_REPORT, CHANGELOG, APP_STORE_SUBMISSION, ERRORS_AND_FIXES |
| `05_App_Store_Listing` | Cover, icon, description page preview |
| `06_Screenshots` / `07_Sample_Outputs` | Screenshots; PDF and Excel samples |
| `08_AppStore_Publish_Kit` | Publish scripts (Windows and Mac/Linux), repository checker, error guide |

**Support:** info@zivoit.com · www.zivoit.com
