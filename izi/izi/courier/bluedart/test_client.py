# -*- coding: utf-8 -*-
"""Tests for the Blue Dart client. No network: every HTTP call goes through FakeTransport.

Run from the app folder:  cd apps/izi && python3 -m unittest izi.izi.courier.bluedart.test_client -v
"""

import base64
import json
import unittest
from datetime import date, datetime, timedelta

from izi.izi.courier.bluedart.client import (
    CANCEL_PATH,
    GENERATE_PATH,
    TOKEN_PATH,
    BlueDartClient,
    find_token,
)
from izi.izi.courier.bluedart.models import BlueDartConfig, Party, ShipmentInput
from izi.izi.courier.bluedart.transport import HttpResponse, TransportError, TransportTimeout
from izi.izi.courier.bluedart.validation import validate_shipment
from izi.izi.courier.exceptions import (
    CourierAuthError,
    CourierBusinessError,
    CourierOutcomeUnknown,
    CourierTechnicalError,
    CourierValidationError,
)

# .invalid never resolves, so a test that escapes the fake transport cannot reach a real server.
BASE = "https://api.bluedart.invalid/in/transportation"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.testtoken.signature"
PDF_B64 = base64.b64encode(b"%PDF-1.4 test label").decode()
CFG = BlueDartConfig(
    base_url=BASE,
    client_id="CID-TEST",
    client_secret="SECRET-TEST",
    login_id="LOGIN-TEST",
    licence_key="LICKEY-TEST",
    api_type="S",
    area_code="BHO",
    customer_code="123456",
    timeout_seconds=60,
)


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeTransport:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, *, headers, json_body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": json_body})
        if not self.script:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def login_ok(token=TOKEN):
    return HttpResponse(200, json.dumps({"JWTToken": token}))


def awb_ok(awb="80189941381"):
    return HttpResponse(
        200,
        json.dumps({
            "AWBNo": awb,
            "IsError": False,
            "Status": [{"StatusCode": "", "StatusInformation": ""}],
            "AWBPrintContent": PDF_B64,
        }),
    )


def refusal(code, info=""):
    return HttpResponse(400, json.dumps({"IsError": True, "Status": [{"StatusCode": code, "StatusInformation": info}]}))


def make_shipment(**overrides):
    pickup = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
    values = dict(
        reference="IZI20260001",
        product_code="E",
        sub_product_code="P",
        pickup_at=pickup,
        pickup_time="1700",
        weight_kg=1.0,
        pieces=1,
        dimensions_cm=[(14.0, 12.0, 13.0)],
        shipper=Party(
            name="IZI VENTURES PVT LTD",
            address1="PLOT NO 3 MALVIYA NAGAR",
            address2="BHOPAL",
            address3="MADHYA PRADESH",
            pincode="462001",
            mobile="9999999999",
            email="ops@example.com",
        ),
        consignee=Party(
            name="ARUN KUMAR",
            address1="RANGPURI ROOM 10",
            address2="NEW DELHI",
            pincode="110020",
            mobile="8700000000",
            email="buyer@example.com",
        ),
        commodity="DOCUMENTS",
        declared_value=1000.0,
        collectable_amount=0.0,
        special_instruction="TEST",
    )
    values.update(overrides)
    return ShipmentInput(**values)


def make_client(script, clock=None, log=None):
    transport = FakeTransport(script)
    client = BlueDartClient(CFG, transport, clock=clock or FakeClock(), sleep=lambda s: None, log=log)
    return client, transport


def calls_to(transport, path):
    return [c for c in transport.calls if c["url"].endswith(path)]


class GenerateWaybillTests(unittest.TestCase):
    def test_success_returns_awb_and_label(self):
        client, t = make_client([login_ok(), awb_ok("80189941381")])
        result = client.generate_waybill(make_shipment())
        self.assertEqual(result.awb, "80189941381")
        self.assertTrue(result.label_pdf.startswith(b"%PDF"))
        self.assertEqual(t.calls[0]["url"], BASE + TOKEN_PATH)
        self.assertEqual(t.calls[1]["url"], BASE + GENERATE_PATH)
        self.assertEqual(t.calls[1]["headers"]["JWTToken"], TOKEN)

    def test_payload_matches_documented_shape(self):
        client, t = make_client([login_ok(), awb_ok()])
        client.generate_waybill(make_shipment())
        body = t.calls[1]["body"]
        self.assertEqual(set(body), {"Request", "Profile"})
        self.assertEqual(body["Profile"], {"Api_type": "S", "Area": "BHO", "LicenceKey": "LICKEY-TEST", "LoginID": "LOGIN-TEST"})
        req = body["Request"]
        self.assertEqual(set(req), {"Consignee", "Returnadds", "Services", "Shipper"})
        svc = req["Services"]
        self.assertEqual(svc["DeclaredValue"], "1000.00")
        self.assertFalse(svc["PDFOutputNotRequired"])
        self.assertTrue(svc["PickupDate"].startswith("/Date(") and svc["PickupDate"].endswith(")/"))
        self.assertEqual(svc["Dimensions"], [{"Breadth": "12", "Count": "1", "Height": "13", "Length": "14"}])
        self.assertEqual(req["Shipper"]["CustomerCode"], "123456")
        self.assertEqual(req["Shipper"]["OriginArea"], "BHO")

    def test_token_is_reused_until_refresh_window(self):
        client, t = make_client([login_ok(), awb_ok("80189941381"), awb_ok("80189941382")])
        client.generate_waybill(make_shipment(reference="A1"))
        client.generate_waybill(make_shipment(reference="A2"))
        self.assertEqual(len(calls_to(t, TOKEN_PATH)), 1)

    def test_token_refreshed_after_23_hours(self):
        clock = FakeClock()
        client, t = make_client([login_ok(), awb_ok(), login_ok(), awb_ok("80189941383")], clock=clock)
        client.generate_waybill(make_shipment(reference="B1"))
        clock.t += 23 * 3600 + 1
        client.generate_waybill(make_shipment(reference="B2"))
        self.assertEqual(len(calls_to(t, TOKEN_PATH)), 2)

    def test_401_refreshes_token_and_retries_once(self):
        client, t = make_client([login_ok(), HttpResponse(401, '{"Error Message":"Invalid User"}'), login_ok(), awb_ok()])
        result = client.generate_waybill(make_shipment())
        self.assertEqual(result.awb, "80189941381")
        self.assertEqual(len(calls_to(t, TOKEN_PATH)), 2)
        self.assertEqual(len(calls_to(t, GENERATE_PATH)), 2)

    def test_business_refusal_maps_to_message(self):
        client, _ = make_client([login_ok(), refusal("InvalidPinCode")])
        with self.assertRaises(CourierBusinessError) as cm:
            client.generate_waybill(make_shipment())
        self.assertEqual(cm.exception.code, "InvalidPinCode")
        self.assertIn("six digits", cm.exception.message)

    def test_unknown_refusal_uses_blue_dart_text(self):
        client, _ = make_client([login_ok(), refusal("SomethingNew", "Blue Dart says no")])
        with self.assertRaises(CourierBusinessError) as cm:
            client.generate_waybill(make_shipment())
        self.assertIn("Blue Dart says no", cm.exception.message)

    def test_timeout_is_not_retried_and_reports_unknown(self):
        client, t = make_client([login_ok(), TransportTimeout("read timeout")])
        with self.assertRaises(CourierOutcomeUnknown) as cm:
            client.generate_waybill(make_shipment(reference="TIMEOUT1"))
        self.assertEqual(len(calls_to(t, GENERATE_PATH)), 1, "waybill creation must not be retried")
        self.assertIn("TIMEOUT1", cm.exception.message)

    def test_connection_drop_is_not_retried(self):
        client, t = make_client([login_ok(), TransportError("connection reset")])
        with self.assertRaises(CourierOutcomeUnknown):
            client.generate_waybill(make_shipment())
        self.assertEqual(len(calls_to(t, GENERATE_PATH)), 1)

    def test_500_reports_unknown_and_is_not_retried(self):
        client, t = make_client([login_ok(), HttpResponse(500, "Internal Server Error")])
        with self.assertRaises(CourierOutcomeUnknown):
            client.generate_waybill(make_shipment())
        self.assertEqual(len(calls_to(t, GENERATE_PATH)), 1)

    def test_missing_awb_reports_unknown(self):
        client, _ = make_client([login_ok(), HttpResponse(200, json.dumps({"IsError": False, "Status": []}))])
        with self.assertRaises(CourierOutcomeUnknown):
            client.generate_waybill(make_shipment())

    def test_wrapped_success_response_is_read(self):
        """Blue Dart returns the AWB inside GenerateWayBillResult, with the label as a byte array."""
        wrapped = HttpResponse(200, json.dumps({
            "GenerateWayBillResult": {
                "AWBNo": "50942916404",
                "IsError": False,
                "Status": [{"StatusCode": "", "StatusInformation": ""}],
                "AWBPrintContent": list(b"%PDF-1.7 label"),
            }
        }))
        client, _ = make_client([login_ok(), wrapped])
        result = client.generate_waybill(make_shipment(reference="WRAP0001"))
        self.assertEqual(result.awb, "50942916404")
        self.assertEqual(result.label_pdf, b"%PDF-1.7 label")

    def test_wrapped_error_response_gives_the_real_message(self):
        wrapped = HttpResponse(400, json.dumps({
            "status": 400,
            "title": "Bad Request",
            "error-response": [{
                "IsError": True,
                "Status": [{"StatusCode": "NullObject", "StatusInformation": "WayBillGenerationRequest is Null"}],
            }],
        }))
        client, _ = make_client([login_ok(), wrapped])
        with self.assertRaises(CourierBusinessError) as cm:
            client.generate_waybill(make_shipment(reference="WRAP0002"))
        self.assertIn("WayBillGenerationRequest is Null", cm.exception.message)

    def test_invalid_input_never_reaches_the_network(self):
        client, t = make_client([])
        cases = [
            dict(consignee=Party(name="ARUN <KUMAR>", address1="X", pincode="110020", mobile="8700000000")),
            dict(consignee=Party(name="A" * 31, address1="X", pincode="110020", mobile="8700000000")),
            dict(consignee=Party(name="ARUN", address1="X", pincode="11002", mobile="8700000000")),
            dict(consignee=Party(name="ARUN", address1="X", pincode="110020")),
            dict(dimensions_cm=[]),
            dict(pickup_at=datetime(2020, 1, 1)),
            dict(sub_product_code="C", collectable_amount=0.0),
            dict(sub_product_code="C", declared_value=500.0, collectable_amount=600.0),
            dict(sub_product_code="P", collectable_amount=100.0),
            dict(declared_value=0.0),
            dict(reference="AB-12"),
            dict(commodity="BATTERY!"),
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(CourierValidationError):
                    client.generate_waybill(make_shipment(**case))
        self.assertEqual(t.calls, [])

    def test_valid_cod_shipment_passes_validation(self):
        shipment = make_shipment(sub_product_code="C", declared_value=1000.0, collectable_amount=1000.0)
        self.assertEqual(validate_shipment(shipment), [])


class CancelWaybillTests(unittest.TestCase):
    def test_payload_shape(self):
        client, t = make_client([login_ok(), HttpResponse(200, json.dumps({"IsError": False, "Status": [], "AWBNo": "80189941381"}))])
        client.cancel_waybill("80189941381")
        body = calls_to(t, CANCEL_PATH)[0]["body"]
        self.assertEqual(body["Request"], {"AWBNo": "80189941381"})
        self.assertEqual(body["Profile"]["LoginID"], "LOGIN-TEST")

    def test_timeout_is_retried_because_cancel_is_safe_to_repeat(self):
        client, t = make_client([
            login_ok(),
            TransportTimeout("t1"),
            TransportTimeout("t2"),
            HttpResponse(200, json.dumps({"IsError": False, "Status": []})),
        ])
        client.cancel_waybill("80189941381")
        self.assertEqual(len(calls_to(t, CANCEL_PATH)), 3)

    def test_already_in_transit_is_a_business_error(self):
        client, _ = make_client([login_ok(), refusal("CancelFailure")])
        with self.assertRaises(CourierBusinessError) as cm:
            client.cancel_waybill("80189941381")
        self.assertIn("can no longer be cancelled", cm.exception.message)

    def test_bad_awb_format_rejected_locally(self):
        client, t = make_client([])
        with self.assertRaises(CourierValidationError):
            client.cancel_waybill("abc")
        self.assertEqual(t.calls, [])


class AuthTests(unittest.TestCase):
    def test_login_401_is_auth_error(self):
        client, _ = make_client([HttpResponse(401, "")])
        with self.assertRaises(CourierAuthError):
            client.authenticate()

    def test_login_timeout_is_technical(self):
        client, t = make_client([TransportTimeout("t")])
        with self.assertRaises(CourierTechnicalError):
            client.generate_waybill(make_shipment())
        self.assertEqual(len(t.calls), 1)

    def test_unknown_token_shape_fails_clearly_without_leaking_secrets(self):
        client, _ = make_client([HttpResponse(200, json.dumps({"status": "ok", "data": {"session": "x"}}))])
        with self.assertRaises(CourierAuthError) as cm:
            client.authenticate()
        self.assertNotIn("SECRET-TEST", str(cm.exception))
        self.assertIn("format must be confirmed", cm.exception.message)

    def test_find_token_accepts_json_nested_and_plain_text(self):
        self.assertEqual(find_token(json.dumps({"JWTToken": "abc"})), "abc")
        self.assertEqual(find_token(json.dumps({"data": {"token": "nested-token"}})), "nested-token")
        self.assertEqual(find_token("a" * 30), "a" * 30)
        self.assertIsNone(find_token("not a token"))
        self.assertIsNone(find_token(""))


class LoggingTests(unittest.TestCase):

    def test_label_bytes_inside_wrapper_are_not_logged(self):
        entries = []
        wrapped = HttpResponse(200, json.dumps({
            "GenerateWayBillResult": {"AWBNo": "50942916404", "IsError": False, "Status": [], "AWBPrintContent": list(range(200))}
        }))
        client, _ = make_client([login_ok(), wrapped], log=entries.append)
        client.generate_waybill(make_shipment(reference="LOG0001"))
        blob = json.dumps(entries)
        self.assertNotIn("[37, 80", blob)
        self.assertIn("label data omitted", blob)
        
    def test_logs_contain_no_secrets_or_label_bytes(self):
        entries = []
        client, _ = make_client([login_ok(), awb_ok()], log=entries.append)
        client.generate_waybill(make_shipment())
        blob = json.dumps(entries)
        for secret in ("SECRET-TEST", "LICKEY-TEST", "LOGIN-TEST", TOKEN, PDF_B64):
            self.assertNotIn(secret, blob)
        self.assertIn("generate_waybill", blob)
        self.assertIn("Success", blob)

    def test_failing_log_sink_does_not_break_the_call(self):
        def broken(_entry):
            raise RuntimeError("log storage is down")

        client, _ = make_client([login_ok(), awb_ok("80189941381")], log=broken)
        self.assertEqual(client.generate_waybill(make_shipment()).awb, "80189941381")


if __name__ == "__main__":
    unittest.main()