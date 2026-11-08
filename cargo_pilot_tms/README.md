# Cargo Pilot TMS (Odoo 19), single module

Transport management for shippers and transporters: indents, spot auctions, RFQ rate contracts,
load builder, trips with live tracking, ePOD, freight audit, consignment notes (LR), PTL challans
and hub stock, market-vehicle hire slips, trip P&L, fuel, maintenance, tyres, compliance documents,
driver safety and plant gate/yard.

## Install
1. Copy the `cargo_pilot_tms` folder into your addons path (this one folder holds everything).
2. Update the apps list and install **Cargo Pilot TMS**. It pulls in the standard Odoo apps it uses:
   Sales, Inventory (with delivery), Purchase, Invoicing, Fleet, Portal, SMS and Discuss.
3. Give users the *Cargo Pilot TMS / User* or *Administrator* group, and the Dispatch roles
   (Sales Coordinator, Gate Security, Plant Operations Head) where needed.

## Upgrading from the separate Cargo Pilot modules (19.0.3.0.0)
Until 19.0.2.x Cargo Pilot shipped as eight modules (`cargo_pilot_tms` plus `_courier`, `_courier_delivery`,
`_india`, `_zoho`, `_easyecom`, `_stock`, `_dispatch`). They are now one module.
1. Back up the database.
2. Remove the seven old add-on folders from the addons path and put this `cargo_pilot_tms` in place of the old one.
3. Upgrade **Cargo Pilot TMS** (Apps > Upgrade, or `-u cargo_pilot_tms`). A migration step hands every record,
   field, view and access rule of the old modules to `cargo_pilot_tms`; nothing is recreated or lost.
   The old modules then show as not installed. Expect one "manifest not found" warning per old module
   on that first start; it does not come back.

## Set up
- Setup > Locations (add coordinates for geofencing), Lanes, Truck Types, Docks.
- Plan > Transporters; grant portal access so they can bid, book dock slots and upload delivery proof. Give customers portal access to request trucks.
- Settings > Cargo Pilot TMS: audit tolerances, auction rules, stoppage alert, API key.

## Odoo 19 edition (19.0.1.0.0)
- Security uses Odoo 19 privileges (`res.groups.privilege`), `user_ids` / `group_ids`.
- Unique rules rewritten as `models.Constraint`; JSON routes use `type='jsonrpc'`.
- Portal, driver and tracking pages rebuilt as Odoo 19 frontend Interactions (`public.interactions`).
- Partner `mobile` replaced by `phone`; search views and actions updated to the Odoo 19 schema.
- Installs with zero warnings on Odoo 19.

## 2.0: shipper features (19.0.2.0.0)
- **FTL + LTL**: shipment mode on bookings, load requests and shipments; LTL by docket and carrier tracking; intrastate / interstate lanes.
- **Rate cards**: per trip, ton, km, kg or box; minimum weight / charge, docket, ODA, FOV %, fuel surcharge, multi-drop, transit days, weekly capacity; zone lines by state; diesel escalation clause with a diesel price list; peak-season surcharges.
- **Transporter acceptance**: offers with a deadline, accept / decline (portal, link `/cp/offer/<token>`, WhatsApp with Cargo Pilot India), cascade to the next contract, then bidding.
- **Orders on shipments**: order, invoice, customer, value, serials, e-way bill; freight split by weight / volume / value / units / equal; follows the audited bill.
- **Channel rules**: delivery promise, appointment, GRN and serial scan per channel or key account; approved transporters.
- **ePOD**: serial scan (camera) with missing / extra serials, GRN number, POD ageing buckets and turnaround.
- **Appointments and containers**: retail DC slots with at-risk alerts; import container free days, detention and empty return.
- **Scorecard** (acceptance 15, placement 20, on-time 25, damage-free 15, POD in 48 h 10, billing accuracy 15) and **capacity plan**.
- **Freight Insights** dashboard; **REST API v2** with OpenAPI; signed **webhooks** with retries.

## Features
- **Today** home screen: setup checklist, live counts, issues inbox, live map, CO2 this month. Works on phones.
- **Issues** inbox: late/stopped trucks, delivery proof problems, invoice overcharges, expiring papers, fuel anomalies and unsafe driving open automatically with a suggested fix and close themselves when the cause clears.
- **Book a Load**: compare contract prices and the bidding option (price, transit, on-time score, CO2) and book in one click.
- **Customer portal**: customers request trucks and follow deliveries at `/my/cp/shipments`.
- **Dock appointments**: transporters pick their own loading slot from the portal.
- **CO2e per shipment** (tonne-km x emission factor per truck type).
- **Milestone tracker** on shipments, mobile card views everywhere, installable driver app, Leaflet maps.
- Plain-language menus: Today, Plan, Move, Carrier Office, Fleet, Pay, Insights, Setup.

## Main flows
- **Shipper:** Book a Load (or Load Request) > contract truck or Load Bidding > Shipment > tracking > Delivery Proof > Invoice Check > Vendor Bill.
- **Transporter:** Booking (LR) > Hub Transfer or FTL Shipment > Delivery > Invoice; Hired Trucks for market vehicles.

## Public URLs
- `/my/cp/auctions`, `/my/cp/trips`, `/my/cp/rfqs`, `/my/cp/bills` transporter portal
- `/my/cp/shipments` customer portal (request a truck, follow deliveries)
- `/cp/track/<token>` customer tracking, `/cp/driver/<token>` driver page

## API (header `X-CP-API-Key`)
- v1: `POST /cp/api/v1/ping`, `POST /cp/api/v1/event`, `POST /cp/api/v1/indent`, `GET /cp/api/v1/trip/<name>`
- v2: `GET|POST /cp/api/v2/shipments`, `GET /cp/api/v2/shipments/<name>`, `POST .../events`, `POST .../pod`,
  `GET /cp/api/v2/orders/<ref>/freight`, `GET /cp/api/v2/freight-bills`, `GET /cp/api/v2/transporters/scorecard`,
  `GET /cp/api/v2/openapi.json` (no key)
- Webhooks: Setup > Webhooks; header `X-CP-Signature: sha256=<HMAC of the body>` and `X-CP-Event`.

## Tests
```
odoo-bin -d <db> -i cargo_pilot_tms --test-tags /cargo_pilot_tms --stop-after-init
```

## Third-party
Leaflet 1.9.4 (BSD-2-Clause) is bundled in `static/lib/leaflet`.

## Licence and price
OPL-1, USD 599 on the Odoo App Store (`price` / `currency` in `__manifest__.py`). One purchase covers every feature
in this file. Author: ZivoIT Technologies, https://www.zivoit.com, support: info@zivoit.com.

## Cargo Pilot Courier
Courier (parcel) shipping for Cargo Pilot TMS: compare prices, book AWBs, print labels, request pickups,
track every parcel, act on failed deliveries and reconcile cash on delivery.

### Couriers
| Account type | How it connects | Prices | Label | Tracking | Cancel | Pickup | Failed-delivery actions | Push updates |
|---|---|---|---|---|---|---|---|---|
| Delhivery | API token | API | API | API | API | API | Re-attempt | Webhook |
| Blue Dart | DHL/Blue Dart API gateway (JWT + Login ID / Licence Key) | Rate card + transit time API | API | API | API | API | Manual | Polling |
| DTDC | Booking API key + tracking user | Rate card | API | API | API | Manual | Manual | Webhook |
| Shiprocket | API user (25+ couriers incl. Delhivery, Blue Dart, DTDC, Xpressbees, Ekart) | API, per courier | API | API | API | API | Re-attempt / return | Webhook |
| Xpressbees | Account e-mail / password | API | API | API | API | Auto at booking | Manual | Webhook |
| ClickPost | Username + API key + courier partner id (Shree Maruti = 69, Professional, Trackon, Gati...) | Rate card | API | API | API | Manual | Manual | Webhook |
| Counter / phone booking | Type the docket number or use an AWB number series (any courier, e.g. Shree Maruti direct) | Rate card | Cargo Pilot 4x6 label | Manual updates | Manual | Manual | Manual | - |
| Demo courier | Built-in simulator | Simulated | Cargo Pilot label | Simulated | Yes | Yes | Yes | Yes |

Shree Maruti Courier has no public API of its own. Connect it through ClickPost (partner id 69), or use a
counter booking account with your docket book as the AWB series.

### Set up
1. Install **Cargo Pilot Courier** (needs Cargo Pilot TMS). Install **Cargo Pilot Courier for Sales & Inventory**
   as well if you ship from Odoo delivery orders (it installs itself when Inventory is present).
2. Setup > Courier Accounts > New. Choose the courier, paste the credentials, set the pickup address
   (with PIN code and phone) and press **Test connection**.
3. Keep **Test / sandbox** until a test booking works, then **Switch to live**.
4. Fill in the **Rate card** tab for couriers without a price API.
5. Optional: copy the **webhook address** from the Tracking tab into the courier's dashboard for instant updates.
   Tracking also refreshes every two hours.

### Daily use
- **Move > Send a Parcel**: receiver, boxes, payment mode, then **Compare & book**. The best mix of price,
  speed and rating is starred; cheapest and fastest are tagged.
- Print the courier label (or the Cargo Pilot 4x6 label), request a pickup for many parcels at once and print
  the handover sheet.
- Failed delivery opens an issue in **Move > Issues**. Choose *Deliver again* or *Return to sender*.
- Late parcels, returns and lost parcels open issues too and close themselves when the parcel moves on.
- **Pay > COD to Receive** lists delivered COD parcels until you mark the courier's remittance as received.
- Bookings (LR) have a **Send by courier** button. The customer gets a branded tracking page.
- With the Sales & Inventory add-on: shipping methods of type *Cargo Pilot courier* price quotations and the
  website checkout, and validating a delivery order books the AWB and attaches the label.

### Notes on the APIs
Endpoints follow each courier's published API documentation (Delhivery developer portal, DHL/Blue Dart API
gateway, DTDC/Shipsy, Shiprocket, Xpressbees, ClickPost). Couriers sometimes give accounts a different host or
field set; the **API address** and **Courier tracking page** fields on the account let you adjust without code,
and the **Courier API Log** shows each request and answer with secrets masked. The automated tests use recorded-
style answers; run a sandbox booking with your own credentials before going live.

### Tests
```
odoo-bin -d <db> -i cargo_pilot_tms --test-tags /cargo_pilot_tms --stop-after-init
```

## Cargo Pilot Courier for Sales & Inventory
Bridge between Cargo Pilot Courier and Odoo's shipping methods (`stock_delivery`).

1. Inventory > Configuration > Shipping Methods > New, Provider **Cargo Pilot courier**.
2. Courier tab: choose the courier account, the pick rule (best value / cheapest / fastest) and the COD rule.
3. Quotations get prices from the courier; validating the delivery order books the AWB, attaches the label
   and sets the tracking reference. The tracking link points to the branded Cargo Pilot parcel page.

## Cargo Pilot India
India connectors for Cargo Pilot TMS.

| Connector | What it does | Calls |
|---|---|---|
| E-way bill (GSP) | Part-B vehicle update on assignment, validity extension before expiry, consolidated EWB | NIC e-way bill JSON (`ewbNo`, `vehicleNo`, `fromPlace`, `fromState`, `reasonCode`, `transMode`, `extnRsnCode`, `tripSheetEwbBills`) to your GSP's paths |
| ULIP FASTag | Toll plaza crossings become positions (source `fastag`) | `POST /user/login`, `POST /FASTAG/01 {vehiclenumber}` |
| ULIP Vahan | Fitness, insurance, PUC, permit, tax and RC status on assignment; problems open a compliance issue | `POST /VAHAN/01` (XML answer) |
| SIM tracking | Driver consent by SMS, then network location | Configurable paths and JSON keys |
| WhatsApp | Offer, driver link, dispatched, delivered, POD reminder templates; delivery receipts | `POST graph.facebook.com/{ver}/{phone_number_id}/messages`; webhook `/cp/whatsapp/webhook` |

### Set up
Setup > India Integrations. Each connector has a **Simulator** mode for testing without credentials.

### Confirm with live credentials
- GSP base URL, auth headers and paths differ per GSP (ClearTax, Masters India, IRIS, Logitax...). Defaults: `/updatevehicle`, `/extendvalidity`, `/consolidated`.
- ULIP access needs approval on goulip.in; FASTag times are read as IST.
- SIM providers each document their own paths and field names.
- WhatsApp templates must be approved in WhatsApp Manager with the same number of `{{n}}` values.

### Tests
`odoo-bin -d <db> -i cargo_pilot_tms --test-tags /cargo_pilot_tms --stop-after-init

## Cargo Pilot Zoho
Zoho Books and Zoho Creator for Cargo Pilot TMS.

- OAuth with a refresh token (`https://accounts.zoho.<dc>/oauth/v2/token`), all data centres.
- Books (`/books/v3`, header `Authorization: Zoho-oauthtoken`):
  - transporters: `GET/POST /contacts` (contact_type `vendor`, `gst_no`, `gst_treatment`, `place_of_contact`)
  - approved freight bills: `POST /bills` (vendor_id, bill_number, line_items with account_id / tax_id / `hsn_or_sac`,
    `is_reverse_charge_applied`, `tds_tax_id`); option to skip the Odoo vendor bill
  - freight per order: `GET /invoices?invoice_number=` then `PUT /invoice/{id}/customfields`
- Creator (`/creator/v2.1/data/{owner}/{app}`): `POST /form/{form}` once, then `PATCH /report/{report}/{ID}`;
  configurable field map.
- Queue (`Setup > Zoho > Sync Jobs`) with back-off 5 min, 15 min, 1 h, 4 h, 12 h; failures open an issue.

### Confirm with live credentials
Account, tax, TDS and custom-field IDs come from your Zoho Books organisation; Creator link names from your app.

### Tests
`odoo-bin -d <db> -i cargo_pilot_tms --test-tags /cargo_pilot_tms --stop-after-init

## Cargo Pilot EasyEcom
EasyEcom (OMS/WMS) with Cargo Pilot TMS.

- Auth: `x-api-key` plus JWT from `POST /access/token {email, password, location_key}` (renewed on 401).
- Orders: `GET /orders/V2/getAllOrders` (paged with `nextUrl`) or webhook `/cp/easyecom/webhook/order`
  into **Channel Orders** (items, serials, invoice, e-way bill, marketplace -> channel).
- Plan Transport: groups by warehouse, destination and channel into FTL / LTL load requests.
- Status back: `POST /Carrier/updateTrackingStatus` with awb, `current_shipment_status_id` (configurable map), ETA,
  delivery date.
- Custom carrier: `/cp/easyecom/carrier/create_shipment`, `/cp/easyecom/carrier/cancel_shipment` (header `X-CP-Token`).
- Stock: `GET /getInventoryDetailsV3` or webhook `/cp/easyecom/webhook/inventory` into **Channel Stock**.

### Confirm with live credentials
EasyEcom paths, status ids and webhook payloads vary by account and API version; all are settings on the connection
(Advanced tab). The parser accepts the common field-name variants.

### Tests
`odoo-bin -d <db> -i cargo_pilot_tms --test-tags /cargo_pilot_tms --stop-after-init

## Cargo Pilot Inventory & Sales
- **Plan Truck** on deliveries and internal transfers (list or form): load requests per warehouse / destination /
  channel, lines carry order, invoice, value, weight (product weight x qty) and lot / serial numbers.
- Shipment lines link back to the delivery and sales order; **Validate Deliveries** from a delivered shipment.
- Sales orders: channel, freight cost, margin after freight (amount, %), freight % of sales; pivot and graph.
- **Warehouse Rules** by ship-to state, channel, customer, with stock check; applied on confirmation (setting).
- **Stock 360**: incoming (open receipts by supplier), in warehouse, reserved, on trucks, at channels (EasyEcom).

### Tests
`odoo-bin -d <db> -i cargo_pilot_tms --test-tags /cargo_pilot_tms --stop-after-init

## Cargo Pilot Dispatch
Implements the FRD *Logistics Dispatch & Transport Management* (regular carrier first, 3PL bidding fallback).
The FRD names the module `tms_dispatch_bidding`; here it is cargo_pilot_tms, built on Cargo Pilot TMS so
bidding, tracking, driver app and ePOD are reused. Model names follow Cargo Pilot (`cp.*`):

| FRD model | Here |
|---|---|
| tms.dispatch.plan | cp.dispatch.plan |
| tms.carrier.check.log | cp.carrier.check.log |
| tms.bid / tms.bid.quote | cp.auction / cp.auction.bid (with dispatch fields) |
| tms.vehicle.entry | cp.gate.entry (with dispatch fields) |
| tms.detention.record | cp.detention.record |
| tms.pod.log | cp.dispatch.pod |

### Requirement traceability

| FRD | Delivered | Test |
|---|---|---|
| FR-01 Dispatch plan, multi-picking, capacity check, status workflow | Plan from delivery orders; weight / volume / packages / value; smallest fitting truck suggested, undersized truck blocked; Draft → Ready → Bidding → Assigned → Loading → In Transit → Delivered (POD pending) → POD Verified → Closed | test_fr01 |
| FR-02 Regular partner first | Availability wizard (Confirmed / No Vehicle / Over Capacity / Out of Service Area), pre-check from weight, volume and PIN limits, immutable audit log, Book Regular Carrier (AWB / booking ref, shipment, PO) | test_fr02 (2) |
| FR-03 10-20 minute bid | Escalation only when every regular partner is unavailable; truck type, pickup slot, duration 10-20 (default 15), Mode A reverse auction / Mode B target price; e-mail + SMS + WhatsApp to approved vendors for the truck type and zone; portal countdown; lock at the deadline | test_fr03 (3), test_portal |
| FR-04 Winner and PO | L1 or first acceptance proposed; Logistics Manager approves; other vendor needs a justification; PO created; winner and closure notices | test_fr03_fr04 |
| FR-05 Gate | Gate-in T0 with driver name, phone, licence, vehicle; inspection (licence, fitness, cleanliness, seal); loading T1/T2; gate-out T3 with e-way bill (interstate or above value threshold) | test_fr05 |
| FR-06 Detention | TAT = T3 - T0; free hours per vendor and truck type; cause required; transporter fault = 0; hourly or daily slab (full / pro-rata days); Logistics Manager then Plant Operations Head (different people); adjusted amounts need a reason | test_fr05_fr06, test_fr06 |
| FR-07 Tracking | DHL / Delhivery / FedEx tracking links from the AWB, GPS tracking page for 3PL trucks; customer e-mail / SMS / WhatsApp at dispatch and out-for-delivery; transit board (In Transit, Delayed, Out for Delivery, Delivered) | test_fr02, test_fr05, test_epod |
| FR-08 POD | Upload from vendor portal, driver ePOD or back office; audit checklist (signature, date, stamp, quantity or damage remarks); verify or reject with reason and re-upload request | test_fr08 |
| FR-09 3-way match | Bill lines: base freight (linked to the PO line) + approved detention; posting and payment blocked until the POD is verified; any freight difference or detention above approval needs an accounting manager; bills created from the PO are gated too | test_fr08_fr09, test_fr09 (2) |
| FR-10 KPIs | Dashboard: regular share vs target, bid savings and participation, detention by cause, on-time reporting, transit compliance, POD turnaround, outstanding PODs, bills on hold, vendor table; pivot reports | test_fr10 |
| Section 6 roles | Sales Coordinator, Gate Security, Plant Operations Head groups; Logistics Officer / Manager = Cargo Pilot User / Administrator; Finance read-only + accounting manager approval; vendor portal | test_role_matrix |

### Different from the FRD wording (on purpose)
- **Live timer**: the portal counts down every second and checks the server every 5 seconds (no WebSocket needed);
  a bid is locked by the first poll or the one-minute cron after its deadline, and no offer is accepted after it.
- **Carrier availability**: logged by the team (phone / e-mail / portal) with pre-checks. DHL, Delhivery and FedEx
  live capacity APIs are not called; Delhivery API booking exists in Cargo Pilot Courier for parcels.
- **POD storage**: attachments on the POD record; also filed in Odoo Documents when that (Enterprise) app is
  installed. No OCR.
- **Freight accounting**: bills post to the Freight / Detention products' expense accounts. For landed cost, install
  `stock_landed_costs` and mark those products as landed costs (standard Odoo).

### Set up
1. Dispatch > Setup > Regular Partners: DHL Express India, Delhivery and FedEx India are created; add weight, volume
   and PIN limits and check the tracking links.
2. Dispatch > Setup > 3PL Vendors: approve vendors, their truck types and service states; add detention terms.
3. Give users the Dispatch roles (Settings > Users). Give vendors portal access.
4. Settings > Cargo Pilot TMS > Dispatch & 3PL Bidding: bid duration, reporting window, e-way bill rule, SMS,
   default detention terms, regular share target.

### Tests
`odoo-bin -d <db> -i cargo_pilot_tms --test-tags /cargo_pilot_tms --stop-after-init
