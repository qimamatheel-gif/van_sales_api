from frappe.model.document import Document
from van_sales_api.settings import validate_document


class MobileSalesSettings(Document):
    def validate(self):
        validate_document(self)
