# Van Sales API 0.4.0 / API 4 — ERPNext/Frappe v16

Deploy the complete contents of this directory as the root of the van_sales_api Git repository. Deploy the chosen branch to the site's Bench and update/migrate the site. Do not upload only api.py or the ZIP as application code. New installations additionally require install-app. Bootstrap checks the new schema and reports incomplete migration.

## Salesperson customer scope

System Manager configures each user's Mobile Sales Settings: company, customer_group, warehouse, selling price list, PIN and optional defaults. A salesperson without an assigned group is denied customer operations. The group includes permitted descendants. A System Manager without assignment can initialize setup; assigned administrators are scoped too. Normal ERPNext permissions and User Permissions remain active.

Scope is enforced on customer catalogs, customer-specific item prices, invoice/receipt reads and creation, returns, statements, PDFs/thermal print data and drafts. Bootstrap returns allowed IDs to prune stale cached customer data, even when a customer moves between groups without changing the salesperson's configuration. These controls govern this API; configure User Permissions separately if the same scope must apply in ERPNext Desk and other integrations.

## Draft edit consistency

Mobile Request now includes an abandoned flag and optional document links. prepare_pending_edit runs under the same user/request lock as original saving. It resolves saved requests or permanently retires unsaved UUIDs before altered data is allowed under a new UUID. save_document rejects retired requests, preventing delayed originals from creating duplicates.

update_document uses a stable UUID/envelope and persistent ledger, requires normal Write/Create validation and docstatus=0, compares the expected modified timestamp, rebuilds supported transaction fields through ERPNext pricing/defaults, and saves the same document. It shares the submit lock. Repeated confirmed update requests return the same document without saving again. New clients submit with the reviewed modified timestamp; changed drafts require reopening before approval.

Draft listing is restricted to the current user's drafts and supported Sales Invoice/Return, Receive-from-Customer Payment Entry and Material Transfer Stock Entry operations. Custom fields are retained, but complex external drafts/custom workflows need site acceptance testing.

## Printing/barcodes/stock

Item catalog includes all Item Barcode rows and their UOMs. Stock reports available_qty=actual_qty-reserved_qty in stock UOM; this is a display calculation, not a reservation or a complete batch/serial availability solver.

Document PDF supports Sales Invoice (including returns), Payment Entry and Stock Entry using standard print permissions/templates/settings. print_data provides current authorized data for the mobile bilingual ESC/POS layout. statement_print_data and statement_pdf require GL Entry report permission, use scoped customer/normal list permissions and v16 dict SUM aggregation, and compute running balances. Statement PDF limit: 20000 rows; mobile thermal limit:1000 rows. Narrow the period for large reports.

The thermal format is separate from site HTML print formats and does not automatically import custom logos, QR/ZATCA layouts. Validate those separately before using the thermal ticket as an official tax document. Standard server PDF preserves the selected default template behavior.

## Permissions and validation

Grant only needed master-data read, Customer Group read, Bin read, GL Entry read/report, document create/read/write/submit/print. Contact/Address read supplies linked details. Configure default collection accounts, company-currency price lists and accounts, tax templates and UOM conversions in ERPNext. Do not grant System Manager to salespeople. Settings PIN uses encrypted Frappe Password fields, attempt throttling and session-bound short-lived edit authorization.

All business documents retain ordinary ERPNext permissions and validation. Private request-ledger insert and explicitly PIN-authorized allowlisted own-user settings writes use ignore_permissions. Redis is required for locks/throttling. Retain ledger records needed for recovery. HTTPS is mandatory.

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

Local tests mock the Frappe boundary. They do not establish live-site accounting, Redis concurrency, hardware printing or installation correctness. The user reported testing version0.3.0 on real hardware/site; version0.4.0 additions require the included acceptance tests. Source is MIT licensed.

## API4 reports and UOMs

- document_history(kind=invoice|receipt) returns 50 submitted documents per page, newest first. It respects normal ORM permissions and salesperson customer-group scope; receipt history only includes Receive/Customer. Reprints do not create or submit documents.
- item_prices returns units[] with an independent ERPNext quote for one unit of each Item UOM. before_tax=net_total and after_tax=grand_total, preserving inclusive/exclusive taxes and pricing rules. App pages use five items and debounce search.
- stock rows now include item name, barcodes and unit conversions for actual/reserved/available. stock_pdf and stock_print_data apply warehouse, query/barcode and hide_zero filters. Bin report permission is required in addition to ordinary read permissions. Report limit10000 matching items /20000 scanned bins; mobile thermal limit500 items.
- Returns accept qty plus optional uom per invoice row. Server derives factors and normalizes to the original invoice UOM, preserving original rates and tax calculation. Original historical factor takes precedence for its own UOM. make_sales_return and ERPNext save/submit enforce previous returns. No client rate/factor is trusted.
- bootstrap adds full_name/email and api_version4. No new DocType fields in this update; deploy/migrate the complete app, retaining existing Mobile Request ledgers.
- Removing a pending operation is an offline, encrypted local tombstone, reversible in the app. It never calls ERPNext delete or retires a server request; restore the same UUID to resolve uncertain outcomes before replacing an operation.
- ML Kit release keep rules explicitly preserve registrars, barcode classes and native bridges for AGP9/R8. Initialization is inside an error boundary, not a Kotlin field initializer. Verify physical camera behavior after installing this update.
