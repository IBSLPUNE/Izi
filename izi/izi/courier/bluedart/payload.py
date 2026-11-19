# -*- coding: utf-8 -*-
"""Request bodies for Blue Dart. The waybill body sits under a "Request" key, as in the TSD and the cancel sample."""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def pickup_date_token(pickup_at, pickup_time):
    """PickupDate is /Date(<epoch ms>)/ for the pickup day and time in IST."""
    hh, mm = int(pickup_time[:2]), int(pickup_time[2:])
    when = datetime(pickup_at.year, pickup_at.month, pickup_at.day, hh, mm, tzinfo=IST)
    return f"/Date({int(when.timestamp() * 1000)})/"


def build_waybill_payload(s, cfg):
    c, sh = s.consignee, s.shipper
    cod = s.sub_product_code == "C"
    request = {
        "Consignee": {
            "ConsigneeAddress1": c.address1,
            "ConsigneeAddress2": c.address2,
            "ConsigneeAddress3": c.address3,
            "ConsigneeMobile": c.mobile,
            "ConsigneeTelephone": c.telephone,
            "ConsigneeName": c.name,
            "ConsigneePincode": c.pincode,
            "ConsigneeMaskedContactNumber": c.masked_number,
            "ConsigneeEmailID": c.email,
            "ConsigneeAddressinfo": "",
            "ConsigneeGSTNumber": "",
        },
        # Blank, as in Blue Dart's working sample. To confirm: whether to send our own return address.
        "Returnadds": {
            "ManifestNumber": "", "ReturnAddress1": "", "ReturnAddress2": "", "ReturnAddress3": "",
            "ReturnContact": "", "ReturnEmailID": "", "ReturnLatitude": "", "ReturnLongitude": "",
            "ReturnMaskedContactNumber": "", "ReturnMobile": "", "ReturnPincode": "", "ReturnTelephone": "",
        },
        "Services": {
            "ActualWeight": float(s.weight_kg),
            "Commodity": {"CommodityDetail1": s.commodity, "CommodityDetail2": "", "CommodityDetail3": ""},
            "CreditReferenceNo": s.reference,
            "DeclaredValue": f"{s.declared_value:.2f}",
            "CollectableAmount": f"{(s.collectable_amount if cod else 0):.2f}",
            "Dimensions": [
                {"Breadth": f"{breadth:g}", "Count": "1", "Height": f"{height:g}", "Length": f"{length:g}"}
                for (length, breadth, height) in s.dimensions_cm
            ],
            "PickupDate": pickup_date_token(s.pickup_at, s.pickup_time),
            "PickupTime": s.pickup_time,
            "PieceCount": int(s.pieces),
            "ProductCode": s.product_code,
            "SubProductCode": s.sub_product_code,
            "PackType": "",
            "ProductType": "1",
            "PDFOutputNotRequired": False,  # we need the label back; the sample sets this to true
            "IsMPSLabelRequired": False,
            "PrinterLableSize": 0,
            "RegisterPickup": False,
            "SpecialInstruction": s.special_instruction,
        },
        "Shipper": {
            "CustomerAddress1": sh.address1,
            "CustomerAddress2": sh.address2,
            "CustomerAddress3": sh.address3,
            "CustomerCode": cfg.customer_code,
            "CustomerMobile": sh.mobile,
            "CustomerEmailID": sh.email,
            "CustomerName": sh.name,
            "CustomerPincode": sh.pincode,
            "IsToPayCustomer": False,
            "OriginArea": cfg.area_code,
            "Sender": sh.name,
            "VendorCode": "",
        },
    }
    return {
        "Request": request,
        "Profile": {
            "Api_type": cfg.api_type,
            "Area": cfg.area_code,
            "LicenceKey": cfg.licence_key,
            "LoginID": cfg.login_id,
        },
    }


def build_cancel_payload(awb, cfg):
    return {
        "Request": {"AWBNo": awb},
        "Profile": {"Api_type": cfg.api_type, "LicenceKey": cfg.licence_key, "LoginID": cfg.login_id},
    }