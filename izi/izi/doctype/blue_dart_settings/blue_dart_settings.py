# -*- coding: utf-8 -*-
import frappe
from frappe import _
from frappe.model.document import Document


class BlueDartSettings(Document):
    def validate(self):
        if self.base_url:
            self.base_url = self.base_url.strip().rstrip("/")
            if not self.base_url.startswith("https://"):
                frappe.throw(_("Base URL must start with https://"))
        if self.timeout_seconds and not (10 <= int(self.timeout_seconds) <= 120):
            frappe.throw(_("Timeout must be between 10 and 120 seconds."))
        if self.enabled and not all([self.base_url, self.client_id, self.login_id, self.area_code, self.customer_code]):
            frappe.throw(_("Fill in the base URL, client ID, LoginID, area code and customer code before enabling Blue Dart."))


@frappe.whitelist()
def test_connection():
    """Fetch a login token only. Creates no shipment and no waybill."""
    frappe.only_for("System Manager")
    from izi.izi.courier.bluedart.frappe_adapter import get_client

    get_client(require_enabled=False).authenticate()
    return _("Blue Dart login succeeded. No shipment was created.")