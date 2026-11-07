# ZivoIT EPC AI — EPC Project Controls with AI for Odoo 19

**Technical name:** `zivoit_epc_ai` · **Version:** 19.0.2.0.2 · **Licence:** OPL-1 · **Author:** Zivo IT Technologies

An Odoo 19 vertical for Engineering, Procurement & Construction (EPC) contractors in power,
transmission & distribution, renewables, infrastructure, oil & gas and industrial projects. It covers
the five areas EPC buyers ask AI for — **project management, risk prediction, design optimisation,
supply chain and site operations** — and runs in two deployment modes:

| Mode | Typical customer | How it is used |
| --- | --- | --- |
| **End-to-end on Odoo** | Mid-size EPC contractors (₹100–2,000 cr turnover) | Odoo is the ERP: projects, BOQ, purchase, inventory, billing and accounting, with the AI layer on top. |
| **AI overlay** | Large EPC companies running SAP / Oracle + Primavera P6 | Odoo is the project-controls and AI layer. Schedules come from P6 (XER), material status from SAP/Oracle (REST API or CSV), and forecasts flow back through the API. Multi-company, portfolio views and API keys per integration user. |

Works on **Odoo 19 Community and Enterprise** (Odoo.sh or on-premise; Odoo Online does not allow custom code).

---

## 1. What it does

### Project management
* EPC project data: contract value, LD rate & cap, retention, advance recovery, system of record and external project ID.
* WBS activities with FS/SS/FF/SF links and lag; **CPM engine** on a **working calendar** (7/6/5-day week, company and project holidays): early/late dates, total float, critical path; baselines.
* Earned value (BAC, PV, EV, AC, SPI, CPI) at activity and project level.
* **Schedule optimiser**: least-cost crashing to a target finish date plus fast-tracking suggestions; apply in one click.
* Primavera **P6 XER import and export**.
* **Portfolio** list, pivot and graph across projects and companies (stored KPI snapshot refreshed by every prediction run).

### Risk prediction
* Forecast start/finish for every activity from measured progress rate, logic, material arrival (Odoo POs *and* ERP materials), drawing readiness, open major NCRs and learned duration overruns.
* Delay probability (transparent logistic model) with drivers in plain words; project forecast finish, EAC P50/P80, LD exposure.
* **Monte Carlo simulation** (seeded, reproducible): finish P50/P80/P90, chance to meet baseline, criticality index per activity.
* Duration overrun factors **learned per discipline** from completed work; **supplier risk learned** from each vendor's delivery history.
* Early-warning risks and to-dos raised and auto-closed by the nightly run; contract risk review of GCC/SCC text.

### Design optimisation
* **AI material take-off (MTO)** from drawings: attach a PDF/text BOM or a drawing image; the LLM (or the built-in BOM parser) extracts item, description, quantity and unit and maps lines to BOQ items.
* **BIM import (IFC 2x3 / IFC4)** from Revit, Tekla, AVEVA, Plant 3D: physical elements grouped by type with volume / weight / length / area from the model's quantity sets.
* **Design vs BOQ variance** on every BOQ item (flags possible variations early).
* **Change orders / variations** with **AI impact assessment**: quantity and new-item changes priced from the BOQ, schedule changes run through CPM to give the true finish-date impact (float absorbed), manager approval updates BOQ, durations, schedule and contract value.
* Engineering document register (IFR → IFA → AFC, revisions, transmittals with AI cover notes).

### Supply chain
* MTO → RFQs grouped by vendor, or → **bid package**.
* **Technical & commercial bid evaluation (TBE/CBE)**: AI reads each vendor offer (PDF/text) for unit prices, total, delivery, deviations, payment terms and validity; weighted scoring of price, delivery (penalised if it misses the site need date), learned supplier risk and technical compliance with a technical gate; AI award recommendation; award creates the purchase order linked to activities.
* Expediting board: required-at-site date, forecast delivery, predicted delay, status; AI-drafted vendor follow-ups.
* **External ERP materials**: SAP/Oracle PO lines mirrored by API or CSV, forecast with the same supplier logic and fed into the schedule forecast and early warnings.

### Site operations
* Daily Progress Report: measured quantities, manpower, plant hours, hindrances, weather; **AI Draft DPR** from typed or dictated notes; **AI photo review** (vision model) for progress and safety observations.
* HSE: permits to work with AI precautions checklists and expiry; incidents with AI root cause; safety observations; man-hours, LTIFR, safe man-hours.
* Quality: inspection requests (ITP), automatic NCR on rejection with AI disposition suggestion, punch list (A/B/C).
* Plant & equipment utilisation/availability; breakdowns become hindrances automatically.

### Commercial and governance
* Bill of Quantities; **RA billing** from measured quantities with certification, retention and advance recovery, one-click invoice.
* "Ask AI" assistant on live project data; AI progress report; PDF reports (RA bill, progress report, DPR).
* Every AI call logged; human approval on all AI output; two roles; multi-company record rules.
* **REST API** secured with Odoo API keys (see §6) and CSV import templates for BOQ, activities, MTO and ERP materials.

---

## 2. Installation

1. Copy the `zivoit_epc_ai` folder into your custom addons path.
2. Python dependencies: `requests` and `pypdf` (both already required by Odoo 19). No other packages.
3. Restart Odoo, update the Apps list, install **ZivoIT EPC AI**. Project, Purchase, Inventory, Accounting and Discuss install automatically.
4. Give users a role in *Settings → Users → EPC Project Controls*:
   * **User** — site and project staff (DPRs, activities, documents, HSE, quality, MTO, change-order drafts, ERP materials).
   * **Manager** — approves DPRs and change orders, evaluates and awards bids, certifies RA bills, runs predictions, configures AI.

```bash
./odoo-bin -c odoo.conf -d <db> -i zivoit_epc_ai
# run the module's tests (database without demo data)
./odoo-bin -c odoo.conf -d <test_db> -i zivoit_epc_ai --test-tags /zivoit_epc_ai --stop-after-init
```

---

## 3. AI configuration

*ZivoIT EPC AI → Configuration → Settings → ZivoIT EPC AI*

| Provider | What to enter |
| --- | --- |
| Built-in engine only (default) | Nothing. Every feature works offline with deterministic rules (photo review and image drawings need a vision model). |
| OpenAI | API key; model optional (default `gpt-4o-mini`). |
| Anthropic Claude | API key; model optional (default `claude-sonnet-5-5`). |
| OpenAI-compatible | Base URL (Ollama, vLLM, Azure OpenAI gateways); key and model as required. Use this when data must stay on-premise. |

Press **Test Connection**. If the provider fails or returns unusable output, the built-in engine answers
and the error is recorded in *AI & Risk → AI Audit Log*. The numeric engines (CPM, prediction, Monte Carlo,
supplier risk, bid scoring, schedule optimiser, change-order time impact) run inside Odoo and never send data to an LLM.

---

## 4. Typical flow

1. **Project** — *Projects → New*, tick *EPC Project*, fill *EPC Contract* (or run a Contract Risk Review → Apply).
2. **Scope & schedule** — BOQ and activities (CSV templates, or **Import P6**); **Compute Schedule**, **Set Baseline**.
3. **Design** — attach drawings → **AI Extract MTO**, or **Import BIM (IFC)**; check *Design vs BOQ*; raise **Change Orders** → **AI Assess Impact** → Submit → Approve.
4. **Procurement** — confirm MTO lines → **Create RFQs** or **Create Bid Package**; add bids → **AI Read Offer** → **Evaluate Bids** → **Award**. Large companies push SAP/Oracle PO status to *External ERP Materials*.
5. **Site** — daily DPR (AI Draft DPR, photos → AI Photo Review), permits, incidents, inspections, NCRs, punch list, plant hours.
6. **Every night** predictions, Monte Carlo and early warnings refresh; managers can press **Run AI Prediction** at any time; **Optimise Schedule** to recover delays.
7. **Review & bill** — AI Insights, Portfolio, Early Warnings, Ask AI, AI Progress Report; RA bills → invoice.

---

## 5. Engines in brief

* **Forecast**: completed = actuals; in progress = remaining % ÷ measured rate; not started = latest of baseline, predecessor forecasts (by link type and lag), status date, material arrival (+1 day), drawing AFC; duration × learned P50 factor. All on the working calendar.
* **Delay probability**: logistic model of slip, criticality, progress gap, late material, late documents, recent hindrances, supplier risk and open major NCRs (`WEIGHTS` in `models/epc_prediction.py`).
* **Monte Carlo**: triangular durations (0.9 × d, P50 × d, P80-derived tail) through the network with all readiness constraints; iterations set in settings (default 500).
* **Supplier risk** 0–100 from on-time rate and average delay, shrunk toward 30 when history is thin.
* **Bid score** = price (best ÷ price) × w₁ + delivery (best ÷ lead, −3 points per day past the need date) × w₂ + (100 − supplier risk) × w₃ + technical compliance × w₄; bids below the technical minimum are disqualified.
* **Change-order time impact** = CPM finish with proposed durations − current CPM finish (working days).

---

## 6. REST API (enterprise integration)

Create an API key for an integration user with the EPC role (*Preferences → Account Security → New API Key*)
and send it as `Authorization: Bearer <key>`. Base path `/api/zivoit_epc/v1`. JSON in and out; record rules apply.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/projects?external_ref=…&contract_ref=…` | EPC projects with KPIs and forecasts |
| GET | `/projects/<id>` | One project |
| GET | `/projects/<id>/activities` | Activities with CPM, forecast, probability, criticality, drivers |
| GET | `/projects/<id>/risks` | Open risks |
| POST | `/projects/<id>/materials` | Upsert ERP material status `{"materials": [{source_system, external_ref, description, quantity, delivered_qty, uom, vendor_ref, vendor_name, promised_date, stage, activity_codes}]}` |
| POST | `/projects/<id>/progress` | `{"date", "lines": [{activity_code, quantity}], "manpower": [{trade, count}], "submit": true, "activities": [{code, actual_start, actual_finish, progress}]}` — creates a DPR and/or updates activities (codes or external IDs) |
| POST | `/projects/<id>/predict` | Run the prediction now (manager key) |

Errors: 400 bad JSON, 401 bad key, 403 missing role, 404 unknown project, 422 validation.

---

## 7. Technical overview

| Model | Purpose |
| --- | --- |
| `project.project` (extended) | EPC fields, EVM, prediction and Monte Carlo results, KPI snapshot, HSE/quality KPIs, external refs |
| `epc.boq.line` | BOQ, executed/billed and design (MTO) quantities |
| `epc.activity`, `epc.activity.link`, `epc.holiday` | Schedule network, CPM, calendar |
| `epc.dpr` (+ line, manpower, hindrance, plant) | Daily progress reports |
| `epc.hse.permit`, `epc.hse.incident`, `epc.hse.observation` | HSE |
| `epc.qa.inspection`, `epc.qa.ncr`, `epc.punch.item`, `epc.plant` | Quality and plant |
| `epc.document` (+ revision, transmittal) | Document control, drawing files |
| `epc.mto.line`, `epc.bim.import` | Material take-off and IFC import |
| `epc.change.order` (+ boq, activity) | Change orders / variations |
| `epc.bid.package` (+ line), `epc.bid` (+ line) | Bid evaluation |
| `epc.external.material` | ERP material status |
| `purchase.order.line`, `res.partner` (extended) | Expediting, supplier risk |
| `epc.ra.bill`, `epc.contract.review`, `epc.risk` | Commercial, contract and risk |
| `epc.prediction.engine`, `epc.prediction.model` | Prediction engine and learned factors |
| `epc.ai.service`, `epc.ai.log`, `epc.api` | LLM gateway, audit log, API service |
| Wizards | P6 import/export, AI assistant, schedule optimiser, BIM import |

Sequences: `DPR/`, `RAB/`, `TRN/`, `PTW/`, `INC/`, `IR/`, `NCR/`, `CO/`, `BID/`. Crons: daily predictions, weekly learning.

**Tests:** 100 automated tests in `tests/` (CPM and calendars, EVM, DPR, prediction, Monte Carlo, optimiser,
supply chain and ERP materials, bid evaluation, MTO/BIM/change orders, HSE/quality/plant, photo review,
RA billing, AI provider calls (mocked), contract review, P6, documents, REST API over HTTP, access rights and multi-company).

---

## 8. Demo data

Installing with demo data creates a 400 kV D/C transmission line (behind plan, late conductor supplier,
overdue stringing chart) linked to a fictional SAP project, a completed 132 kV project the engine learns from,
DPRs, a certified RA bill, an analysed contract, permits, an incident, an inspection with NCR, punch items,
a crane with a breakdown, a drawing with an extracted MTO, a change order with AI assessment, an evaluated
bid package, and two SAP-tracked materials. All names are fictional.

---

## 9. Known limits

* The built-in DPR and BOM parsers understand English; other languages and image-only drawings need an LLM provider.
* IFC import reads quantities from IFC quantity sets; models exported without quantities are counted by element.
* The REST API is pull/push over HTTPS; scheduled pulls from SAP/Oracle are done by the customer's middleware (SAP CPI, Oracle Integration Cloud, MuleSoft, etc.).
