"""Read-only reports; normal document permissions and per-representative scope."""
from html import escape
import frappe
from frappe import _
from frappe.utils import nowdate, flt


def api():
    from van_sales_api import api as module
    return module


@frappe.whitelist()
def document_history(company, kind, customer=None, query='', start=0):
    a=api();a.auth();a.company_doc(company)
    start=int(start)
    if start<0 or kind not in ('invoice','receipt'):frappe.throw(_('طلب غير صالح'))
    dt='Sales Invoice' if kind=='invoice' else 'Payment Entry'
    filters={'company':company,'docstatus':1}
    if customer:
        a.read_doc('Customer',customer)
        filters['customer' if kind=='invoice' else 'party']=customer
    if kind=='receipt':filters.update(payment_type='Receive',party_type='Customer')
    query=str(query or '').strip()[:140]
    if query:filters['name']=['like','%'+query+'%']
    fields=['name','posting_date','modified']+(['customer','customer_name','grand_total','currency','is_return'] if kind=='invoice' else ['party','party_name','paid_amount','paid_to_account_currency'])
    # visible applies scope on the server, even when the caller omits customer.
    # Order by posting date after permission/scope filtering at database level.
    from van_sales_api.customer_scope import customer_names
    names=customer_names()
    if names is not None:
        field='customer' if kind=='invoice' else 'party'
        if not customer:filters[field]=['in',names or ['__NO_CUSTOMERS__']]
    rows=frappe.get_list(dt,fields=fields,filters=filters,order_by='posting_date desc, modified desc, name desc',limit_start=start,limit_page_length=50)
    for row in rows:
        row['doctype']=dt
        if kind=='receipt':row.update(customer=row.party,customer_name=row.party_name,grand_total=row.paid_amount,currency=row.paid_to_account_currency)
    return {'rows':rows,'next_start':start+50 if len(rows)==50 else None}


@frappe.whitelist()
def stock_print_data(company,warehouse,query='',hide_zero=0):
    a=api();a.auth();co=a.company_doc(company)
    # Printing stock requires report permission in addition to normal Bin/Item/WH reads.
    if not frappe.has_permission('Bin','report'):frappe.throw(_('لا تملك صلاحية تقرير المخزون'),frappe.PermissionError)
    rows=[];start=0;query=str(query or '').strip().casefold()[:140]
    while True:
        page=a.stock(company,warehouse,start)
        for row in page['rows']:
            hay=' '.join([str(row.item_code),str(row.item_name)]+[str(b['barcode']) for b in row['barcodes']]).casefold()
            if query and query not in hay:continue
            if int(hide_zero) and not flt(row.actual_qty):continue
            rows.append(row)
            if len(rows)>10000:frappe.throw(_('التقرير كبير؛ حدّد الأصناف بالبحث'))
        if page['next_start'] is None:break
        start=page['next_start']
        if start>=20000:frappe.throw(_('التقرير كبير؛ استخدم تقرير المخزون في ERPNext'))
    return {'kind':'stock','company_name':co.get('company_name') or co.name,'warehouse':warehouse,'posting_date':nowdate(),'items':rows,'query':query}


@frappe.whitelist()
def stock_pdf(company,warehouse,query='',hide_zero=0):
    d=stock_print_data(company,warehouse,query,hide_zero)
    e=lambda v:escape(str(v or ''))
    n=lambda v:f'{flt(v):,.3f}'
    rows=''
    for item in d['items']:
        for unit in item['units']:
            rows+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in (item['item_code'],item['item_name'],unit['uom'],n(unit['actual_qty']),n(unit['reserved_qty']),n(unit['available_qty'])))+'</tr>'
    html='<html dir="rtl"><meta charset="utf-8"><style>body{font:12px Arial}h2{text-align:center}table{width:100%;border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px}th{background:#eaf0f5}thead{display:table-header-group}tr{page-break-inside:avoid}</style>'
    html+=f'<h2>{e(d["company_name"])}<br>جرد المخزون / Stock Balance</h2><p>{e(warehouse)} | {e(d["posting_date"])}</p><table><thead><tr><th>الكود / Code</th><th>الصنف / Item</th><th>الوحدة / UOM</th><th>الفعلي / Actual</th><th>المحجوز / Reserved</th><th>المتاح / Available</th></tr></thead><tbody>{rows}</tbody></table><p>تقرير أرصدة؛ لا يُنشئ حركة تسوية مخزون.</p></html>'
    from frappe.utils.pdf import get_pdf
    frappe.local.response.filename='Stock-Balance.pdf';frappe.local.response.filecontent=get_pdf(html);frappe.local.response.type='pdf'
