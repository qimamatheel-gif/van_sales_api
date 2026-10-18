"""PIN-protected, user-scoped defaults. PINs use Frappe encrypted Password fields."""
import hashlib
import hmac
import re
import secrets

import frappe
from frappe import _
from frappe.utils import cint

FIELDS = ('company', 'warehouse', 'target_warehouse', 'price_list', 'tax_template',
          'mode_of_payment', 'update_stock', 'latitude_field', 'longitude_field')


def normalize_pin(value):
    return str(value or '').translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))


def validate_document(doc):
    if doc.settings_pin and '*' not in doc.settings_pin:
        doc.settings_pin = normalize_pin(doc.settings_pin)
        if not re.fullmatch(r'[0-9]{6,12}', doc.settings_pin):
            frappe.throw(_('رقم الإعدادات السري يجب أن يتكون من 6 إلى 12 رقمًا'))
    company = frappe.get_doc('Company', doc.company)
    company.check_permission('read')
    for field in ('warehouse', 'target_warehouse'):
        if doc.get(field):
            wh = frappe.get_doc('Warehouse', doc.get(field)); wh.check_permission('read')
            if wh.company != doc.company or wh.is_group or wh.disabled:
                frappe.throw(_('مستودع غير صالح للشركة'))
    price = frappe.get_doc('Price List', doc.price_list); price.check_permission('read')
    if not price.enabled or not price.selling or price.currency != company.default_currency:
        frappe.throw(_('اختر قائمة أسعار بيع فعالة بعملة الشركة'))
    if doc.tax_template:
        tax = frappe.get_doc('Sales Taxes and Charges Template', doc.tax_template)
        tax.check_permission('read')
        if tax.company != doc.company or tax.disabled:
            frappe.throw(_('قالب الضرائب غير صالح للشركة'))
    if doc.mode_of_payment:
        frappe.get_doc('Mode of Payment', doc.mode_of_payment).check_permission('read')
    for field in ('latitude_field', 'longitude_field'):
        if doc.get(field) and not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', doc.get(field)):
            frappe.throw(_('اسم حقل الموقع غير صالح'))


def get_settings(required=False):
    if not frappe.db.exists('DocType', 'Mobile Sales Settings'):
        if required:
            frappe.throw(_('يلزم تحديث إضافة السيرفر وتنفيذ Migrate أولًا'))
        return None
    if not frappe.db.exists('Mobile Sales Settings', frappe.session.user):
        if required:
            frappe.throw(_('يلزم أن يضبط مسؤول النظام إعدادات هذا المستخدم أولًا'))
        return None
    return frappe.get_doc('Mobile Sales Settings', frappe.session.user)


def public_settings(doc=None):
    doc = doc or get_settings()
    return {f: doc.get(f) for f in FIELDS} if doc else {}


def transaction_defaults(payload):
    # Old clients also use these defaults as soon as configuration is created.
    doc = get_settings()
    if not doc:
        return dict(payload)  # upgrade preserves existing operations until admin configures PIN
    p = dict(payload)
    for field in ('company', 'price_list', 'tax_template'):
        p[field] = doc.get(field)
    if p.get('kind') == 'invoice':
        p['warehouse'] = doc.warehouse
        p['update_stock'] = bool(doc.update_stock)
    if p.get('kind') == 'receipt':
        p['mode_of_payment'] = p.get('mode_of_payment') or doc.mode_of_payment
    if p.get('kind') == 'transfer':
        p['source'] = p.get('source') or doc.warehouse
        p['target'] = p.get('target') or doc.target_warehouse
    return p


def unlock(pin):
    doc = get_settings(required=True)
    key = 'van-sales:pin:' + hashlib.sha256(frappe.session.user.encode()).hexdigest()
    with frappe.cache.lock(key + ':lock', timeout=10, blocking_timeout=2):
        attempts = cint(frappe.cache.get_value(key))
        if attempts >= 5:
            frappe.throw(_('تم إيقاف المحاولات لمدة 15 دقيقة بعد تكرار الرقم غير الصحيح'))
        expected = doc.get_password('settings_pin', raise_exception=False) or ''
        if not expected or not hmac.compare_digest(normalize_pin(pin), expected):
            frappe.cache.set_value(key, attempts + 1, expires_in_sec=900)
            frappe.throw(_('الرقم السري غير صحيح'))
        frappe.cache.delete_value(key)
    token = secrets.token_urlsafe(32)
    token_key = 'van-sales:settings-token:' + hashlib.sha256(token.encode()).hexdigest()
    frappe.cache.set_value(token_key, {'user': frappe.session.user, 'sid': frappe.session.sid}, expires_in_sec=600)
    return token


def save(values, token=None, pin=None):
    doc = get_settings()
    if doc:
        token_key = 'van-sales:settings-token:' + hashlib.sha256(str(token or '').encode()).hexdigest()
        granted = frappe.cache.get_value(token_key) or {}
        if granted.get('user') != frappe.session.user or granted.get('sid') != frappe.session.sid:
            frappe.throw(_('أدخل الرقم السري للإعدادات من جديد'), frappe.PermissionError)
    else:
        if 'System Manager' not in frappe.get_roles():
            frappe.throw(_('تهيئة الإعدادات أول مرة متاحة لمسؤول النظام فقط'), frappe.PermissionError)
        doc = frappe.new_doc('Mobile Sales Settings')
        doc.user = frappe.session.user
        doc.settings_pin = normalize_pin(pin)
        if not re.fullmatch(r'[0-9]{6,12}', doc.settings_pin):
            frappe.throw(_('اختر رقمًا سريًا من 6 إلى 12 رقمًا'))
    doc.update({f: values.get(f) for f in FIELDS})
    doc.save(ignore_permissions=True)  # only this user's document after PIN/session authorization
    return public_settings(doc)
