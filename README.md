# Van Sales API 0.2.0 — ERPNext / Frappe v16

Companion backend for the Arabic Android app, API version 2. Frappe Cloud must permit custom applications. This source has not been deployed to the user's repository or verified against their live site.

## Repository structure and upgrade

Upload the complete contents of this directory to the root of the `van_sales_api` Git repository: `pyproject.toml`, `van_sales_api/`, `tests/`, README and LICENSE. Do not upload only api.py or the ZIP as application source. `hooks.py`, `modules.txt`, `patches.txt` and `__init__.py` must remain inside the Python package.

Deploy the updated app on the site's Bench, then update/migrate the site. An already installed application must be updated rather than installed a second time. New installations require installing van_sales_api on the site after adding it to the Bench. Confirm Mobile Request and Mobile Sales Settings exist.

## Administrator setup

A System Manager creates one Mobile Sales Settings document for each mobile user. Set User, Company, Warehouse, Price List, Settings PIN (6–12 digits), and optional target warehouse, tax template, payment mode and custom coordinate field names. Use a selling price list in company currency. PIN is a Frappe encrypted Password field. Mobile first-time self-setup is restricted to System Manager; configured users need PIN to edit their own settings. Five failed attempts lock PIN access for 15 minutes; edit authorization expires after 10 minutes and is bound to user/session. Reset PIN from Desk as administrator.

Invoice company, list, tax and warehouse are applied server-side from these settings. Receipt payment mode and transfer warehouses are defaults; authorized alternatives can be selected. Old clients retain legacy operations until their user settings are configured. The new mobile client requires configuration before starting.

The API uses normal ERPNext document and list permissions. The private idempotency ledger uses ignore_permissions after document authorization; PIN-authorized writes only modify the current user's allowlisted settings. Do not grant System Manager to salespeople. Grant Read for required master data, Bin for stock, Contact/Address for linked details, GL Entry for statements/balances, and document Create/Read/Write/Submit/Print only as needed. Configure User Permissions and company/warehouse restrictions in ERPNext.

## Behavior

Invoice preview does not insert or submit documents. Prices, UOM conversion, tax and ledger validations come from ERPNext. Saving creates a draft; submission is a separate explicit step. Receipt allocations validate invoice customer, company, currency, receivable account, outstanding balance and total amount. PDF uses standard ERPNext print permissions and print settings. Stock is a Bin balance view, not a stock reconciliation document. Location reads existing Customer/Address fields; no invented coordinates. Unsupported foreign-currency receipts, serial/batch workflows and customized compliance flows require further development.

Unexpected save/submit errors return a support ID matching Error Log. The HTTP500 screenshot alone is insufficient to prove the specific live-site cause; this update fixes a known Payment Entry initialization issue and provides diagnostics. Statement aggregation uses the v16 dictionary SUM format.

Redis is required for locks and PIN throttling. Retain Mobile Request records for retry idempotency. HTTPS is mandatory for the Android client. Test business effects on a non-production company before rollout.

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

The tests mock the Frappe boundary; they do not certify live ERPNext accounting, Redis behavior or installation. The full project includes Arabic setup and acceptance guides.
