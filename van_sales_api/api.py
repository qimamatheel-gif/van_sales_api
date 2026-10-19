"""All user documents use normal ERPNext permissions and validation.
The private request ledger and explicitly authorized, user-scoped settings
use ignore_permissions; business documents retain normal ERPNext permissions.
No offline accounting, hard-coded VAT or client-selected ledger accounts.
"""
import hashlib
import json

import frappe
from frappe import _
from frappe.utils import getdate, nowdate, flt
from frappe.sessions import get_csrf_token
from van_sales_api.settings import get_settings, public_settings, transaction_defaults
from van_sales_api.validation import clean_lines, positive, request_identity


def auth():
    if frappe.session.user == "Guest":
        frappe.throw(_("يرجى تسجيل الدخول"), frappe.AuthenticationError)


def read_doc(doctype, name):
    doc = frappe.get_doc(doctype, name)
    doc.check_permission("read")
    from van_sales_api.customer_scope import check_customer
    if doctype == 'Customer':check_customer(doc)
    elif doctype == 'Sales Invoice':read_doc('Customer',doc.customer)
    elif doctype == 'Payment Entry' and doc.party_type == 'Customer':read_doc('Customer',doc.party)
    return doc


def company_doc(name):
    return read_doc("Company", name)


def parse(value):
    return frappe.parse_json(value) if isinstance(value, str) else value


def visible(doctype, fields, filters=None, start=0, length=200):
    filters=dict(filters or {})
    if doctype in ('Sales Invoice','Payment Entry'):
        from van_sales_api.customer_scope import customer_names
        names=customer_names()
        if names is not None:
            field='customer' if doctype=='Sales Invoice' else 'party'
            requested=filters.get(field)
            if isinstance(requested,str) and requested not in names:frappe.throw(_('العميل خارج مجموعتك'),frappe.PermissionError)
            if not requested:filters[field]=['in',names or ['__NO_CUSTOMERS__']]
    return frappe.get_list(doctype, fields=fields, filters=filters,
        order_by="name asc", limit_start=int(start), limit_page_length=int(length))


@frappe.whitelist()
def bootstrap():
    auth()
    if not frappe.db.has_column('Mobile Request','abandoned') or not frappe.db.has_column('Mobile Sales Settings','customer_group'):
        frappe.throw(_('ملفات التحديث وصلت، لكن قاعدة البيانات لم تُحدّث. أكمل نشر الموقع وتنفيذ Migrate في Frappe Cloud أولًا'))
    companies = visible("Company", ["name", "default_currency"], length=1000)
    config = get_settings()
    manager = 'System Manager' in frappe.get_roles()
    from van_sales_api.customer_scope import customer_names
    allowed = customer_names() if (config and config.get('customer_group')) or manager else []
    # Scope IDs also prune previously cached customers when membership changes.
    return {"user": frappe.session.user, "companies": companies, "customer_scope": allowed, "api_version": 3, "csrf_token": get_csrf_token(), "posting_date": nowdate(), "settings": public_settings(), "configured": bool(get_settings()), "can_initialize": "System Manager" in frappe.get_roles()}


@frappe.whitelist()
def catalog(kind, company=None, start=0, price_list=None):
    auth()
    if company:
        company_doc(company)
    if kind == "prices":
        read_doc("Price List", price_list)
    if int(start) < 0:
        frappe.throw(_("رقم صفحة غير صالح"))
    definitions = {
        "customer_groups": ("Customer Group", ["name"], {}),
        "customers": ("Customer", ["name", "customer_name", "tax_id"], {"disabled": 0}),
        "items": ("Item", ["name", "item_name", "stock_uom"], {"disabled": 0, "is_sales_item": 1}),
        "warehouses": ("Warehouse", ["name"], {"company": company, "is_group": 0, "disabled": 0}),
        "price_lists": ("Price List", ["name", "currency"], {"enabled": 1, "selling": 1}),
        "payment_modes": ("Mode of Payment", ["name", "type"], {"enabled": 1}),
        "prices": ("Item Price", ["name", "item_code", "uom", "price_list_rate", "currency", "valid_from", "valid_upto", "customer"], {"price_list": price_list, "selling": 1}),
        "tax_templates": ("Sales Taxes and Charges Template", ["name"], {"company": company, "disabled": 0}),
    }
    if kind not in definitions:
        frappe.throw(_("نوع بيانات غير صالح"))
    dt, fields, filters = definitions[kind]
    if kind == 'customers':
        from van_sales_api.customer_scope import customer_filters
        filters.update(customer_filters())
    rows = visible(dt, fields, filters, start)
    has_more = len(rows) == 200
    if kind == "prices":
        from van_sales_api.customer_scope import customer_names
        allowed = customer_names()
        if allowed is not None:
            rows = [r for r in rows if not r.customer or r.customer in allowed]
    if kind == "items":
        for row in rows:
            item = read_doc("Item", row.name)
            row["barcodes"] = [{"barcode": b.barcode, "uom": b.uom} for b in item.barcodes]
            row["units"] = [{"uom": u.uom, "conversion_factor": u.conversion_factor} for u in item.uoms]
            if not any(u["uom"] == item.stock_uom for u in row["units"]):
                row["units"].append({"uom": item.stock_uom, "conversion_factor": 1})
    return {"rows": rows, "next_start": int(start) + 200 if has_more else None}


@frappe.whitelist()
def item_units(item_code):
    auth()
    doc = read_doc("Item", item_code)
    return [{"uom": r.uom, "conversion_factor": r.conversion_factor} for r in doc.uoms]


def validate_settings(p):
    company = company_doc(p.get("company"))
    warehouse = p.get("warehouse")
    if warehouse:
        wh = read_doc("Warehouse", warehouse)
        if wh.company != company.name or wh.is_group or wh.disabled:
            frappe.throw(_("المستودع غير صالح للشركة"))
    return company


def lines(p, warehouse=None):
    try:
        rows = clean_lines(p.get("items"))
    except (ValueError, TypeError) as exc:
        frappe.throw(str(exc))
    for row in rows:
        item = read_doc("Item", row["item_code"])
        if item.disabled:
            frappe.throw(_("الصنف موقوف"))
        units = {u.uom: flt(u.conversion_factor) for u in item.uoms}
        units.setdefault(item.stock_uom, 1)
        factor = units.get(row["uom"])
        if not factor or factor <= 0:
            frappe.throw(_("وحدة البيع غير معرفة لهذا الصنف"))
        row.update(conversion_factor=factor, stock_uom=item.stock_uom)
        if warehouse:
            row["warehouse"] = warehouse
    return rows


def invoice(p):
    p = transaction_defaults(dict(p, kind="invoice"))
    company = validate_settings(p)
    customer = read_doc("Customer", p.get("customer"))
    price_list = read_doc("Price List", p.get("price_list"))
    if customer.disabled or not price_list.enabled or not price_list.selling:
        frappe.throw(_("العميل أو قائمة الأسعار غير مفعّل"))
    if price_list.currency != company.default_currency:
        frappe.throw(_("هذه النسخة تدعم قائمة أسعار بعملة الشركة فقط"))
    tax = p.get("tax_template")
    if tax:
        template = read_doc("Sales Taxes and Charges Template", tax)
        if template.company != company.name or template.disabled:
            frappe.throw(_("قالب الضريبة غير صالح"))
    doc = frappe.new_doc("Sales Invoice")
    doc.update({"company": company.name, "customer": customer.name,
        "posting_date": nowdate(), "due_date": p.get("due_date") or nowdate(),
        "selling_price_list": price_list.name, "currency": company.default_currency,
        "price_list_currency": company.default_currency, "conversion_rate": 1,
        "plc_conversion_rate": 1, "update_stock": 1 if p.get("update_stock") else 0,
        "set_warehouse": p.get("warehouse"), "taxes_and_charges": tax,
        "items": lines(p, p.get("warehouse"))})
    if doc.update_stock and not p.get("warehouse"):
        frappe.throw(_("اختر مستودعًا لتحديث المخزون"))
    doc.check_permission("create")
    doc.set_missing_values()
    if tax:
        doc.set("taxes", [])
        for row in template.taxes:
            values = row.as_dict()
            for field in ("name", "parent", "parenttype", "parentfield", "doctype", "idx", "owner", "creation", "modified", "modified_by"):
                values.pop(field, None)
            doc.append("taxes", values)
    doc.calculate_taxes_and_totals()
    # ERPNext item/pricing rules must return a price; never silently invoice a missing price at zero.
    for row in doc.items:
        if not flt(row.price_list_rate) and not flt(row.rate):
            frappe.throw(_("لا يوجد سعر للصنف {0} بوحدة {1}").format(row.item_code, row.uom))
    return doc


def sales_return(p):
    from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
    original = read_doc("Sales Invoice", p.get("invoice"))
    if original.docstatus != 1 or original.is_return:
        frappe.throw(_("اختر فاتورة مبيعات معتمدة"))
    if original.company != p.get("company"):
        frappe.throw(_("الفاتورة ليست للشركة المختارة"))
    doc = make_sales_return(original.name)
    doc.check_permission("create")
    selected = p.get("return_items") or []
    amounts = {}
    for row in selected:
        key = row.get("row_name")
        if key in amounts:
            frappe.throw(_("سطر مرتجع مكرر"))
        amounts[key] = positive(row.get("qty"))
    if not amounts:
        frappe.throw(_("حدد كمية واحدة على الأقل للإرجاع"))
    keep = []
    for row in doc.items:
        source = row.sales_invoice_item
        if source in amounts:
            if amounts[source] > abs(flt(row.qty)):
                frappe.throw(_("كمية المرتجع أكبر من الكمية الأصلية"))
            row.qty = -amounts.pop(source)
            keep.append(row)
    if amounts:
        frappe.throw(_("سطر المرتجع غير موجود في الفاتورة"))
    doc.set("items", keep)
    doc.posting_date = nowdate()
    doc.calculate_taxes_and_totals()
    # Save/submit also enforces quantities already returned, serials and batches in ERPNext.
    return doc


def receipt(p):
    p = transaction_defaults(dict(p, kind="receipt"))
    from erpnext.accounts.party import get_party_account
    company = validate_settings(p)
    customer = read_doc("Customer", p.get("customer"))
    mode = read_doc("Mode of Payment", p.get("mode_of_payment"))
    amount = positive(p.get("amount"))
    account_name = next((r.default_account for r in mode.accounts if r.company == company.name), None)
    if not account_name:
        frappe.throw(_("حدد الحساب الافتراضي لطريقة الدفع داخل ERPNext"))
    paid_to = read_doc("Account", account_name)
    allocations = p.get("references") or []
    if not isinstance(allocations, list) or len(allocations) > 100:
        frappe.throw(_("قائمة الفواتير المسددة غير صالحة"))
    checked, seen = [], set()
    allocated = 0
    for entry in allocations:
        name = entry.get("reference_name")
        if name in seen:
            frappe.throw(_("فاتورة مسددة مكررة"))
        seen.add(name)
        inv = read_doc("Sales Invoice", name)
        value = positive(entry.get("allocated_amount"))
        if (inv.docstatus != 1 or inv.is_return or inv.customer != customer.name
                or inv.company != company.name or inv.currency != company.default_currency):
            frappe.throw(_("مرجع الفاتورة غير صالح لهذا العميل والشركة والعملة"))
        if value > flt(inv.outstanding_amount) + 0.000001:
            frappe.throw(_("المبلغ أكبر من المتبقي على الفاتورة {0}").format(inv.name))
        checked.append((inv, value))
        allocated += value
    if allocated > amount + 0.000001:
        frappe.throw(_("إجمالي توزيع التحصيل أكبر من مبلغ السند"))
    party_account_name = checked[0][0].debit_to if checked else get_party_account("Customer", customer.name, company.name)
    if any(inv.debit_to != party_account_name for inv, value in checked):
        frappe.throw(_("اختر فواتير على نفس حساب الذمم المدينة"))
    paid_from = read_doc("Account", party_account_name)
    if (paid_to.company != company.name or paid_to.is_group or paid_to.disabled
            or paid_to.account_type not in ("Bank", "Cash")):
        frappe.throw(_("حساب التحصيل يجب أن يكون حساب بنك أو نقدية صالحًا للشركة"))
    for account in (paid_to, paid_from):
        if account.account_currency != company.default_currency:
            frappe.throw(_("هذه النسخة تدعم التحصيل بعملة الشركة فقط"))
    doc = frappe.new_doc("Payment Entry")
    doc.update({"payment_type": "Receive", "company": company.name, "posting_date": nowdate(),
        "party_type": "Customer", "party": customer.name, "mode_of_payment": mode.name,
        "paid_from": paid_from.name, "paid_to": paid_to.name,
        "paid_from_account_currency": company.default_currency,
        "paid_to_account_currency": company.default_currency,
        "paid_amount": amount, "received_amount": amount,
        "source_exchange_rate": 1, "target_exchange_rate": 1,
        "reference_no": p.get("reference_no"), "reference_date": nowdate()})
    if mode.type == "Bank" and not p.get("reference_no"):
        frappe.throw(_("رقم مرجع العملية البنكية مطلوب"))
    for inv, value in checked:
        doc.append("references", {"reference_doctype": "Sales Invoice", "reference_name": inv.name,
            "due_date": inv.due_date, "total_amount": inv.grand_total,
            "outstanding_amount": inv.outstanding_amount, "allocated_amount": value})
    doc.check_permission("create")
    doc.setup_party_account_field()
    doc.set_missing_values()
    doc.set_missing_ref_details()
    doc.set_exchange_rate()
    doc.set_amounts()
    return doc


def transfer(p):
    p = transaction_defaults(dict(p, kind="transfer"))
    company = validate_settings(p)
    source = read_doc("Warehouse", p.get("source"))
    target = read_doc("Warehouse", p.get("target"))
    for wh in (source, target):
        if wh.company != company.name or wh.disabled or wh.is_group:
            frappe.throw(_("المستودع غير صالح للشركة"))
    if source.name == target.name:
        frappe.throw(_("اختر مستودعين مختلفين"))
    doc = frappe.new_doc("Stock Entry")
    doc.update({"company": company.name, "stock_entry_type": "Material Transfer",
        "purpose": "Material Transfer", "posting_date": nowdate(),
        "from_warehouse": source.name, "to_warehouse": target.name})
    for row in lines(p):
        row.update(s_warehouse=source.name, t_warehouse=target.name)
        doc.append("items", row)
    doc.check_permission("create")
    doc.set_missing_values()
    return doc


def summary(doc):
    return {"doctype": doc.doctype, "name": doc.name, "docstatus": doc.docstatus,
        "currency": doc.get("currency") or doc.get("paid_to_account_currency"),
        "net_total": doc.get("net_total"), "tax_total": doc.get("total_taxes_and_charges"),
        "grand_total": doc.get("grand_total"), "amount": doc.get("paid_amount"),
        "modified": str(doc.get("modified") or ""), "company": doc.get("company"), "is_return": bool(doc.get("is_return")),
        "posting_date": doc.get("posting_date"), "customer": doc.get("customer") or doc.get("party"),
        "customer_name": doc.get("customer_name") or doc.get("party_name"), "tax_id": doc.get("tax_id"),
        "references": [{"reference_name": r.reference_name, "allocated_amount": r.allocated_amount} for r in (doc.get("references") or [])],
        "items": [{"item_code": r.item_code, "item_name": r.get("item_name"), "qty": r.qty, "uom": r.uom,
            "rate": r.get("rate"), "amount": r.get("amount")} for r in (doc.get("items") or [])]}


@frappe.whitelist(methods=["POST"])
def save_document(request_id, payload):
    auth()
    p = parse(payload)
    if not isinstance(p, dict):
        frappe.throw(_("بيانات غير صالحة"))
    builders = {"invoice": invoice, "return": sales_return, "receipt": receipt, "transfer": transfer}
    if p.get("kind") not in builders:
        frappe.throw(_("عملية غير مدعومة"))
    try:
        key, fingerprint = request_identity(frappe.session.user, request_id, p)
    except (ValueError, TypeError):
        frappe.throw(_("معرف طلب أو بيانات غير صالحة"))
    # Lock + committed ledger form one transactional boundary, including the document.
    # TTL is longer than normal web request timeouts; retain ledger permanently.
    lock_name = "van-sales:" + key
    with frappe.cache.lock(lock_name, timeout=600, blocking_timeout=5):
        try:
            previous = frappe.db.get_value("Mobile Request", key,
                ["payload_hash", "document_type", "document_name", "abandoned"], as_dict=True)
            if previous:
                if previous.payload_hash != fingerprint:
                    frappe.throw(_("لا يمكن تغيير بيانات طلب تم إرساله سابقًا"))
                if previous.abandoned:
                    frappe.throw(_("أُلغي هذا الطلب للتعديل. استخدم الطلب الجديد"))
                return summary(read_doc(previous.document_type, previous.document_name))
            doc = builders[p["kind"]](p)
            doc.insert()  # normal permissions and validations; draft only
            frappe.get_doc({"doctype": "Mobile Request", "request_key": key,
                "request_user": frappe.session.user, "payload_hash": fingerprint,
                "document_type": doc.doctype, "document_name": doc.name}).insert(ignore_permissions=True)
            frappe.db.commit()
            return summary(doc)
        except (frappe.ValidationError, frappe.PermissionError):
            frappe.db.rollback()
            raise
        except Exception:
            frappe.db.rollback()
            incident = frappe.generate_hash(length=10)
            frappe.log_error(title="Van Sales " + incident, message=frappe.get_traceback())
            frappe.throw(_("تعذر تنفيذ العملية. راجع سجل الأخطاء في ERPNext برقم {0}").format(incident))


@frappe.whitelist(methods=["POST"])
def submit_document(doctype, name, modified=None):
    auth()
    if doctype not in ("Sales Invoice", "Payment Entry", "Stock Entry"):
        frappe.throw(_("نوع مستند غير مسموح"))
    key = hashlib.sha256(f"{doctype}:{name}".encode()).hexdigest()
    with frappe.cache.lock("van-sales:submit:" + key, timeout=600, blocking_timeout=5):
        try:
            doc = read_doc(doctype, name)
            doc.check_permission("submit")
            if doc.docstatus == 0:
                if modified is not None and str(doc.modified) != str(modified):
                    frappe.throw(_("تغير المستند بعد المراجعة. أعد فتحه وراجع أحدث نسخة قبل الاعتماد"))
                doc.submit()
                frappe.db.commit()
            elif doc.docstatus != 1:
                frappe.throw(_("المستند ملغي"))
            return summary(doc)
        except (frappe.ValidationError, frappe.PermissionError):
            frappe.db.rollback()
            raise
        except Exception:
            frappe.db.rollback()
            incident = frappe.generate_hash(length=10)
            frappe.log_error(title="Van Sales " + incident, message=frappe.get_traceback())
            frappe.throw(_("تعذر تنفيذ العملية. راجع سجل الأخطاء في ERPNext برقم {0}").format(incident))


@frappe.whitelist()
def invoices(company, customer=None, start=0):
    auth()
    company_doc(company)
    filters = {"company": company, "docstatus": 1, "is_return": 0}
    if customer:
        read_doc("Customer", customer)
        filters["customer"] = customer
    return visible("Sales Invoice", ["name", "customer", "posting_date", "grand_total", "currency"],
        filters, start)


@frappe.whitelist()
def invoice_details(name):
    auth()
    doc = read_doc("Sales Invoice", name)
    return {"name": doc.name, "customer": doc.customer,
        "items": [{"row_name": r.name, "item_code": r.item_code,
            "item_name": r.item_name, "qty": r.qty, "uom": r.uom} for r in doc.items]}


@frappe.whitelist()
def statement(company, customer, from_date, to_date, start=0):
    auth()
    co = company_doc(company)
    read_doc("Customer", customer)
    if getdate(from_date) > getdate(to_date):
        frappe.throw(_("تاريخ البداية يجب أن يسبق تاريخ النهاية"))
    # ORM get_list respects GL Entry roles, document permissions and User Permissions.
    # No raw SQL bypass and no fallback to get_all. Currency is company currency.
    filters = {"company": company, "party_type": "Customer", "party": customer, "is_cancelled": 0}
    before = dict(filters, posting_date=["<", from_date])
    period = dict(filters, posting_date=["between", [from_date, to_date]])
    opening = frappe.get_list("GL Entry", fields=["party", {"SUM": "debit", "as": "debit"}, {"SUM": "credit", "as": "credit"}], filters=before, group_by="party", order_by="party", limit_page_length=1)
    total = frappe.get_list("GL Entry", fields=["party", {"SUM": "debit", "as": "debit"}, {"SUM": "credit", "as": "credit"}], filters=period, group_by="party", order_by="party", limit_page_length=1)
    opening_balance = flt(opening[0].debit) - flt(opening[0].credit) if opening else 0
    debit = flt(total[0].debit) if total else 0
    credit = flt(total[0].credit) if total else 0
    # Group by the filtered party, compatible with ONLY_FULL_GROUP_BY.
    rows = frappe.get_list("GL Entry", fields=["name", "posting_date", "voucher_type", "voucher_no", "debit", "credit"],
        filters=period, order_by="posting_date asc, creation asc, name asc", limit_start=int(start), limit_page_length=200)
    return {"opening": opening_balance, "debit": debit, "credit": credit,
        "closing": opening_balance + debit - credit, "currency": co.default_currency,
        "rows": rows, "next_start": int(start) + 200 if len(rows) == 200 else None}


@frappe.whitelist(methods=["POST"])
def request_status(request_id, payload):
    """Check under the same lock before discarding a failed client request."""
    auth()
    p = parse(payload)
    key, fingerprint = request_identity(frappe.session.user, request_id, p)
    with frappe.cache.lock("van-sales:" + key, timeout=600, blocking_timeout=5):
        previous = frappe.db.get_value("Mobile Request", key,
            ["payload_hash", "document_type", "document_name", "abandoned"], as_dict=True)
        if not previous:
            return {"saved": False}
        if previous.payload_hash != fingerprint:
            frappe.throw(_("بيانات الطلب مختلفة"))
        if previous.abandoned:
            return {"saved": False, "retired": True}
        return {"saved": True, "document": summary(read_doc(previous.document_type, previous.document_name))}


@frappe.whitelist(methods=["POST"])
def unlock_settings(pin):
    auth()
    from van_sales_api.settings import unlock
    return {"token": unlock(pin)}


@frappe.whitelist(methods=["POST"])
def save_settings(values, token=None, pin=None):
    auth()
    from van_sales_api.settings import save
    return {"settings": save(parse(values), token, pin)}


@frappe.whitelist(methods=["POST"])
def preview_invoice(payload):
    auth()
    p = parse(payload)
    if not isinstance(p, dict):
        frappe.throw(_("بيانات المعاينة غير صالحة"))
    return summary(invoice(p))  # no insert, submit, stock or GL effects


@frappe.whitelist()
def outstanding_invoices(company, customer, start=0):
    auth()
    company_doc(company)
    read_doc("Customer", customer)
    rows = visible("Sales Invoice", ["name", "customer", "posting_date", "due_date", "grand_total", "outstanding_amount", "currency"],
        {"company": company, "customer": customer, "docstatus": 1, "is_return": 0, "outstanding_amount": [">", 0]}, start)
    return {"rows": rows, "next_start": int(start) + 200 if len(rows) == 200 else None}


def readable_field(doc, field):
    meta = doc.meta.get_field(field)
    if not meta:
        return None
    if meta.permlevel and meta.permlevel not in doc.get_permlevel_access("read"):
        return None
    return doc.get(field)


@frappe.whitelist()
def customer_details(customer, company):
    auth()
    company_doc(company)
    doc = read_doc("Customer", customer)
    result = {"name": doc.name, "customer_name": doc.customer_name, "tax_id": doc.tax_id,
        "phone": readable_field(doc, "mobile_no") or readable_field(doc, "phone"),
        "email": readable_field(doc, "email_id"), "address": "", "latitude": None, "longitude": None,
        "currency": company_doc(company).default_currency, "balance": None}
    sources = [doc]
    for doctype, link in (("Contact", doc.get("customer_primary_contact")), ("Address", doc.get("customer_primary_address"))):
        if not link:
            continue
        try:
            linked = read_doc(doctype, link)
        except frappe.PermissionError:
            continue
        sources.append(linked)
        if doctype == "Contact":
            result["phone"] = linked.get("mobile_no") or linked.get("phone") or result["phone"]
            result["email"] = linked.get("email_id") or result["email"]
        else:
            result["address"] = ", ".join(str(linked.get(f)) for f in ("address_line1", "address_line2", "city", "state", "country") if linked.get(f))
    config = public_settings()
    pairs = [(config.get("latitude_field"), config.get("longitude_field")), ("custom_latitude", "custom_longitude"), ("latitude", "longitude")]
    for source in sources:
        for lat, lng in pairs:
            a, b = readable_field(source, lat) if lat else None, readable_field(source, lng) if lng else None
            if a is not None and b is not None:
                try:
                    a, b = float(a), float(b)
                    if -90 <= a <= 90 and -180 <= b <= 180:
                        result.update(latitude=a, longitude=b)
                except (ValueError, TypeError):
                    pass
        for field in ("custom_location", "location", "geolocation"):
            raw = readable_field(source, field)
            if raw:
                try:
                    geo = parse(raw)
                    if geo.get("type") == "FeatureCollection":
                        geo = geo["features"][0]["geometry"]
                    if geo.get("type") == "Point":
                        lng, lat = geo["coordinates"][:2]
                        if -90 <= float(lat) <= 90 and -180 <= float(lng) <= 180:
                            result.update(latitude=float(lat), longitude=float(lng))
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                    pass
    try:
        values = frappe.get_list("GL Entry", fields=["party", {"SUM": "debit", "as": "debit"}, {"SUM": "credit", "as": "credit"}],
            filters={"company": company, "party_type": "Customer", "party": customer, "is_cancelled": 0}, group_by="party", order_by="party", limit_page_length=1)
        result["balance"] = flt(values[0].get("debit")) - flt(values[0].get("credit")) if values else 0
    except frappe.PermissionError:
        pass
    return result


@frappe.whitelist()
def stock(company, warehouse=None, start=0):
    auth()
    company_doc(company)
    warehouse = warehouse or public_settings().get("warehouse")
    wh = read_doc("Warehouse", warehouse)
    if wh.company != company or wh.is_group:
        frappe.throw(_("المستودع غير صالح"))
    rows = visible("Bin", ["name", "item_code", "actual_qty", "reserved_qty", "projected_qty", "stock_uom"], {"warehouse": warehouse}, start)
    for row in rows:
        row["available_qty"] = flt(row.actual_qty) - flt(row.reserved_qty)
    return {"rows": rows, "warehouse": warehouse, "next_start": int(start) + 200 if len(rows) == 200 else None}


@frappe.whitelist(methods=["POST"])
def item_prices(customer, items):
    auth()
    codes = parse(items)
    if not isinstance(codes, list) or len(codes) > 20:
        frappe.throw(_("طلب الأسعار يدعم 20 صنفًا في الصفحة"))
    result = []
    for code in codes:
        item = read_doc("Item", code)
        try:
            doc = invoice({"customer": customer, "items": [{"item_code": code, "uom": item.stock_uom, "qty": 1}]})
            result.append({"item_code": code, "uom": item.stock_uom, "before_tax": doc.net_total,
                "after_tax": doc.grand_total, "tax": doc.total_taxes_and_charges, "currency": doc.currency})
        except frappe.ValidationError as exc:
            result.append({"item_code": code, "error": str(exc)})
    return result


@frappe.whitelist()
def document_pdf(doctype, name):
    auth()
    if doctype not in ("Sales Invoice", "Payment Entry", "Stock Entry"):
        frappe.throw(_("تصدير هذا المستند غير متاح"))
    doc = read_doc(doctype, name)
    doc.check_permission("print")
    # Standard ERPNext print permissions, letterhead and draft-print settings are retained.
    data = frappe.get_print(doctype, name, as_pdf=True)
    frappe.local.response.filename = name.replace("/", "-") + ".pdf"
    frappe.local.response.filecontent = data
    frappe.local.response.type = "pdf"

from van_sales_api.mobile_documents import (get_document, drafts, prepare_pending_edit, update_document, print_data, statement_print_data, statement_pdf)
