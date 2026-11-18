# -*- coding: utf-8 -*-
"""Tests for ERPNext-to-Blue Dart mapping. Run: cd apps/izi && python3 -m unittest izi.izi.courier.bluedart.test_mapping -v"""

import unittest
from datetime import date

from izi.izi.courier.bluedart.mapping import (
    MappingError,
    build_shipment_input,
    clean_email,
    clean_name,
    clean_text,
    digits_only_phone,
    make_reference,
    parcels_to_dimensions,
    split_address,
)

TODAY = date(2026, 11, 18)
PICKUP = date(2026, 11, 19)


def good_args(**overrides):
    args = dict(
        shipment_name="SHIP-00001",
        attempt=1,
        consignee={
            "name": "Arun Kumar",
            "address_lines": ["Rangpuri Room No 10", "New Delhi"],
            "pincode": "110020",
            "mobile": "+91 87006-19964",
            "email": "buyer@example.com",
        },
        shipper={
            "name": "IZI Ventures Pvt Ltd",
            "address_lines": ["Plot No 3 Malviya Nagar", "Bhopal", "Madhya Pradesh"],
            "pincode": "462001",
            "mobile": "9999999999",
            "email": "ops@example.com",
        },
        parcels=[{"count": 1, "length": 14, "width": 12, "height": 13, "weight": 1.0}],
        product_code="E",
        sub_product_code="P",
        pickup_date=PICKUP,
        pickup_time="1700",
        commodity="DRONE PARTS",
        declared_value=1000.0,
        special_instruction="DN-00001",
        today=TODAY,
    )
    args.update(overrides)
    return args


class TextCleaningTests(unittest.TestCase):
    def test_forbidden_characters_are_replaced(self):
        self.assertEqual(clean_text("A<B>  C"), "A B C")

    def test_name_is_truncated_to_30(self):
        self.assertEqual(len(clean_name("N" * 40)), 30)

    def test_phone_normalisation(self):
        self.assertEqual(digits_only_phone("+91 87006-19964"), "8700619964")
        self.assertEqual(digits_only_phone("12345"), "")

    def test_bad_email_is_dropped(self):
        self.assertEqual(clean_email("not-an-email"), "")
        self.assertEqual(clean_email("a@b.co"), "a@b.co")


class AddressTests(unittest.TestCase):
    def test_wraps_into_three_lines_of_30(self):
        lines = split_address(["PLOT NO 3 MALVIYA NAGAR", "BHOPAL", "MADHYA PRADESH"])
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(len(line) <= 30 for line in lines))

    def test_pads_short_addresses_to_three_fields(self):
        self.assertEqual(split_address(["Only one line"])[1:], ["", ""])

    def test_too_long_address_is_rejected(self):
        with self.assertRaises(MappingError):
            split_address(["word " * 60])

    def test_empty_address_is_rejected(self):
        with self.assertRaises(MappingError):
            split_address(["", "  "])


class ParcelTests(unittest.TestCase):
    def test_counts_expand_to_one_dimension_per_piece(self):
        dims, weight, pieces = parcels_to_dimensions([
            {"count": 2, "length": 10, "width": 10, "height": 10, "weight": 0.5},
            {"count": 1, "length": 20, "width": 15, "height": 5, "weight": 1.2},
        ])
        self.assertEqual(len(dims), 3)
        self.assertEqual(pieces, 3)
        self.assertEqual(weight, 2.2)

    def test_missing_dimensions_are_reported(self):
        with self.assertRaises(MappingError) as cm:
            parcels_to_dimensions([{"count": 1, "length": 0, "width": 10, "height": 10, "weight": 1}])
        self.assertIn("length, width and height", " ".join(cm.exception.problems))

    def test_no_parcels_is_reported(self):
        with self.assertRaises(MappingError):
            parcels_to_dimensions([])


class ReferenceTests(unittest.TestCase):
    def test_reference_is_alphanumeric_and_unique_per_attempt(self):
        first = make_reference("SHIP-00001", 1)
        second = make_reference("SHIP-00001", 2)
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[A-Za-z0-9]{1,20}$")
        self.assertTrue(first.endswith("001"))

    def test_reference_never_exceeds_20(self):
        self.assertLessEqual(len(make_reference("X" * 50, 999)), 20)


class BuildTests(unittest.TestCase):
    def test_happy_path_builds_a_valid_shipment(self):
        inp = build_shipment_input(**good_args())
        self.assertEqual(inp.reference, "SHIP00001001")
        self.assertEqual(inp.consignee.mobile, "8700619964")
        self.assertEqual(inp.consignee.pincode, "110020")
        self.assertEqual(inp.pieces, 1)
        self.assertEqual(inp.dimensions_cm, [(14.0, 12.0, 13.0)])
        self.assertEqual(inp.declared_value, 1000.0)
        self.assertEqual(inp.commodity, "DRONE PARTS")

    def test_all_problems_are_reported_together(self):
        args = good_args(
            parcels=[],
            consignee={"name": "X", "address_lines": ["A"], "pincode": "11002", "mobile": "", "email": ""},
        )
        with self.assertRaises(MappingError) as cm:
            build_shipment_input(**args)
        self.assertGreaterEqual(len(cm.exception.problems), 2)

    def test_cod_without_amount_is_rejected(self):
        with self.assertRaises(MappingError):
            build_shipment_input(**good_args(sub_product_code="C", collectable_amount=0))

    def test_long_consignee_name_is_truncated_not_rejected(self):
        args = good_args()
        args["consignee"]["name"] = "Arun " + "Kumar " * 10
        inp = build_shipment_input(**args)
        self.assertLessEqual(len(inp.consignee.name), 30)


if __name__ == "__main__":
    unittest.main()