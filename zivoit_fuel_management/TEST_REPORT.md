# Test Report — ZivoIT Fuel Management System (Odoo 19)

| Item | Value |
|---|---|
| Odoo | 19.0 (community source, branch 19.0) |
| Python / PostgreSQL | 3.13 / 16 |
| Command | `odoo-bin -d bpcl_test -i zivoit_fuel_management --without-demo=all --test-tags /zivoit_fuel_management` |
| Result | **28 tests — 0 failed, 0 errors** |

## Automated tests

| # | Suite | Test | What it proves |
|---|---|---|---|
| 1 | TestFleetAi | `test_01_capacity_and_documents` | Tanker capacity from compartments; document status & search; expiry alert activities |
| 2 | TestFleetAi | `test_02_depot_trip_creates_receipt_and_expenses` | Depot trip → tanker receipt & stock; double dispatch blocked; expenses posted; odometer/distance |
| 3 | TestFleetAi | `test_03_capacity_constraint_and_blocking_documents` | Trip above capacity rejected; expired PESO blocks dispatch |
| 4 | TestFleetAi | `test_04_doorstep_delivery` | Delivery issues stock, creates delivery slip, books sale; not double counted in shift; bill clears clearing account |
| 5 | TestFleetAi | `test_05_fleet_fuel_log` | Fleet fuel at last purchase cost; km/L; stock out; odometer regression rejected |
| 6 | TestFleetAi | `test_06_maintenance_schedule_and_job` | Plan due by km; workshop blocks dispatch; job posts cost, writes Fleet service log, resets plan; empty job rejected |
| 7 | TestFleetAi | `test_07_bill_payment_status_fifo` | FIFO payment allocation: paid / partial / overdue; account summary; vehicle summary; bill PDF sections & UPI QR |
| 8 | TestFleetAi | `test_08_ai_anomalies_and_risk` | Same-day double fill flagged; over-tank quantity blocked; risk score/grade range |
| 9 | TestFleetAi | `test_09_ai_stock_forecast_and_insights` | Stock-out forecast danger level and station insight |
| 10 | TestFleetAi | `test_10_ai_assistant_customer` | Assistant intents: balance, vehicle consumption, indent prefill, rates, plain-English balance question, statement links, fallback, staff stock answer, logging |
| 11 | TestFleetAi | `test_11_ai_access_control` | Portal users cannot call AI / dashboard services or read another customer ledger; own ledger allowed |
| 12 | TestPortal | `test_login_redirect_and_unlinked` | Branded login page; internal user without fuel account sees friendly page |
| 13 | TestPortal | `test_portal_downloads_and_isolation` | Excel downloads; other customer bill/slip PDFs return 404 |
| 14 | TestPortal | `test_portal_indent` | Indent POST with CSRF creates a portal indent |
| 15 | TestPortal | `test_portal_pages` | All portal pages render for the customer; only own bills listed |
| 16 | TestPortal | `test_portal_v2_pages_and_ai` | Bill detail page & isolation; vehicles page; AI-prefilled indent; AI chat endpoint returns answer |
| 17 | TestPumpFlow | `test_01_rate_lookup_and_slip_amount` | Rate history lookup by date; slip amount; slip and customer sequences |
| 18 | TestPumpFlow | `test_02_credit_limit_enforced` | Credit limit blocks slips; unbilled/available credit; manager override |
| 19 | TestPumpFlow | `test_03_shift_sheet_posts_stock_and_cash` | Shift posting: totals, expected cash, short/excess, clearing, tank stock, meter sequence, cancel restores stock |
| 20 | TestPumpFlow | `test_04_reading_validation` | Closing < opening and testing > metered are rejected |
| 21 | TestPumpFlow | `test_05_bill_receipt_and_ledger` | Bill generation, slip locking, receipt, ledger running balance, opening carry-forward, month view, bill cancel |
| 22 | TestPumpFlow | `test_06_vouchers_balanced` | Unbalanced / partyless vouchers rejected; posted vouchers undeletable; trial balance nets to zero |
| 23 | TestPumpFlow | `test_07_purchase_and_dip` | Tanker receipt stock & bank; capacity guard; DIP variance posting |
| 24 | TestPumpFlow | `test_08_dip_chart_interpolation` | DIP chart mm→litre interpolation and bounds |
| 25 | TestPumpFlow | `test_09_indent_to_slip` | Indent by amount converts to slip quantity; indent delivered on confirm |
| 26 | TestPumpFlow | `test_10_salary_and_settlement` | Salary from attendance (paid days); card settlement with MDR |
| 27 | TestPumpFlow | `test_11_dashboard_and_reports` | Dashboard data; statement and bill PDFs render; Excel ledger is a valid XLSX |
| 28 | TestPumpFlow | `test_12_only_manager_changes_limit` | Cashier cannot change credit limits |

## Manual / browser verification (Chromium, headless)

- Admin: dashboard loads with AI insights, Ask AI answers; fleet, trips, job cards, documents, bills, customers, slips, fuel logs, maintenance plan, AI log, tanker and trip forms open — **no JavaScript console errors**.
- Portal (desktop 1366 px and mobile 390 px): dashboard, bills, bill detail, vehicles, AI chat (3 questions), AI-prefilled indent (qty 200 carried over), ledger, month view, statement, slips, rates — all HTTP 200, **no console errors**.
- PDFs: credit bill (with UPI QR), statement; Excel: ledger and slips open as valid XLSX.
- Accounting check on 75-day demo: trial balance debits = credits; card/UPI settled to bank T+1.
- Security: customer A receives 404 for customer B's bill and slip; portal user RPC to AI/dashboard raises AccessError.

## Release 19.0.2.0.1 checks
- **App Store scanner simulation:**
  1. Built a Git repository with branch `19.0` and the module at the root.
  2. Cloned that branch fresh.
  3. Installed and tested from the clone: **28 tests, 0 failed, 0 errors**.
- **Install log:** clean install with and without demo data; **0 warnings, 0 errors**.
- **Uninstall:** uninstalled with demo data present (845 posted vouchers, 34 posted bills); the module reached the *uninstalled* state.
- **pylint-odoo (Odoo 19 rules):** no errors. The only remaining findings are OCA-specific rules (OCA author, LGPL licence), which do not apply to paid App Store apps, plus cosmetic style notes.
- **Strings:** translation template re-exported; every string is English.

## Bugs found and fixed during testing

- A local variable named `cr` shadowed Odoo's cursor lookup inside the translation helper, so the "voucher not balanced" message crashed instead of showing. Renamed throughout.
- Non-stored computed balances (tank stock, nozzle last reading, partner balances, fleet odometer) could be stale inside one transaction. Explicit cache invalidation was added.
- Odoo 19 passes set-type values to custom search methods. The document-status search now accepts any iterable.
- The UPI QR code needed ReportLab's optional PNG backend, so it was missing on some servers. It now uses the `qrcode` library, with ReportLab as a fallback.
- The bill page headline showed the bill-date position. It now shows the current balance due, with the bill-date breakdown labelled.
- AI public methods use elevated access internally, so they are now guarded against direct portal RPC calls. Customer ledger methods are restricted to the owner.
