# ZivoIT Tally Connector AI (`zivoit_tally_connector`) - Odoo 19

Two-way, real-time Odoo 19 and TallyPrime integration with an on-premise Bridge
Agent and an AI layer. Built by Zivo IT Technologies (official Odoo partner).

| | |
|---|---|
| Odoo | 19.0 Community & Enterprise | Odoo Online (via agent) | Odoo.sh | On-premise |
| Tally | TallyPrime 1.x to 7.x (incl. 7.2, Oct 2026) and Tally.ERP 9 (legacy schema option) |
| Depends | `account`, `product`, `uom`, `mail`, `stock` - no extra Python packages |
| Tests | 77 automated tests against a TallyPrime protocol simulator, plus a live agent E2E run |
| Packaging | One module folder (`zivoit_tally_connector`) - ready for the Odoo App Store |

## What it syncs

| Object | Direction | Notes |
|---|---|---|
| Groups, voucher types | Tally to Odoo | Custom types ("GST Sales" under "Sales") resolve automatically |
| Party ledgers / Contacts | Two-way | GSTIN, PAN, address, state, e-mail, phone; renames propagate |
| GL ledgers / Accounts | Two-way | Auto-created in the right Tally group from the account type |
| Duty ledgers / Taxes | Two-way | CGST/SGST/IGST/Cess with duty head and rate |
| Bank/Cash ledgers / Journals | Two-way | |
| Stock items / Products, Units / UoM | Two-way | HSN, GST rate, part no., price |
| Sales / Purchase / Credit Note / Debit Note | Two-way | Item-invoice or accounting-invoice mode |
| Receipt / Payment / Journal / Contra | Two-way | Bill-wise allocations (New Ref / Agst Ref / Advance) both ways; auto-reconciliation in Odoo |
| Cancellations | Two-way | Odoo cancel becomes a Tally cancel; Tally cancel becomes an Odoo cancel (by policy) |
| Cost centres / Analytic accounts | Two-way | Cost categories map to analytic plans; allocations on ledger and item lines both ways (`analytic_distribution`) |
| Godowns / Locations | Two-way | "Main Location" maps to warehouse stock; new godowns created as locations |
| Batches / Lots/Serials | Two-way | Expiry dates; items pushed with "Maintain in batches" when tracked |
| Stock moves | Tally to Odoo | Item invoices create & validate deliveries/receipts/returns per godown and lot; reversed when the Tally voucher is altered, cancelled or deleted |
| Deletions in Tally | Detected | Vouchers and masters, via periodic census; policy-driven; mass-deletion guard |
| Party balances | Compare | Reconciliation report with mismatch hints |

## Transports

1. **Direct** - Odoo reaches TallyPrime's XML server (LAN/VPN, port 9000).
2. **Bridge Agent** - for Odoo Online / Odoo.sh / any cloud. A stdlib-only Python
   agent on the Tally PC polls Odoo over HTTPS with an API key (JSON-2 API) and
   relays requests to `localhost:9000`. No port forwarding, VPN or static IP.
3. **Offline XML** - export a zip of Tally-importable XML; import Tally XML exports
   (masters / Day Book).

## Reliability design

* One queue + audit log (`tally.request`) for all transports: masters always before
  vouchers, exponential back-off retries, stale agent jobs reclaimed, full request
  and response XML kept for audit.
* Incremental pulls by Tally **AlterID** - only changed objects are read.
* Idempotent pushes via **REMOTEID** - re-posting alters the same Tally voucher.
* **Echo-loop prevention** - vouchers originating in Odoo are recognised on pull
  and vice versa.
* Vouchers are rebuilt right before sending, so reconciliations done after posting
  (payment wizard) and master renames are reflected.
* Conflict policy per connection: hold for review / Tally wins / Odoo wins, for
  masters and for vouchers altered after import.
* **Deletion detection** - Tally never exports deleted objects, so every few hours
  (configurable) a lightweight census fetches only GUIDs/names for the sync window:
  * Tally voucher deleted: the Odoo record is flagged "Deleted in Tally" (Tally wins: the
    Odoo entry is cancelled and its stock moves reversed).
  * Odoo document deleted in Tally: flagged (Odoo wins: re-pushed automatically).
  * Ledger/item deleted: mapping flagged (Odoo wins: re-created). Odoo records are
    never deleted.
  * If more than the guard % (default 30%) disappears at once, nothing is applied.
* **Odoo to Tally batch allocations** come from the real delivery/receipt linked to the
  invoice line (sale_stock / purchase_stock): godown and lot per quantity split;
  otherwise Main Location.
* Tally's quirks handled: UTF-16 responses, `&#4;` control characters, Dr/Cr
  strings, forex amounts, Indian digit grouping, multi-root responses.

## AI layer

Works fully offline with the built-in engine; an LLM is optional.

* **Master matching** - GSTIN (with checksum validation), same-PAN detection across
  states, e-mail/phone, part number/HSN, normalised fuzzy names (M/s., Pvt Ltd,
  abbreviations). High-confidence matches auto-confirm; the ambiguous band goes to
  the LLM (Claude, OpenAI, Gemini or any OpenAI-compatible endpoint incl. Ollama)
  in batches with only names sent.
* **Anomaly guard** (both directions) to GST place-of-supply (IGST vs CGST+SGST),
  GST arithmetic, missing SGST pair, duplicates, robust z-score outliers vs party
  history, large round amounts, lock-date violations, invalid GSTIN, missing HSN.
  Blocking findings hold the voucher with an explanation and a risk score.
* **Error doctor + auto-heal** - Tally LINEERRORs are translated into fixes; missing
  ledgers/items/units are pushed automatically and the voucher retried.
* **Reviews & health summary** - per-voucher AI review and a connection health
  narrative. Amounts are redacted unless "Share amounts with AI" is on.

## Setup (10 minutes)

1. Install the module (Apps > ZivoIT Tally Connector AI).
2. TallyPrime: F1 > Settings > Connectivity > *TallyPrime acts as* = Both, port 9000.
3. Odoo: Tally > Configuration > Connections > New. Enter the exact Tally company
   name and pick the transport.
   * Agent: create a user with only *Tally Bridge Agent (API user)*, generate an API
     key, click **Download agent package**, unzip on the Tally PC, paste the key,
     run `install_windows_service.bat` as Administrator.
4. **Test connection**, then **Sync now**. Review *Mappings* (AI pre-fills them) and
   *Vouchers from Tally*.
5. Optional: **Historical migration** (dry run first) and **Reconcile balances**.

## Running the tests

```bash
odoo-bin -d testdb -i zivoit_tally_connector --test-tags /zivoit_tally_connector --stop-after-init
```

`tests/tally_simulator.py` is a TallyPrime protocol simulator; run it standalone
(`python tests/tally_simulator.py --port 9000`) to demo the connector without Tally.

## Known limits (honest list)

* Native JSON (TallyPrime 7.0+) is not used; XML is used because it is supported by
  every TallyPrime and Tally.ERP 9 release and its schema is fully documented.
* Multi-currency vouchers are pushed in company currency.
* Stock journals, manufacturing journals and delivery/receipt notes that exist in
  Tally without an invoice are not converted to Odoo transfers.
* Deletion detection is periodic (default every 6 h, or "Detect deletions" on demand),
  not instant, because Tally offers no deletion feed.
* A lot/serial-tracked Odoo product arriving from Tally without a batch name gets its
  transfer left for manual validation (flagged on the voucher).
* Validated against a protocol simulator and real-format fixtures; run a pilot on a
  copy of a live Tally company before go-live.

## Support

Zivo IT Technologies | Official Odoo Partner | Cherlapally, Hyderabad, India
Email: info@zivoit.com | Website: www.zivoit.com
