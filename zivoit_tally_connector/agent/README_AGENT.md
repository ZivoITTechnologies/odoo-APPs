# ZivoIT Tally Bridge Agent

The agent lets a cloud Odoo (Odoo Online, Odoo.sh, any hosted Odoo 19) talk to
TallyPrime running in your office — with **no port forwarding, VPN or static IP**.
It only makes outbound HTTPS calls to Odoo.

## 1. Prepare TallyPrime
1. Open the company you want to sync.
2. `F1 (Help) > Settings > Connectivity > Client/Server configuration`:
   *TallyPrime acts as* = **Both** (or Server), *Port* = **9000**.
3. Restart TallyPrime once. Keep the company open while syncing.

## 2. Prepare Odoo
1. Create an internal user, e.g. `tally-agent@yourco`, and give it only
   **Tally Connector › Tally Bridge Agent (API user)**.
2. Log in as that user (or as admin via *Settings > Users*), open
   *Preferences > Account Security > New API Key*, copy the key.
3. In *Tally > Connections*, set *Transport* = **Bridge Agent**, then click
   **Agent setup** and download the package.

## 3. Install on the Tally computer (Windows)
1. Install Python 3.10+ from python.org (tick *Add to PATH*).
2. Unzip the package to `C:\ZivoIT\TallyAgent`.
3. Edit `agent_config.json` and paste the API key into `api_key`.
4. Test: `python zivoit_tally_agent.py --check`
5. Run as a service (starts with Windows): right-click
   `install_windows_service.bat` › *Run as administrator*.

Linux/macOS: `python3 zivoit_tally_agent.py` (systemd/launchd unit optional).

## Security
* The API key belongs to a user that can do nothing except call the two agent
  methods; the connection token identifies which Tally connection it serves.
* All traffic to Odoo is HTTPS. Tally traffic stays on the local machine/LAN.
* Logs: `zivoit_tally_agent.log` (rotated, 3 × 2 MB).

## Support
Email info@zivoit.com · www.zivoit.com
