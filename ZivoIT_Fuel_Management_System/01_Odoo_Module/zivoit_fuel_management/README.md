# ZivoIT Fuel Management System (AI enabled) — Odoo 19

Technical name: `zivoit_fuel_management` · Version 19.0.2.0.0 · Price: USD 1,599 · License: OPL-1
Publisher: ZivoIT Technologies — www.zivoit.com — info@zivoit.com

This is a complete fuel-station platform for a BPCL retail outlet (it can be re-branded for any oil company). It covers:

- the forecourt (nozzles, shifts, credit slips);
- the back office (billing, accounts, payroll);
- the station's own fleet (fuel tankers, delivery bowsers, maintenance);
- a self-service customer portal with an AI assistant.

---

## Contents
1. What's inside
2. Installation & first-time setup
3. Daily workflow (step by step)
4. Credit billing — how the bill is built
5. Fleet & tanker management
6. AI features
7. Customer portal
8. Accounting model
9. Security & roles
10. Reports & exports
11. Demo data & logins
12. Tests
13. Technical reference
14. Limits & roadmap

---

## 1. What's inside

| Area | Features |
|---|---|
| **Dashboard** | KPI tiles: sale, credit, card/UPI, cash, bank, outstanding. **AI Insights** panel. **Ask AI** box for staff. Fleet status and trips. Stock-out forecast per tank. Product rate cards, 14-day sales chart, tank gauges, credit exposure, recent slips, alerts |
| **Forecourt** | Shift sheets from nozzle meters, with testing litres, attendant, and card/UPI/fleet-card collections. Credit slips are picked up automatically. Cash short/excess is posted. DSR PDF |
| **Credit customers** | Customer code, credit limit (only a manager can change it), credit days, billing cycle and vehicles. Live outstanding, unbilled amount, exposure, available credit and overdue amount. **AI risk grade A–D** and **AI suggested limit** |
| **Filling slips** | Rate auto-filled from rate history. Credit-limit block, with manager override. **Vehicle-tank capacity check**. Odometer → **km/L per fill**. **AI anomaly flag** on suspicious slips. Printable A6 slip |
| **Indents** | Raised from the portal (or prepared by the AI assistant) or by the office. Approve, reject, or convert to a slip |
| **Billing** | Period bills for one customer or all. **Payment status: Paid / Partially paid / Not paid / Overdue**, using FIFO allocation of receipts. Clear PDF bill with **UPI QR code** |
| **Stock** | Tanks with capacity, dead stock and reorder level. DIP chart (mm → litres). DIP variance posting. Tanker receipts. Stock ledger and pivot |
| **Fleet** *(Odoo Fleet)* | Tankers with compartments, delivery bowsers and service vehicles. Statutory documents: insurance, permit, fitness, PUC, **PESO licence**, W&M calibration, road tax, RC. Expiry alerts. Live status (available / on trip / in workshop) |
| **Trips** | **Depot loading** (terminal → station) creates and posts the tanker receipt. **Doorstep delivery** (station → customer site) creates the customer's credit slip, issues the stock and books the sale. Trip expenses (toll, driver allowance, loading) are posted. Odometer and distance are tracked |
| **Maintenance** | Preventive plans by km and/or days, with due-soon and overdue status. Job cards with parts, labour, tyres and oil, plus vendor, downtime hours and payment. Each job writes an Odoo Fleet service log and posts the cost |
| **Fleet fuel** | Fuel issued to your own vehicles from a station tank (stock out, valued at last purchase cost) or bought outside. km/L per vehicle |
| **Accounts** | Own double-entry ledger (no Odoo Accounting needed). Vouchers: receipt, payment, journal, contra, card/UPI settlement with MDR. Day book. Trial balance PDF |
| **Staff** | Pump roles (manager, cashier, DSM, driver, cleaner). Attendance. Monthly salary from paid days, with incentive and advance |
| **Customer portal** | Dashboard with AI insights and bills to pay. Ledger, month view, statements (PDF/Excel), bills with status and QR, slips (Excel), **My Vehicles** (km/L and 6-month trend), indents, rates, **AI chat assistant** |
| **AI** | Built-in engine that works offline, plus optional **Anthropic Claude** or **OpenAI** behind the assistant. Conversation log |

---

## 2. Installation & first-time setup

**Requirements**
- Odoo 19.0 (Community or Enterprise), Python 3.10+, PostgreSQL 13+.
- wkhtmltopdf 0.12.6 (patched Qt), for PDFs.
- Python packages `xlsxwriter`, `qrcode` and `requests`. All three are already in Odoo's requirements.
- Odoo modules installed automatically: `base`, `mail`, `portal`, `web`, `hr`, `fleet`.

```bash
cp -r zivoit_fuel_management /opt/odoo/custom_addons/
./odoo-bin -c odoo.conf -d <db> -i zivoit_fuel_management               # production
./odoo-bin -c odoo.conf -d <demo_db> -i zivoit_fuel_management --with-demo # with 75 days of sample data
```

Or go to **Apps → Update Apps List → "ZivoIT Fuel Management System" → Activate**.

**On install the module** creates the chart of accounts, 3 shifts, fuel and lubricant products, fleet service types, the BPCL supplier and a daily alert job. It does **not** change your company logo or currency; set them yourself in *Station Settings* (the demo data does this for the sample company).

**First-time setup checklist**
1. *Configuration → Station Settings:* company logo and currency (e.g. INR), outlet name, dealer code, address, GSTIN, bank account, **UPI ID** (this enables the QR code on bills), bill terms and AI engine.
2. *Configuration → Products & Rates:* check the products, then use *Daily Entry → Update Today's Rates*.
3. *Configuration → Tanks & DIP Charts:* add each tank, its DIP chart and opening stock, then press **Post Opening Stock**.
4. *Configuration → Nozzles:* add each nozzle with its tank and opening meter reading.
5. *Customers → Credit Customers:* set the credit limit, credit days, billing cycle and vehicles (with tank capacity). Use **Opening Balance** for old dues.
6. *Fleet → Tankers & Station Fleet:* add each vehicle with its compartments, documents, maintenance plan and driver.
7. *Staff → Employees:* set the pump role and Basic/DA/HRA.
8. Give each customer a login: open the customer → *Fuel Credit* tab → **Portal Login**.

---

## 3. Daily workflow

| When | Who | Where | What happens |
|---|---|---|---|
| Morning | Manager | Update Today's Rates | New rates apply to every slip and nozzle line from that date |
| Each filling | Cashier | Credit Filling Slips → Confirm | The customer's limit and vehicle tank are checked; km/L is computed from the odometer |
| Customer request | Customer | Portal → Indent / AI chat | An indent reaches the office, which approves it or converts it to a slip |
| End of shift | Cashier | Shift Sheet | Enter closing meters, testing litres, card/UPI/fleet amounts and cash collected → **Post** |
| Tanker | Manager | Trips → Depot loading | Dispatch → Unloaded (creates the tanker receipt and stock) → Close (posts expenses, updates odometer) |
| Bulk diesel | Manager | Trips → Doorstep delivery | Dispatch (stock and credit check) → Delivered (creates the customer slip and books the sale) → Close |
| Payments | Cashier | Quick Receipt | The customer ledger updates; bills turn **Paid** or **Partially paid** automatically |
| Banking | Cashier | Bank Deposit / Withdrawal | Cash → bank, or **card/UPI settlement** with MDR charges |
| Weekly | Manager | DIP Readings | Physical stock vs book stock; the variance is posted |
| Billing day | Manager | Generate Bills | One bill per customer for the period |
| Daily (automatic) | System | Scheduled job | Activities are created for expiring documents and due maintenance |

---

## 4. Credit billing — how the bill is built

A **period bill** collects all confirmed, unbilled slips of a customer between two dates.

The PDF and the portal bill page both show:
1. **Amount payable and due date**, with a status stamp: PAID / PARTIALLY PAID / NOT PAID / OVERDUE. For a partly paid bill, the remaining balance on that bill is also shown.
2. **Account summary:** balance as per last bill − payments received since + other debits + this bill = **total outstanding**. The individual payments are listed with their UTR or cheque number.
3. **Vehicle-wise summary:** fills, litres, amount and share for each vehicle.
4. **Product summary.**
5. **Filling details grouped by vehicle:** date, slip number, product, **odometer**, quantity, rate and amount, with a subtotal per vehicle.
6. **Amount in words**, **How to pay** (UPI QR code and bank details), terms and signatures.

**Payment status:** receipts are applied to the oldest open debits first (FIFO). This gives each bill an *amount paid*, *amount due* and a status. A bill becomes **Overdue** when money is still due after its due date (bill date + credit days).

---

## 5. Fleet & tanker management

- Vehicles are standard **Odoo Fleet** records. Tick **Station Fleet** and choose the role: tanker, bowser or service.
- **Compartments** add up to the tanker's capacity. A trip cannot plan more than that.
- **Dispatch blocks:**
  - the vehicle is already on a trip or in the workshop;
  - an expired insurance, PESO licence, fitness certificate or permit;
  - not enough stock or credit for a delivery.
- **Depot loading:** delivering creates and posts a *Tanker Receipt*. It holds the invoice and received quantity, adds stock and posts the purchase voucher.
- **Doorstep delivery:** delivering issues stock from the source tank and creates a confirmed slip (source = *Doorstep delivery*). It posts Dr Unbilled Clearing / Cr Sales. The slip is billed like any other, and is *not* counted again in the shift sheet.
- **Trip expenses** (toll, driver allowance, loading, parking, repair) are posted to *Fleet – Trip Expenses* when the trip is closed. Cost per km is shown.
- **Fleet fuel logs:** fuel issued from a station tank is valued at the last purchase rate (Dr Fleet Fuel / Cr Purchase), or bought outside for cash. Each log checks the odometer and computes km/L.
- **Maintenance plan:** intervals by km and/or days; next due date and km; ok / due soon / overdue. **Create Job Card** fills in the job.
- **Job card:**
  - **Send to Workshop:** the vehicle shows *In Maintenance* and cannot be dispatched.
  - **Complete & Post Cost:** creates an Odoo Fleet service log, posts the expense (cash, bank or vendor credit), and resets the plan.

---

## 6. AI features

| Feature | Where | How it works |
|---|---|---|
| Stock-out forecast | Dashboard, insights, staff assistant | Average daily issues over the last 14 days (sales + fleet + deliveries) give the days of stock left, the reorder date and a suggested tanker quantity that fits the ullage |
| Suspicious slip detection | Slip list and form, dashboard, customer vehicles page | Flags quantity above the vehicle's tank, the same vehicle filled twice on one day, and km/L below 60% of that vehicle's usual |
| Credit-risk grade A–D | Customer form and list, insights, staff assistant | Weighted score from limit used (40%), days the oldest bill is overdue (40%) and 90-day payment ratio (20%). Suggested limit = average monthly billing × (credit days/30 + 0.5) × a risk factor |
| Customer insights | Portal dashboard | Spend vs last month, next-bill forecast, days of credit left, bills due or overdue, least efficient vehicle, unusual fillings |
| Station insights | Admin dashboard | Stock alerts, risky customers, anomalies, DIP loss above 0.3%, week-on-week sales, fleet documents and maintenance due |
| **AI assistant (customers)** | Portal chat on every page | Balance, credit limit, rates, latest bill and its status, payments and how to pay, statements (PDF/Excel links), consumption by vehicle, product and period, mileage, and **prepared indents** ("Book 200 litres diesel for TS09UB4522" opens a pre-filled indent to review and submit). English language |
| **AI assistant (staff)** | Dashboard "Ask AI" | Sales for a period, stock and days left, credit risk ranking, anomalies, fleet alerts, rates |
| LLM option | Station Settings → AI Engine | *Built-in* (default; no data leaves Odoo), *Anthropic Claude* or *OpenAI* with an API key. The LLM receives only the asking customer's own account summary plus the built-in draft answer. If the call fails, the built-in answer is used |
| Conversation log | Reports → AI Conversations | Every question, answer, intent and engine is recorded for review |

---

## 7. Customer portal (`/my/fuel`)

Customers sign in at `/web/login` (BPCL-branded) and land on their fuel dashboard.

| Page | Contents |
|---|---|
| Dashboard | Outstanding (Dr/Cr), credit limit with a utilisation bar, available credit, unbilled slips, **AI insights**, **bills to pay**, today's rates, recent slips, this month's consumption |
| Ledger Detail | Date range with presets, opening balance, running balance, cheque/UTR, bill links. PDF / Excel / Print |
| Month View | Opening, bills, payments and closing for each month of the financial year, with PDF/Excel per month |
| Statements | Opening, billed, received and closing; bills and payments in the period. PDF / Excel |
| Filling Bills | Status badge, balance due, due date. **Bill page** with the amount due, how the amount is made up, slips by vehicle, and a **UPI QR code** |
| Filling Slips | Filter by date, vehicle, product and status. Excel export. Printable slip |
| My Vehicles | Litres and spend this month, average km/L, last fill, 6-month bar chart, AI warnings |
| Indent Request | Quantity / amount / tank full, checked against available credit. Can be pre-filled by the AI assistant |
| Fuel Rates | Current rates and changes over the last 60 days |
| AI Assistant | Floating chat with quick-question chips and action links |

The portal is responsive: on a phone the side menu collapses and the chat goes full width. A customer can only see their own data; anything else returns 404 or an access error.

---

## 8. Accounting model

| Event | Debit | Credit |
|---|---|---|
| Shift posted | Cash, Card/UPI/Fleet settlement, Unbilled Credit (clearing) | Sales per product (short/excess → Cash Short/Excess) |
| Doorstep delivery | Unbilled Credit (clearing) | Sales – product |
| Period bill | Sundry Debtors (customer) | Unbilled Credit (clearing) |
| Customer receipt | Bank / Cash | Sundry Debtors |
| Tanker receipt | Purchase – Fuel | Bank or Sundry Creditors |
| Fleet fuel (station) | Fleet – Fuel Consumption | Purchase – Fuel |
| Job card | Fleet – Repairs & Maintenance | Cash / Bank / Creditors |
| Trip close | Fleet – Trip Expenses | Cash |
| Card/UPI settlement | Bank + Bank & Card Charges | Card/UPI settlement |
| Salary | Salaries & Wages | Cash/Bank (+ Staff Advances recovered) |

Every voucher must balance. Automatic vouchers are cancelled only through their source document. The trial balance always nets to zero (this is checked in the tests).

---

## 9. Security & roles

| Group | Rights |
|---|---|
| Operator / Cashier | Daily entries, slips, receipts, DIP, trips, fuel logs, job cards. Read access to masters. Fleet user |
| Manager / Owner | Everything, including credit limits, rates, billing cancellation, journals, trial balance, payroll, portal logins, AI log and AI settings. Fleet administrator |
| Portal customer | Their own account only, through `/my/fuel`. AI and ledger services refuse direct RPC calls from portal users |

The AI API key is visible only to system administrators.

---

## 10. Reports & exports
- **PDF reports:** credit bill (with QR), customer statement, filling slip (A6), DSR, trial balance.
- **Excel exports:** customer ledger, filling slips.
- **Pivots and graphs:** sales by day, product, nozzle and attendant; credit sales; stock summary; account balances; attendance; rate trend.

---

## 11. Demo data & logins (`--with-demo`)
The demo covers 75 days for a fictional outlet, *Sri Lakshmi Narasimha Fuels*:

- 8 fictional credit customers with 18 vehicles;
- 4 tanks and 6 nozzles;
- about 225 shift sheets, about 380 slips and about 34 bills;
- tanker trips, doorstep deliveries, fleet fuel logs, 3 job cards and documents (one PESO licence expiring, one PUC expired);
- salaries;
- planted anomalies (a double fill and a mileage drop) for the AI to find.

| Role | Login | Password |
|---|---|---|
| Admin | `admin` | `admin` |
| Customer: Konark Roadlines | `konark` | `Konark@123` |
| Customer: Vayu Cabs & Travels | `vayu` | `Vayu@123` |

---

## 12. Tests
```bash
./odoo-bin -c odoo.conf -d test_db -i zivoit_fuel_management --test-tags /zivoit_fuel_management --stop-after-init
```
Result on Odoo 19.0: **28 tests, 0 failed, 0 errors**. See `TEST_REPORT.md` for the list.

---

## 13. Technical reference

| Model | Purpose |
|---|---|
| `pump.product`, `pump.rate` | Products and rate history |
| `pump.tank`, `pump.dip.chart`, `pump.nozzle`, `pump.dip.reading`, `pump.stock.move` | Tanks, nozzles, DIP and stock |
| `pump.shift`, `pump.shift.sheet(.line)` | Shifts and nozzle readings |
| `pump.credit.sale`, `pump.indent`, `pump.vehicle` | Slips, indents, customer vehicles |
| `pump.bill` | Period bills (payment status, summaries, QR) |
| `pump.account`, `pump.voucher(.line)` | Ledger |
| `pump.purchase` | Tanker receipts |
| `fleet.vehicle` (extended), `pump.tanker.compartment`, `pump.vehicle.document`, `pump.tanker.trip`, `pump.trip.expense`, `pump.fleet.fuel.log`, `pump.maintenance.schedule`, `pump.maintenance.order(.line)` | Fleet |
| `pump.ai`, `pump.ai.log`, `pump.dashboard` | AI engine, log and dashboard service |
| `hr.employee` (extended), `pump.attendance`, `pump.salary.sheet(.line)` | Staff |

**Portal routes:** `/my/fuel`, `/ledger(/pdf|/xlsx)`, `/monthly`, `/statement`, `/bills`, `/bill/<id>(/pdf)`, `/slips(/xlsx)`, `/slip/<id>/pdf`, `/vehicles`, `/indent`, `/rates`, `/ai/ask` (POST).

**Admin export route:** `/fuel/ledger/xlsx/<partner_id>`.

**Scheduled job:** *Fuel Station: fleet document & maintenance alerts* (daily).

---

## 14. Limits & roadmap
- One station per database. Multi-outlet is on the roadmap.
- Lubricants are sold on slips; stock is tracked for tank fuels only.
- GST/VAT return filing, SMS/WhatsApp, automatic tank gauges (ATG) and BPCL SAP/SmartFleet integrations are not included. They can be added as separate modules.
- The external LLM path depends on the customer's own API key and internet access. It is optional and falls back to the built-in engine automatically.
