# -*- coding: utf-8 -*-
"""Turns Blue Dart StatusCode values into messages a user can act on.

Keys are the status code lower-cased with every non-alphanumeric character removed,
so 'InvalidAddress1/2/3' and 'InvalidAreaCustomer Code' both match.
Unknown codes fall back to Blue Dart's own StatusInformation text.
"""

import re

_MESSAGES = {
    "userdoesnotexists": "Blue Dart rejected the API login. Check the LoginID and LicenceKey.",
    "communicationfailure": "Blue Dart's shipping service could not reach its legacy system. Retry later.",
    "invalidpincode": "Pincode must be six digits.",
    "invalidproductcode": "Product code is not valid for Blue Dart's product master.",
    "invalidsubproduct": "Sub product code is not valid for Blue Dart's product master.",
    "invalidareacode": "Area code is not part of Blue Dart's master.",
    "invalidclientcode": "Area and customer code are not in Blue Dart's customer master.",
    "unauthorizeduser": "This login is not linked to the customer code and area.",
    "invalidclientname": "Shipper or consignee name has invalid characters (< or >) or is longer than 30 characters.",
    "invalidaddress123": "An address line has invalid characters (< or >) or is longer than 30 characters.",
    "invalidareacustomercode": "Customer code does not exist for this origin area.",
    "invalidareascnotinregion": "Pincode is outside the service region of the origin area.",
    "invalidcustnotsendcodshpt": "This customer does not have COD facility.",
    "invalidcollectableamount": "COD amount is invalid. It must be above zero and not more than the declared value for COD, and must be zero for prepaid.",
    "invalidpickupdate": "Pickup date cannot be earlier than today.",
    "invalidcommoditydetail123": "Commodity detail must be letters or digits, up to 30 characters.",
    "excessdimension": "The number of dimension entries must equal the number of pieces.",
    "blankcontactno": "A consignee mobile, telephone or masked contact number is required.",
    "invalidmobile": "Mobile number must be 10 to 15 digits.",
    "invalidemailid": "Email address is not valid.",
    "invalidawbno": "Waybill number is not valid for this product.",
    "invalidawblength": "Waybill number length is not valid.",
    "awbseriesproductcodemismatch": "Waybill series does not match the product code.",
    "cancelfailure": "This waybill can no longer be cancelled. It may already be in transit.",
    "shipmentalreadypicked": "This shipment is already manifested.",
    "pickupisalreadyregister": "A pickup is already registered for this waybill.",
}


def describe(status_items):
    """Return (status_code, message) from Blue Dart's Status array."""
    items = [i for i in (status_items or []) if isinstance(i, dict)]
    if not items:
        return "", "Blue Dart refused the request without giving a reason."
    first = items[0]
    code = str(first.get("StatusCode") or "").strip()
    info = str(first.get("StatusInformation") or "").strip()
    key = re.sub(r"[^a-z0-9]", "", code.lower())
    if key in _MESSAGES:
        return code, _MESSAGES[key]
    if info:
        return code, f"Blue Dart: {info}"
    return code, f"Blue Dart refused the request (code {code or 'unknown'})."