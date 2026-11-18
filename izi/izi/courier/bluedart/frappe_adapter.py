# -*- coding: utf-8 -*-
"""Connects the Blue Dart client to ERPNext: reads Blue Dart Settings and writes Courier API Log."""

import json

import frappe
from frappe import _

from izi.izi.courier.bluedart.client import BlueDartClient
from izi.izi.courier.bluedart.models import BlueDartConfig
from izi.izi.courier.bluedart.transport import RequestsTransport

LOG_PAYLOAD_LIMIT = 100000


def get_client(require_enabled=True):
    s = frappe.get_single("Blue Dart Settings")
    if require_enabled and not s.enabled:
        frappe.throw(_("Blue Dart is switched off in Blue Dart Settings."))

    client_secret = s.get_password("client_secret", raise_exception=False)
    licence_key = s.get_password("licence_key", raise_exception=False)
    required = [
        (_("Base URL"), s.base_url),
        (_("Client ID"), s.client_id),
        (_("Client Secret"), client_secret),
        (_("LoginID"), s.login_id),
        (_("Licence Key"), licence_key),
        (_("Area Code"), s.area_code),
        (_("Customer Code"), s.customer_code),
    ]
    missing = [label for label, value in required if not value]
    if missing:
        frappe.throw(_("Fill these in Blue Dart Settings first: {0}").format(", ".join(missing)))

    config = BlueDartConfig(
        base_url=s.base_url.rstrip("/"),
        client_id=s.client_id,
        client_secret=client_secret,
        login_id=s.login_id,
        licence_key=licence_key,
        api_type=s.api_type or "S",
        area_code=s.area_code,
        customer_code=s.customer_code,
        timeout_seconds=float(s.timeout_seconds or 60),
    )
    return BlueDartClient(config, RequestsTransport(), log=write_log)


def write_log(entry):
    """Store one API call. Commits so the record survives a later failure in the same job."""
    try:
        frappe.get_doc({
            "doctype": "Courier API Log",
            "courier": "Blue Dart",
            "operation": entry.get("operation"),
            "reference": entry.get("reference"),
            "url": entry.get("url"),
            "http_status": entry.get("http_status"),
            "outcome": entry.get("outcome"),
            "duration_ms": entry.get("duration_ms"),
            "request_payload": _as_text(entry.get("request")),
            "response_payload": _as_text(entry.get("response")),
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(title="Blue Dart: could not write Courier API Log", message=frappe.get_traceback())


def _as_text(value):
    if value is None:
        return ""
    return json.dumps(value, indent=1, ensure_ascii=False, default=str)[:LOG_PAYLOAD_LIMIT]