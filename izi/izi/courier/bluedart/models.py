# -*- coding: utf-8 -*-
"""Plain data objects for the courier layer. No Frappe imports."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple


@dataclass
class Party:
    """Shipper or consignee. Lengths and characters are checked in validation.py."""

    name: str
    address1: str
    address2: str = ""
    address3: str = ""
    pincode: str = ""
    mobile: str = ""
    telephone: str = ""
    masked_number: str = ""
    email: str = ""


@dataclass
class ShipmentInput:
    """Everything needed to create one waybill. `reference` is sent as CreditReferenceNo and must be unique."""

    reference: str
    product_code: str  # A = Air/Apex, E = Ground, D = Domestic Priority
    sub_product_code: str  # "" = none, P = prepaid, C = COD
    pickup_at: datetime  # only the date is used
    pickup_time: str  # HHMM, 24-hour, IST
    weight_kg: float
    pieces: int
    dimensions_cm: List[Tuple[float, float, float]]  # one (length, breadth, height) per piece
    shipper: Party
    consignee: Party
    commodity: str  # CommodityDetail1: letters and digits, max 30
    declared_value: float
    collectable_amount: float = 0.0  # COD only
    special_instruction: str = ""


@dataclass
class BlueDartConfig:
    base_url: str  # e.g. https://apigateway.bluedart.com/in/transportation
    client_id: str
    client_secret: str
    login_id: str
    licence_key: str
    api_type: str = "S"
    area_code: str = ""
    customer_code: str = ""
    timeout_seconds: float = 60.0  # above APIGEE's 55 s backend timeout, so we receive its 504 instead of timing out first


@dataclass
class WaybillResult:
    awb: str
    label_pdf: Optional[bytes]
    raw: dict = field(repr=False)