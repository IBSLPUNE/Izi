# -*- coding: utf-8 -*-
"""Turns ERPNext values (plain dicts) into a ShipmentInput. No Frappe imports, so it is unit-tested."""

import re
import textwrap
from datetime import datetime, time

from izi.izi.courier.bluedart.models import Party, ShipmentInput
from izi.izi.courier.bluedart.validation import EMAIL, party_errors, validate_shipment

NAME_MAX = 30
ADDRESS_LINE_MAX = 30
ADDRESS_LINES = 3
REFERENCE_MAX = 20


class MappingError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(" ".join(self.problems))


def clean_text(value):
    """Blue Dart rejects < and > in names and addresses. Replace them and collapse spaces."""
    text = re.sub(r"[<>]", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def clean_name(value):
    return clean_text(value)[:NAME_MAX].rstrip()


def split_address(lines):
    """Join the address lines and wrap them into Blue Dart's three 30-character address fields."""
    text = ", ".join(part for part in (clean_text(line) for line in lines) if part)
    if not text:
        raise MappingError(["Address is empty."])
    chunks = textwrap.wrap(text, ADDRESS_LINE_MAX, break_long_words=True, break_on_hyphens=False)
    if len(chunks) > ADDRESS_LINES:
        raise MappingError([
            f"Address is too long for Blue Dart: it needs more than {ADDRESS_LINES} lines of {ADDRESS_LINE_MAX} characters."
        ])
    return chunks + [""] * (ADDRESS_LINES - len(chunks))


def digits_only_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if 10 <= len(digits) <= 15 else ""


def clean_email(value):
    value = clean_text(value)
    return value if value and EMAIL.match(value) else ""


def make_reference(shipment_name, attempt):
    """CreditReferenceNo must be unique. Each booking attempt gets its own suffix."""
    if attempt > 999:
        raise MappingError(["Too many booking attempts for this shipment."])
    base = re.sub(r"[^A-Za-z0-9]", "", shipment_name)[:16]
    return f"{base}{attempt:03d}"[:REFERENCE_MAX]


def parcels_to_dimensions(parcels):
    """Each parcel row has a count. Blue Dart wants one (length, breadth, height) per piece."""
    if not parcels:
        raise MappingError(["Add at least one parcel row with length, width, height and weight."])
    dims, weight, pieces, problems = [], 0.0, 0, []
    for index, row in enumerate(parcels, start=1):
        count = int(row.get("count") or 1)
        length, width, height = (float(row.get(k) or 0) for k in ("length", "width", "height"))
        piece_weight = float(row.get("weight") or 0)
        if count < 1 or min(length, width, height) <= 0:
            problems.append(f"Parcel row {index}: enter length, width and height in cm.")
            continue
        if piece_weight <= 0:
            problems.append(f"Parcel row {index}: enter the weight in kg.")
            continue
        dims.extend([(length, width, height)] * count)
        weight += piece_weight * count
        pieces += count
    if problems:
        raise MappingError(problems)
    return dims, round(weight, 3), pieces


def _party(data, label):
    lines = split_address(data.get("address_lines") or [])
    return Party(
        name=clean_name(data.get("name")),
        address1=lines[0],
        address2=lines[1],
        address3=lines[2],
        pincode=str(data.get("pincode") or "").strip(),
        mobile=digits_only_phone(data.get("mobile")),
        email=clean_email(data.get("email")),
    )


def build_shipment_input(
    *,
    shipment_name,
    attempt,
    consignee,
    shipper,
    parcels,
    product_code,
    sub_product_code,
    pickup_date,
    pickup_time,
    commodity,
    declared_value,
    collectable_amount=0.0,
    special_instruction="",
    today=None,
):
    """Build and validate a ShipmentInput. Raises MappingError listing every problem found."""
    problems = []
    dims, weight, pieces = [], 0.0, 0
    try:
        dims, weight, pieces = parcels_to_dimensions(parcels)
    except MappingError as exc:
        problems += exc.problems

    consignee_party = shipper_party = None
    for label, data in (("Consignee", consignee), ("Shipper", shipper)):
        try:
            party = _party(data, label)
        except MappingError as exc:
            problems += [f"{label}: {p}" for p in exc.problems]
            continue
        if label == "Consignee":
            consignee_party = party
        else:
            shipper_party = party

    reference = ""
    try:
        reference = make_reference(shipment_name, attempt)
    except MappingError as exc:
        problems += exc.problems

    if consignee_party is not None:
        problems += party_errors("Consignee", consignee_party)
    if shipper_party is not None:
        problems += party_errors("Shipper", shipper_party)
    if problems or consignee_party is None or shipper_party is None:
        raise MappingError(problems or ["Consignee or shipper address could not be read."])

    inp = ShipmentInput(
        reference=reference,
        product_code=product_code,
        sub_product_code=sub_product_code or "",
        pickup_at=datetime.combine(pickup_date, time.min),
        pickup_time=pickup_time,
        weight_kg=weight,
        pieces=pieces,
        dimensions_cm=dims,
        shipper=shipper_party,
        consignee=consignee_party,
        commodity=clean_text(commodity),
        declared_value=float(declared_value or 0),
        collectable_amount=float(collectable_amount or 0),
        special_instruction=clean_text(special_instruction)[:50],
    )
    problems = validate_shipment(inp, today=today)
    if problems:
        raise MappingError(problems)
    return inp