"""Draft editing with persistent request retirement, optimistic concurrency and print data."""
import hashlib
from html import escape
import frappe
from frappe import _
from frappe.utils import flt
from van_sales_api.validation import request_identity

SUPPORTED = ('Sales Invoice', 'Payment Entry', 'Stock Entry')

def api():
    from van_sales_api import api as endpoints
    return endpoints

def supported_doc(doctype, name, permission='read'):
    if doctype not in SUPPORTED:
        frappe.throw(_('نوع مستند غير مسموح'))
    doc = api().read_doc(doctype, name)
    doc.check_permission(permission)
    if doctype=='Payment Entry' and (doc.payment_type!='Receive' or doc.party_type!='Customer'):
        frappe.throw(_('هذا السند ليس تحصيل عميل'))
    if doctype=='Stock Entry' and doc.purpose!='Material Transfer':
        frappe.throw(_('هذا المستند ليس تحويل مستودعات'))
    return doc

def editable_payload(doc):
    p = {'company': doc.company}
    if doc.doctype == 'Sales Invoice':
        p.update(kind='return' if doc.is_return else 'invoice', customer=doc.customer,
                 invoice=doc.get('return_against'), warehouse=doc.get('set_warehouse'),
                 price_list=doc.get('selling_price_list'), tax_template=doc.get('taxes_and_charges'),
                 update_stock=doc.get('update_stock'), due_date=str(doc.get('due_date') or doc.posting_date))
        p['items'] = [{'item_code': r.item_code, 'item_name': r.item_name, 'qty': abs(flt(r.qty)), 'uom': r.uom} for r in doc.items]
        p['return_items'] = [{'row_name': r.sales_invoice_item, 'qty': abs(flt(r.qty)), 'uom': r.uom} for r in doc.items] if doc.is_return else []
    elif doc.doctype == 'Payment Entry':
        p.update(kind='receipt',customer=doc.party,mode_of_payment=doc.mode_of_payment,
                 amount=doc.paid_amount,reference_no=doc.reference_no,
                 references=[{'reference_name':r.reference_name,'allocated_amount':r.allocated_amount} for r in doc.references])
    else:
        p.update(kind='transfer',source=doc.from_warehouse,target=doc.to_warehouse,
                 items=[{'item_code':r.item_code,'item_name':r.item_name,'qty':r.qty,'uom':r.uom} for r in doc.items])
    return p

def full_summary(doc):
    return dict(api().summary(doc), modified=str(doc.modified), editable_payload=editable_payload(doc),
                company=doc.company, is_return=bool(doc.get('is_return')), source=doc.get('from_warehouse'), target=doc.get('to_warehouse'))

@frappe.whitelist()
def get_document(doctype, name):
    api().auth()
    return full_summary(supported_doc(doctype,name))

@frappe.whitelist()
def drafts(company, start=0):
    api().auth();api().company_doc(company)
    # Each doctype is paged independently; only the current user's saved drafts.
    start=int(start)
    if start<0:frappe.throw(_('صفحة غير صالحة'))
    result=[];more=False
    for dt in SUPPORTED:
        if not frappe.has_permission(dt,'read'):continue
        rows=api().visible(dt,['name','modified'],{'company':company,'owner':frappe.session.user,'docstatus':0},start,length=100)
        more=more or len(rows)==100
        for row in rows:
            doc=api().read_doc(dt,row.name)
            # Do not offer unrelated Payment/Stock Entry variants to this client.
            if dt=='Payment Entry' and (doc.payment_type!='Receive' or doc.party_type!='Customer'):continue
            if dt=='Stock Entry' and doc.purpose!='Material Transfer':continue
            result.append(full_summary(doc))
    return {'rows':result,'next_start':start+100 if more else None}

def locked_doc_key(doctype,name):
    return 'van-sales:submit:'+hashlib.sha256(f'{doctype}:{name}'.encode()).hexdigest()

def request_envelope(doctype,name,modified,payload):
    return {'action':'update','doctype':doctype,'name':name,'modified':modified,'payload':payload}

@frappe.whitelist(methods=['POST'])
def prepare_pending_edit(request_id,payload):
    """Permanently retire an unsaved request before allowing altered data/new UUID."""
    a=api();a.auth();p=a.parse(payload)
    key,fingerprint=request_identity(frappe.session.user,request_id,p)
    with frappe.cache.lock('van-sales:'+key,timeout=600,blocking_timeout=5):
        old=frappe.db.get_value('Mobile Request',key,['payload_hash','document_type','document_name','abandoned'],as_dict=True)
        if old and old.payload_hash!=fingerprint:frappe.throw(_('بيانات الطلب مختلفة'))
        if old and old.document_name:
            return {'document':full_summary(supported_doc(old.document_type,old.document_name))}
        if not old:
            frappe.get_doc({'doctype':'Mobile Request','request_key':key,'request_user':frappe.session.user,
                'payload_hash':fingerprint,'abandoned':1}).insert(ignore_permissions=True)
            frappe.db.commit()
        # An unconfirmed update is retired; edit the original draft's latest version.
        if isinstance(p,dict) and p.get('action')=='update':
            return {'document':full_summary(supported_doc(p.get('doctype'),p.get('name'))),'retired':True}
        return {'document':None,'retired':True}

def copy_children(rows):
    return [{k:v for k,v in row.as_dict().items() if k not in
        ('name','parent','parenttype','parentfield','doctype','idx','creation','modified','owner','modified_by','docstatus')} for row in rows]

@frappe.whitelist(methods=['POST'])
def update_document(request_id,doctype,name,modified,payload):
    a=api();a.auth();p=a.parse(payload)
    if not isinstance(p,dict):frappe.throw(_('بيانات غير صالحة'))
    envelope=request_envelope(doctype,name,modified,p)
    key,fingerprint=request_identity(frappe.session.user,request_id,envelope)
    with frappe.cache.lock('van-sales:'+key,timeout=600,blocking_timeout=5):
        with frappe.cache.lock(locked_doc_key(doctype,name),timeout=600,blocking_timeout=5):
            old=frappe.db.get_value('Mobile Request',key,['payload_hash','document_name','abandoned'],as_dict=True)
            if old:
                if old.payload_hash!=fingerprint:frappe.throw(_('لا يمكن تغيير بيانات طلب سبق إرساله'))
                if old.abandoned:frappe.throw(_('أُلغي هذا الطلب للتعديل. استخدم الطلب الجديد'))
                return full_summary(supported_doc(doctype,name))
            doc=supported_doc(doctype,name,'write')
            if doc.docstatus!=0:frappe.throw(_('التعديل متاح للمستندات غير المعتمدة فقط'))
            if str(doc.modified)!=str(modified):
                frappe.throw(_('تم تعديل المستند في مكان آخر. أعد فتح أحدث نسخة قبل الحفظ'))
            kind=p.get('kind')
            expected={'invoice':'Sales Invoice','return':'Sales Invoice','receipt':'Payment Entry','transfer':'Stock Entry'}
            if expected.get(kind)!=doctype or (doctype=='Sales Invoice' and bool(doc.is_return)!=(kind=='return')):
                frappe.throw(_('لا يمكن تغيير نوع المستند'))
            if doctype=='Payment Entry' and (doc.payment_type!='Receive' or doc.party_type!='Customer'):frappe.throw(_('سند غير مدعوم'))
            if doctype=='Stock Entry' and doc.purpose!='Material Transfer':frappe.throw(_('حركة مخزون غير مدعومة'))
            candidate={'invoice':a.invoice,'return':a.sales_return,'receipt':a.receipt,'transfer':a.transfer}[kind](p)
            if candidate.company!=doc.company:frappe.throw(_('لا يمكن تغيير شركة المستند؛ افتح إعدادات الشركة الأصلية'))
            fields={
                'Sales Invoice':('customer','posting_date','due_date','selling_price_list','currency','price_list_currency','conversion_rate','plc_conversion_rate','update_stock','set_warehouse','taxes_and_charges','return_against'),
                'Payment Entry':('party','mode_of_payment','posting_date','paid_from','paid_to','paid_from_account_currency','paid_to_account_currency','paid_amount','received_amount','source_exchange_rate','target_exchange_rate','reference_no','reference_date'),
                'Stock Entry':('posting_date','from_warehouse','to_warehouse')}
            doc.update({f:candidate.get(f) for f in fields[doctype]})
            for table in {'Sales Invoice':('items','taxes'),'Payment Entry':('references','deductions'),'Stock Entry':('items',)}[doctype]:
                doc.set(table,copy_children(candidate.get(table) or []))
            # ERPNext normal save validation recalculates rates/taxes/amounts and validates links.
            if doctype=='Payment Entry':doc.setup_party_account_field()
            doc.save()
            frappe.get_doc({'doctype':'Mobile Request','request_key':key,'request_user':frappe.session.user,
                'payload_hash':fingerprint,'document_type':doctype,'document_name':name}).insert(ignore_permissions=True)
            frappe.db.commit()
            return full_summary(doc)

@frappe.whitelist()
def print_data(doctype,name):
    a=api();a.auth();doc=supported_doc(doctype,name,'print')
    result=full_summary(doc);company=a.company_doc(doc.company)
    result.update(company_name=company.get('company_name') or company.name,company_tax_id=company.get('tax_id'),
                  mode_of_payment=doc.get('mode_of_payment'),reference_no=doc.get('reference_no'),
                  return_against=doc.get('return_against'),warehouse=doc.get('set_warehouse'),
                  kind=editable_payload(doc)['kind'],customer_code=doc.get('customer') or doc.get('party'))
    if result['customer_code']:
        try:
            customer=a.read_doc('Customer',result['customer_code'])
            result['customer_name_ar']=a.readable_field(customer,'custom_customer_name_in_arabic')
            result['tax_id']=customer.tax_id
        except frappe.PermissionError:pass
    for row in result['items']:
        try:
            item=a.read_doc('Item',row['item_code'])
            row['item_name_ar']=a.readable_field(item,'custom_item_name_in_arabic') or a.readable_field(item,'custom_item_name_arabic')
        except frappe.PermissionError:pass
    return result

@frappe.whitelist()
def statement_print_data(company,customer,from_date,to_date):
    a=api();a.auth();frappe.has_permission('GL Entry','report',throw=True)
    rows=[];start=0;data=None
    while True:
        data=a.statement(company,customer,from_date,to_date,start)
        rows.extend(data['rows'])
        if data['next_start'] is None:break
        start=data['next_start']
        if len(rows)>20000:frappe.throw(_('اختصر فترة الكشف للطباعة إلى أقل من 20000 حركة'))
    running=flt(data['opening'])
    for r in rows:
        running+=flt(r.debit)-flt(r.credit);r['balance']=running
    co=a.company_doc(company);party=a.read_doc('Customer',customer)
    return dict(data,kind='statement',name=customer,company_name=co.get('company_name') or co.name,
        customer_code=customer,customer_name=party.customer_name,
        customer_name_ar=a.readable_field(party,'custom_customer_name_in_arabic'),
        from_date=from_date,to_date=to_date,rows=rows)

@frappe.whitelist()
def statement_pdf(company,customer,from_date,to_date):
    d=statement_print_data(company,customer,from_date,to_date)
    e=lambda v:escape(str(v or ''))
    n=lambda v:f'{flt(v):,.2f}'
    rows=''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in (r['posting_date'],r['voucher_no'],n(r['debit']),n(r['credit']),n(r['balance'])))+'</tr>' for r in d['rows'])
    html='''<html dir="rtl"><meta charset="utf-8"><style>body{font:12px Arial,sans-serif}h1,h2{text-align:center}table{width:100%;border-collapse:collapse}th,td{border:1px solid #ccd5df;padding:7px;text-align:center}th{background:#eaf0f5}thead{display:table-header-group}tr{page-break-inside:avoid}</style>'''
    html+=f'<h2>{e(d["company_name"])}</h2><h1>كشف حساب عميل / Customer Statement</h1><p>{e(d["customer_name"])} — {e(customer)}<br>{e(from_date)} — {e(to_date)} | {e(d["currency"])}</p>'
    html+=f'<p>الرصيد الافتتاحي / Opening: {n(d["opening"])}</p><table><thead><tr><th>التاريخ / Date</th><th>المستند / Document</th><th>مدين / Debit</th><th>دائن / Credit</th><th>الرصيد / Balance</th></tr></thead><tbody>{rows}</tbody></table>'
    html+=f'<p>المدين / Debit: {n(d["debit"])} | الدائن / Credit: {n(d["credit"])} | الرصيد الختامي / Closing: {n(d["closing"])}</p></html>'
    from frappe.utils.pdf import get_pdf
    frappe.local.response.filename='Customer-Statement.pdf';frappe.local.response.filecontent=get_pdf(html)
    frappe.local.response.type='pdf'
