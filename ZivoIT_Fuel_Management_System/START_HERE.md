# ZivoIT Fuel Management System — Odoo 19

This is an AI-enabled petrol pump and fleet ERP by ZivoIT Technologies. It covers:
- **Station operations:** nozzle shifts and credit billing with payment status and a UPI QR code.
- **Stock and fleet:** tanks and DIP, your own tanker fleet, trips, doorstep delivery and maintenance.
- **Back office:** accounts and payroll.
- **Customers:** a self-service portal with an AI assistant.

| Folder | Contents |
|---|---|
| `01_Odoo_Module/zivoit_fuel_management` | Module source. Copy it into your addons path |
| `02_App_Store_Upload` | `zivoit_fuel_management_19.0.2.0.0.zip`, plus the submission guide |
| `03_Documentation` | README (full user and technical guide), TEST_REPORT, CHANGELOG, APP_STORE_SUBMISSION |
| `04_App_Store_Listing` | Cover image (1568×784), app icon (256×256), preview of the description page |
| `05_Screenshots` | 16 screenshots: admin, fleet, billing, customer portal, AI, mobile |
| `06_Sample_Outputs` | Credit bill PDF (with UPI QR), customer statement PDF, ledger and slips Excel |

## Key facts
| | |
|---|---|
| Technical name | `zivoit_fuel_management` |
| Version | 19.0.2.0.0 |
| Price | USD 1,599 |
| Licence | OPL-1 |
| Dependencies | base, mail, portal, web, hr, fleet |
| Language | English (all 1,244 strings checked) |
| Tests | 28 automated tests: 0 failed, 0 errors |

## Quick start
```bash
cp -r 01_Odoo_Module/zivoit_fuel_management /opt/odoo/custom_addons/
./odoo-bin -c odoo.conf -d demo_db -i zivoit_fuel_management --with-demo
```

**Demo logins**
- Admin: `admin` / `admin`
- Customers: `konark` / `Konark@123` and `vayu` / `Vayu@123`

**Support:** ZivoIT Technologies, info@zivoit.com, www.zivoit.com
