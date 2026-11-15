# Changelog

## 19.0.2.0.0 — Fuel & Fleet Suite (AI)
- **New:** Fleet integration with Odoo Fleet:
  - tankers with compartments, bowsers and service vehicles;
  - statutory documents with expiry alerts and a daily job;
  - depot-loading and doorstep-delivery trips with expenses;
  - fleet fuel logs with km/L;
  - preventive maintenance plans and job cards that post costs and Fleet service logs.
- **New:** AI engine:
  - stock-out forecast and tanker order suggestion;
  - suspicious-slip detection;
  - credit-risk grade and suggested limit;
  - customer and station insights;
  - staff and customer assistants (offline built-in engine, optional Claude/OpenAI; English);
  - conversation log.
- **New:** Credit bill redesign:
  - amount payable and status stamp;
  - account summary;
  - vehicle-wise summary;
  - slips grouped by vehicle with odometer;
  - UPI QR code;
  - FIFO payment status (paid / partial / overdue) everywhere.
- **New:** Portal:
  - AI chat on every page;
  - AI insights;
  - bills-to-pay list;
  - bill detail page;
  - My Vehicles (km/L, trend);
  - indents prefilled by the AI.
- **New:** Slip odometer and km/L, vehicle-tank capacity check, AI flag column.
- **New:** Customer risk columns; AI settings in Station Settings; App Store banner and price.
- Security: AI and ledger services are guarded against portal RPC.
- 28 automated tests (up from 16).

## 19.0.1.0.0
- Initial release: shifts, slips, bills, tanks/DIP, vouchers, staff, admin dashboard and customer portal.
