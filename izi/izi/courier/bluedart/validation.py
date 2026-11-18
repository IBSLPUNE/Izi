# -*- coding: utf-8 -*-
"""Checks run before anything is sent. Each rule matches a rejection in Blue Dart's error list."""

import re
from datetime import date

NAME_MAX = 30
ADDRESS_MAX = 30
FORBIDDEN = re.compile(r"[<>]")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_shipment(s, *, today=None):
    """Return a list of problems. An empty list means the shipment can be sent."""
    today = today or date.today()
    errors = []

    if not re.fullmatch(r"[A-Za-z0-9]{1,20}", s.reference or ""):
        errors.append("Reference must be 1 to 20 letters or digits and unique per shipment.")
    if s.product_code not in ("A", "E", "D"):
        errors.append("Product code must be A (Air/Apex), E (Ground) or D (Domestic Priority).")
    if s.sub_product_code not in ("", "P", "C"):
        errors.append("Sub product must be blank, P (prepaid) or C (COD).")
    if not _valid_hhmm(s.pickup_time):
        errors.append("Pickup time must be HHMM in 24-hour format.")
    if s.pickup_at.date() < today:
        errors.append("Pickup date cannot be earlier than today.")
    if s.pieces < 1:
        errors.append("Pieces must be at least 1.")
    if not s.weight_kg or s.weight_kg <= 0:
        errors.append("Weight must be greater than zero.")
    if len(s.dimensions_cm) != s.pieces:
        errors.append("Enter one set of dimensions for each piece.")
    elif any(not (0 < v <= 999) for dims in s.dimensions_cm for v in dims):
        errors.append("Each dimension must be greater than 0 and at most 999 cm.")

    errors += party_errors("Shipper", s.shipper)
    errors += party_errors("Consignee", s.consignee)
    if not (s.consignee.mobile or s.consignee.telephone or s.consignee.masked_number):
        errors.append("Consignee needs a mobile, telephone or masked contact number.")

    if not re.fullmatch(r"[A-Za-z0-9 ]{1,30}", s.commodity or ""):
        errors.append("Commodity must be letters or digits, up to 30 characters.")
    special = s.special_instruction or ""
    if len(special) > 50 or FORBIDDEN.search(special):
        errors.append("Special instruction must be at most 50 characters and must not contain < or >.")

    if s.sub_product_code == "C":
        if s.collectable_amount <= 0:
            errors.append("COD shipments need a collectable amount greater than zero.")
        elif s.collectable_amount > s.declared_value:
            errors.append("COD collectable amount cannot exceed the declared value.")
    else:
        if s.collectable_amount != 0:
            errors.append("Collectable amount is only allowed for COD shipments.")
        if s.declared_value <= 0:
            errors.append("Declared value must be greater than zero for prepaid shipments.")
    return errors


def party_errors(label, p):
    """Checks for a shipper or consignee. Public so the mapping layer can report everything at once."""
    errors = []
    if not p.name or FORBIDDEN.search(p.name) or len(p.name) > NAME_MAX:
        errors.append(f"{label} name is required, at most 30 characters, and must not contain < or >.")
    if not p.address1:
        errors.append(f"{label} address line 1 is required.")
    for line_no, value in ((1, p.address1), (2, p.address2), (3, p.address3)):
        if value and (len(value) > ADDRESS_MAX or FORBIDDEN.search(value)):
            errors.append(f"{label} address line {line_no} must be at most 30 characters and must not contain < or >.")
    if not re.fullmatch(r"\d{6}", p.pincode or ""):
        errors.append(f"{label} pincode must be six digits.")
    if p.mobile and not re.fullmatch(r"\d{10,15}", p.mobile):
        errors.append(f"{label} mobile must be 10 to 15 digits.")
    if p.telephone and not re.fullmatch(r"\d{6,15}", p.telephone):
        errors.append(f"{label} telephone must be 6 to 15 digits.")
    if p.email and not EMAIL.match(p.email):
        errors.append(f"{label} email is not valid.")
    return errors


def _valid_hhmm(value):
    if not re.fullmatch(r"\d{4}", value or ""):
        return False
    return int(value[:2]) < 24 and int(value[2:]) < 60