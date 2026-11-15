# Odoo Apps registration: errors and fixes
Module: **ZivoIT Fuel Management System** (`zivoit_fuel_management`) · version **19.0.2.0.2** · Odoo **19.0**

The module passes every check: 28/28 tests, a clean install from a fresh Git clone, and real PNG images.
Registration errors come from the **repository connection**, not the code. Follow the steps in order.

## Step 1: Push in the right layout
Run one of the scripts in this folder. They build a clean repository, check it, and push branch `19.0`.
- Windows: `publish_to_github_windows.bat <github-user-or-org> <repo-name>`
- Mac/Linux: `./publish_to_github.sh <github-user-or-org> <repo-name>`

Then check what is actually on GitHub:
```bash
git clone --branch 19.0 git@github.com:<org>/<repo>.git check && python check_repo_for_odoo_appstore.py check
```
It must end with `RESULT: READY to register on apps.odoo.com`.

## Step 2: Give Odoo read access (private repository)
On GitHub, open the repository, then **Settings → Collaborators → Add people**. Add the user **`online-odoo`** with **Read** access.
Alternatively, add Odoo's public key from https://github.com/online-odoo.keys as a Deploy key with read access.

## Step 3: Register with the exact URL format
apps.odoo.com → **My Apps** → **Register a repository**:
```
ssh://git@github.com/<org>/<repo>.git#19.0
```
- Use `ssh://`, not `https://` and not a browser link.
- Use `git@github.com/` with a **slash** after `github.com`.
- End with **`#19.0`**.

## Error messages and fixes
| Error shown by Odoo (or in Logs) | Cause | Fix |
|---|---|---|
| `The provided url is invalid` | Wrong URL format: https, a colon after github.com, or no `#19.0` | Use exactly `ssh://git@github.com/<org>/<repo>.git#19.0` |
| `Permission denied (publickey)` / `fatal: Could not read from remote repository` | Private repository and Odoo has no access | Step 2: add `online-odoo` as a Read collaborator, or add the deploy key |
| `ERROR: Repository not found` | Typo in org or repo name, or the repository is private without access | Copy the names from the GitHub address bar, then do Step 2 |
| `Remote branch 19.0 not found` / `couldn't find remote ref` | The branch is called `main` or `master` | Push with the scripts (they push branch `19.0`) |
| `This repository and branch are already registered` | The same URL is already registered, possibly under another form | Open the existing entry in My Apps and press **Scan** instead of registering again |
| Scan succeeds but **no app appears** | Module folder not at the repository root, or a zip file was committed | Run the checker; push with the scripts |
| App appears but is **not updated** | Version not increased since the last successful scan | This release is 19.0.2.0.2. Always bump `version` and push again |
| Old app `bpcl_petrol_pump` still shows | Renaming creates a new technical name, so it is a new app | Remove `bpcl_petrol_pump` from branch 19.0 and unpublish the old listing in My Apps |
| `Fingerprint has already been taken` (adding a deploy key) | The same key is already used on another repository | Use the `online-odoo` collaborator method instead |
| Image or icon error | Icon not a real PNG | Already verified: `icon.png` and `banner.png` are real PNG files |
| Error on the local `git push` | Your PC has no GitHub SSH key | Run `ssh -T git@github.com`. If it fails, add your key on GitHub, or push over HTTPS: `set REMOTE_URL=https://github.com/<org>/<repo>.git` (Windows) or `REMOTE_URL=... ./publish_to_github.sh ...` and sign in when asked. **Only the URL registered in Odoo must be SSH** |

## If an error persists
Copy the **exact text** from apps.odoo.com → My Apps → your repository → **Logs** (or take a screenshot) and send it.
Odoo Apps support: apps@odoo.com.
