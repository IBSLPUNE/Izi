# -*- coding: utf-8 -*-
"""Blue Dart booking started from a submitted Delivery Note.

Booking state and the per-shipment options (payment mode, product, pickup time, commodity,
COD amount) live in the Blue Dart Booking DocType, one record per Shipment.
Booking order: check the options, mark the attempt, call Blue Dart, then record the result.
If the result is unknown, the shipment stays blocked until someone checks the Blue Dart portal.
"""

import re
from datetime import date

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, today

from izi.izi.courier.bluedart.frappe_adapter import get_client
from izi.izi.courier.bluedart.mapping import MappingError, build_shipment_input, clean_text
from izi.izi.courier.exceptions import CourierError, CourierOutcomeUnknown

PAYMENT_SUB_PRODUCT = {"Prepaid": "P", "COD": "C"}
PRODUCT_CODES = ("A", "E", "D")

BOOKING = "Blue Dart Booking"
NOT_BOOKED = "Not Booked"
PENDING = "Booking Pending"
BOOKED = "Booked"
UNKNOWN = "Booking Unknown"
CANCELLED = "Cancelled"

SHIPMENT_DN_FIELD = "shipment_delivery_note"   # table field on Shipment
SHIPMENT_DN_DOCTYPE = "Shipment Delivery Note"  # its child DocType
SHIPMENT_PARCEL_FIELD = "shipment_parcel"


@frappe.whitelist()
def get_booking(shipment):
    """Used by the Shipment form: buttons, downloads, and the starting values for the booking dialog."""
    frappe.get_doc("Shipment", shipment).check_permission("read")
    b = _booking(shipment)
    return {
        "status": (b.status if b else None) or NOT_BOOKED,
        "awb": (b.awb if b else "") or "",
        "label": (b.label if b else "") or "",
        "message": (b.message if b else "") or "",
        "invoice": _invoice_for_shipment(shipment),
        "amount": flt(frappe.db.get_value("Shipment", shipment, "value_of_goods")),
        "options": _option_values(b),
    }


@frappe.whitelist()
def create_shipment_from_delivery_note(delivery_note):
    dn = frappe.get_doc("Delivery Note", delivery_note)
    dn.check_permission("read")
    if dn.docstatus != 1:
        frappe.throw(_("Submit the Delivery Note before creating a Blue Dart shipment."))
    if not dn.get("shipping_address_name"):
        frappe.throw(_("Delivery Note {0} has no shipping address.").format(dn.name))

    active = _active_shipment_for(dn.name)
    if active:
        frappe.throw(_("Shipment {0} already exists for this Delivery Note. Cancel it or open it instead.").format(active))

    settings = frappe.get_single("Blue Dart Settings")
    shipment = frappe.new_doc("Shipment")

    # Pickup: the company's address in ERPNext (IZI's dispatch address).
    shipment.pickup_from_type = "Company"
    shipment.pickup_company = dn.company
    shipment.pickup_address_name = _company_address(dn.company)
    shipment.pickup_date = today()
    shipment.pickup_from = "10:00:00"
    shipment.pickup_to = _time_from_hhmm(settings.default_pickup_time or "1700")

    # Delivery: the customer on the Delivery Note.
    shipment.delivery_to_type = "Customer"
    shipment.delivery_customer = dn.customer
    shipment.delivery_address_name = dn.shipping_address_name
    if dn.get("contact_person"):
        shipment.delivery_contact_name = dn.contact_person

    shipment.shipment_type = "Goods"
    shipment.pickup_type = "Pickup"
    shipment.value_of_goods = flt(dn.grand_total)
    shipment.description_of_content = _description(dn)
    shipment.carrier = "Blue Dart"
    shipment.service_provider = "Blue Dart"

    shipment.append(SHIPMENT_DN_FIELD, {"delivery_note": dn.name, "grand_total": flt(dn.grand_total)})

    # One parcel row from the item weights. Dimensions are entered by the user.
    weight = 0.0
    for item in dn.items:
        weight += flt(frappe.db.get_value("Item", item.item_code, "weight_per_unit")) * flt(item.qty)
    if weight > 0:
        shipment.append(SHIPMENT_PARCEL_FIELD, {"count": 1, "weight": round(weight, 3)})

    shipment.insert()
    _ensure_booking(shipment.name, dn.name)
    return shipment.name


@frappe.whitelist()
def book_shipment(shipment, options=None):
    sh = frappe.get_doc("Shipment", shipment)
    sh.check_permission("write")
    if sh.docstatus == 2:
        frappe.throw(_("This shipment is cancelled in ERPNext."))

    dn = _linked_delivery_note(sh)
    b = _ensure_booking(sh.name, dn.name)
    if b.status == BOOKED:
        frappe.throw(_("This shipment is already booked. AWB {0}.").format(b.awb))
    if b.status == PENDING:
        frappe.throw(_("A booking for this shipment has started but has no result. Check the Blue Dart portal before retrying."))
    if b.status == UNKNOWN:
        frappe.throw(_("Blue Dart did not confirm the last booking. Check the portal, then ask a System Manager to resolve it."))
    if b.status == CANCELLED:
        frappe.throw(_("This shipment was cancelled. Create a new Shipment from the Delivery Note."))

    opts = _clean_options(options, sh)  # checks everything before anything is written
    client = get_client()  # fails early, before anything is marked
    attempt = int(b.attempts or 0) + 1
    shipment_input = _build_input(sh, dn, attempt, opts)

    _set(b, dict(opts, status=PENDING, reference=shipment_input.reference, attempts=attempt, message=""))
    frappe.db.commit()

    try:
        result = client.generate_waybill(shipment_input)
    except CourierOutcomeUnknown as exc:
        _set(b, {"status": UNKNOWN, "message": exc.message})
        frappe.db.commit()
        frappe.throw(exc.message)
    except CourierError as exc:
        _set(b, {"status": NOT_BOOKED, "message": exc.message})
        frappe.db.commit()
        frappe.throw(exc.message)

    label_url = _save_label(b, result)
    _set(b, {
        "status": BOOKED,
        "awb": result.awb,
        "label": label_url,
        "booked_at": now_datetime(),
        "message": "" if label_url else _("Waybill booked, but the label could not be saved. Download it from the Blue Dart portal using the AWB."),
    })
    if sh.meta.has_field("awb_number"):
        frappe.db.set_value("Shipment", sh.name, "awb_number", result.awb, update_modified=False)
    frappe.db.commit()
    return {"awb": result.awb, "label": label_url}


@frappe.whitelist()
def cancel_shipment(shipment):
    frappe.get_doc("Shipment", shipment).check_permission("write")
    b = _booking(shipment)
    if not b or b.status != BOOKED:
        frappe.throw(_("Only a booked shipment can be cancelled."))

    try:
        get_client().cancel_waybill(b.awb)
    except CourierError as exc:
        frappe.throw(exc.message)

    _set(b, {"status": CANCELLED, "message": ""})
    frappe.db.commit()
    return {"cancelled": b.awb}


@frappe.whitelist()
def resolve_unknown_booking(shipment, awb=""):
    """For a System Manager after checking the Blue Dart portal.

    With an AWB: records the waybill as booked. Without one: releases the shipment so it can be booked again.
    """
    frappe.only_for("System Manager")
    b = _booking(shipment)
    if not b or b.status not in (PENDING, UNKNOWN):
        frappe.throw(_("Only a shipment whose booking is unconfirmed can be resolved here."))

    awb = clean_text(awb)
    if awb:
        if not (awb.isdigit() and len(awb) in (11, 12)):
            frappe.throw(_("AWB must be 11 or 12 digits."))
        _set(b, {"status": BOOKED, "awb": awb, "booked_at": now_datetime(), "message": ""})
        if frappe.get_meta("Shipment").has_field("awb_number"):
            frappe.db.set_value("Shipment", shipment, "awb_number", awb, update_modified=False)
    else:
        _set(b, {"status": NOT_BOOKED, "message": "Resolved manually: no Blue Dart waybill exists for this attempt."})
    frappe.db.commit()
    return {"status": BOOKED if awb else NOT_BOOKED}


# ---------------------------------------------------------------- booking record

def _booking(shipment_name):
    if frappe.db.exists(BOOKING, shipment_name):
        return frappe.get_doc(BOOKING, shipment_name)
    return None


def _ensure_booking(shipment_name, dn_name):
    b = _booking(shipment_name)
    if b:
        return b
    settings = frappe.get_single("Blue Dart Settings")
    defaults = {
        "payment_mode": "COD" if (settings.default_sub_product_code or "") == "C" else "Prepaid",
        "product_code": settings.default_product_code or "E",
        "pickup_time": settings.default_pickup_time or "1700",
        "commodity": settings.default_commodity or "",
        "collectable_amount": 0,
    }
    return frappe.get_doc(dict(defaults, doctype=BOOKING, shipment=shipment_name, delivery_note=dn_name,
                               status=NOT_BOOKED, attempts=0)).insert(ignore_permissions=True)


def _option_values(b):
    """Starting values for the booking dialog: the booking's values, or the settings if not yet created."""
    if b:
        return {
            "payment_mode": b.payment_mode or "Prepaid",
            "product_code": b.product_code or "E",
            "pickup_time": b.pickup_time or "1700",
            "commodity": b.commodity or "",
            "collectable_amount": flt(b.collectable_amount),
        }
    settings = frappe.get_single("Blue Dart Settings")
    return {
        "payment_mode": "COD" if (settings.default_sub_product_code or "") == "C" else "Prepaid",
        "product_code": settings.default_product_code or "E",
        "pickup_time": settings.default_pickup_time or "1700",
        "commodity": settings.default_commodity or "",
        "collectable_amount": 0,
    }


def _clean_options(options, sh):
    """Checks the values chosen in the booking dialog. Raises before anything is written."""
    if isinstance(options, str):
        options = frappe.parse_json(options) if options else {}
    options = options or {}

    mode = options.get("payment_mode") or "Prepaid"
    if mode not in PAYMENT_SUB_PRODUCT:
        frappe.throw(_("Payment mode must be Prepaid or COD."))

    product = options.get("product_code") or "E"
    if product not in PRODUCT_CODES:
        frappe.throw(_("Product must be A (Air), E (Ground) or D (Domestic Priority)."))
    if mode == "COD" and product == "D":
        frappe.throw(_("COD is available for Air (A) and Ground (E) only."))

    pickup = str(options.get("pickup_time") or "1700").strip()
    if not re.fullmatch(r"\d{4}", pickup):
        frappe.throw(_("Pickup time must be HHMM in 24-hour format."))

    collectable = flt(options.get("collectable_amount"))
    if mode == "COD":
        if collectable <= 0:
            collectable = flt(sh.get("value_of_goods"))
        if collectable <= 0:
            frappe.throw(_("Enter the amount to collect on delivery."))

    return {
        "payment_mode": mode,
        "product_code": product,
        "pickup_time": pickup,
        "commodity": clean_text(options.get("commodity") or ""),
        "collectable_amount": collectable if mode == "COD" else 0,
    }


def _set(doc, values):
    """Write fields without validation, skipping any field the DocType does not have."""
    values = {k: v for k, v in values.items() if doc.meta.has_field(k)}
    doc.db_set(values, update_modified=False)


# ---------------------------------------------------------------- helpers

def _build_input(sh, dn, attempt, opts):
    if not dn.get("shipping_address_name"):
        frappe.throw(_("Delivery Note {0} has no shipping address.").format(dn.name))
    address = frappe.get_doc("Address", dn.shipping_address_name)
    settings = frappe.get_single("Blue Dart Settings")

    consignee = {
        "name": dn.get("customer_name") or dn.customer,
        "address_lines": [address.address_line1, address.address_line2, address.city],
        "pincode": address.pincode,
        "mobile": dn.get("contact_mobile") or address.phone,
        "email": dn.get("contact_email") or address.email_id,
    }
    shipper = {
        "name": settings.shipper_name,
        "address_lines": [settings.shipper_address1, settings.shipper_address2, settings.shipper_address3],
        "pincode": settings.shipper_pincode,
        "mobile": settings.shipper_mobile,
        "email": settings.shipper_email,
    }
    parcels = [
        {"count": r.get("count"), "length": r.get("length"), "width": r.get("width"),
         "height": r.get("height"), "weight": r.get("weight")}
        for r in (sh.get("shipment_parcel") or [])
    ]
    try:
        return build_shipment_input(
            shipment_name=sh.name,
            attempt=attempt,
            consignee=consignee,
            shipper=shipper,
            parcels=parcels,
            product_code=opts["product_code"],
            sub_product_code=PAYMENT_SUB_PRODUCT[opts["payment_mode"]],
            pickup_date=sh.get("pickup_date") or date.today(),
            pickup_time=opts["pickup_time"],
            commodity=opts["commodity"] or settings.default_commodity or "",
            declared_value=flt(sh.get("value_of_goods") or dn.grand_total),
            collectable_amount=opts["collectable_amount"],
            special_instruction=dn.name,
        )
    except MappingError as exc:
        frappe.throw(_("Cannot book with Blue Dart: {0}").format(" ".join(exc.problems)))


def _save_label(b, result):
    if not result.label_pdf:
        return ""
    try:
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": f"BlueDart-{result.awb}.pdf",
            "content": result.label_pdf,
            "is_private": 1,
            "attached_to_doctype": BOOKING,
            "attached_to_name": b.name,
        }).insert(ignore_permissions=True)
        return file_doc.file_url
    except Exception:
        frappe.log_error(title="Blue Dart: label not saved", message=frappe.get_traceback())
        return ""


def _linked_delivery_note(sh):
    rows = sh.get(SHIPMENT_DN_FIELD) or []
    if not rows:
        frappe.throw(_("This shipment has no Delivery Note."))
    if len(rows) > 1:
        frappe.throw(_("One Blue Dart waybill per Delivery Note for now. Remove the extra rows."))
    return frappe.get_doc("Delivery Note", rows[0].delivery_note)


def _active_shipment_for(dn_name):
    for row in frappe.get_all(SHIPMENT_DN_DOCTYPE, filters={"delivery_note": dn_name, "parenttype": "Shipment"}, fields=["parent"]):
        docstatus = frappe.db.get_value("Shipment", row.parent, "docstatus")
        if docstatus is None or docstatus >= 2:
            continue
        if frappe.db.get_value(BOOKING, row.parent, "status") != CANCELLED:
            return row.parent
    return None


def _company_address(company):
    names = frappe.get_all(
        "Dynamic Link",
        filters={"parenttype": "Address", "link_doctype": "Company", "link_name": company},
        pluck="parent",
        limit=1,
    )
    if not names:
        frappe.throw(_("Company {0} has no address. Add one under Company before creating a Blue Dart shipment.").format(company))
    return names[0]


def _time_from_hhmm(hhmm):
    return f"{hhmm[:2]}:{hhmm[2:]}:00"


def _description(dn):
    names = [it.item_name or it.item_code for it in dn.items]
    return ", ".join(dict.fromkeys(names))[:140]


def _table_child(doctype, table_fieldname):
    field = frappe.get_meta(doctype).get_field(table_fieldname)
    if not field:
        frappe.throw(_("{0} has no table {1}. Check the Blue Dart setup.").format(doctype, table_fieldname))
    return field.options


def _link_fieldname(child_doctype, target):
    for field in frappe.get_meta(child_doctype).fields:
        if field.fieldtype == "Link" and field.options == target:
            return field.fieldname
    frappe.throw(_("The shipment table is missing a link to {0}.").format(target))


def _row_values(child_doctype, values):
    meta = frappe.get_meta(child_doctype)
    return {k: v for k, v in values.items() if meta.has_field(k)}


def _set_if_field(doc, fieldname, value):
    if value not in (None, "") and doc.meta.has_field(fieldname):
        doc.set(fieldname, value)


def _invoice_for_shipment(shipment_name):
    """The submitted Sales Invoice raised against this shipment's Delivery Note, or an empty string."""
    dn_names = frappe.get_all(
        SHIPMENT_DN_DOCTYPE,
        filters={"parent": shipment_name, "parenttype": "Shipment"},
        pluck="delivery_note",
    )
    for dn_name in dn_names:
        invoices = frappe.get_all(
            "Sales Invoice Item",
            filters={"delivery_note": dn_name, "docstatus": 1},
            pluck="parent",
            limit=1,
        )
        if invoices:
            return invoices[0]
    return ""