"""Server-side customer-group boundary, in addition to standard ERP permissions."""
import frappe
from frappe import _
from van_sales_api.settings import get_settings

def group_names():
    config=get_settings()
    selected=config.get('customer_group') if config else None
    if not selected:
        if 'System Manager' in frappe.get_roles():return None
        frappe.throw(_('يجب أن يحدد المسؤول مجموعة العملاء لهذا المندوب في Mobile Sales Settings'),frappe.PermissionError)
    group=frappe.get_doc('Customer Group',selected);group.check_permission('read')
    # Includes the assigned group and permitted descendants in ERPNext's nested tree.
    children=frappe.get_list('Customer Group',filters={'lft':['>=',group.lft],'rgt':['<=',group.rgt]},pluck='name',limit_page_length=0)
    return list(dict.fromkeys([selected]+children))

def customer_filters():
    groups=group_names()
    return {'customer_group':['in',groups]} if groups is not None else {}

def check_customer(doc):
    groups=group_names()
    if groups is not None and doc.customer_group not in groups:
        frappe.throw(_('هذا العميل خارج مجموعة العملاء المخصصة لك'),frappe.PermissionError)

def customer_names():
    filters=customer_filters()
    if not filters:return None
    return frappe.get_list('Customer',filters=filters,pluck='name',limit_page_length=0)
