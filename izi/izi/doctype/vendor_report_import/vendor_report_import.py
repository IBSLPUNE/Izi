# -*- coding: utf-8 -*-
import csv
import json
from collections import Counter

import frappe
import pandas as pd
from frappe import _
from frappe.model.document import Document

from .parsers import (
    AMAZON_CUSTOMER,
    FLIPKART_CUSTOMER,
    ReportFormatError,
    parse_amazon_po,
    parse_flipkart_po_detail,
)

PREVIEW_PO_LIMIT = 300


class VendorReportImport(Document):
    def validate(self):
        if self.import_status in (None, "", "Pending"):
            self.validation_errors = ""
            self.processing_summary = ""

    @frappe.whitelist()
    def process_file(self):
        """Read the attached marketplace file and create draft Sales Orders."""
        self.db_set(
            {
                "import_status": "Processing",
                "processing_summary": "",
                "validation_errors": "",
                "preview_data": "",
            },
            update_modified=False,
        )
        # Commit the "Processing" state first, so a crash mid-run leaves a visible status.
        frappe.db.commit()

        try:
            outcome = self._run_import()
        except Exception as exc:
            frappe.db.rollback()  # drop any partial work from this run
            message = _short(exc)
            self.db_set(
                {"import_status": "Failed", "validation_errors": message},
                update_modified=False,
            )
            frappe.db.commit()  # keep the Failed status, then show the error
            frappe.throw(_("Import failed: {0}").format(message))

        self.db_set(
            {
                "import_status": outcome["status"],
                "processing_summary": outcome["summary"],
                "validation_errors": outcome["errors"],
                "preview_data": outcome["preview"],
            },
            update_modified=False,
        )
        frappe.db.commit()
        frappe.msgprint(outcome["message"], title=_("Import finished"), indicator=outcome["indicator"])

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def _run_import(self):
        if self.document_type != "Purchase Order":
            raise ReportFormatError(
                _("{0} imports are not built yet. Only Purchase Order is supported right now.").format(
                    self.document_type
                )
            )

        if self.marketplace == "Amazon":
            customer = AMAZON_CUSTOMER
            parsed = parse_amazon_po(self._read_table(header=0))
        elif self.marketplace == "Flipkart":
            customer = FLIPKART_CUSTOMER
            parsed = parse_flipkart_po_detail(self._read_table(header=None))
        else:
            raise ReportFormatError(_("Unknown marketplace: {0}").format(self.marketplace))

        if not frappe.db.exists("Customer", customer):
            raise ReportFormatError(
                _("Customer '{0}' does not exist. Create it in ERPNext, then run the import again.").format(
                    customer
                )
            )

        return self._create_sales_orders(parsed, customer, self._default_company())

    def _create_sales_orders(self, parsed, customer, company):
        resolver = _ItemResolver(customer)
        counts = Counter()
        created, errors, preview = [], [], []
        warnings = list(parsed.notes)

        for index, po in enumerate(parsed.plans):
            entry = {
                "po_no": po.po_no,
                "po_date": str(po.po_date),
                "delivery_date": str(po.delivery_date),
                "warnings": po.warnings,
                "status": "",
                "lines": [],
            }
            for note in po.warnings:
                warnings.append(f"PO {po.po_no}: {note}")

            if frappe.db.exists("Sales Order", {"po_no": po.po_no, "docstatus": ["!=", 2]}):
                counts["already_exists"] += 1
                entry["status"] = "Already imported"
                preview.append(entry)
                continue

            open_lines = [ln for ln in po.lines if not ln.skip_reason]
            if not open_lines:
                counts["no_open_qty"] += 1
                reasons = sorted({ln.skip_reason for ln in po.lines})
                entry["status"] = "No open quantity"
                entry["lines"] = [_preview_line(ln, None) for ln in po.lines]
                warnings.append(f"PO {po.po_no}: no open quantity ({'; '.join(reasons)})")
                preview.append(entry)
                continue

            resolved, unmapped = [], []
            for ln in po.lines:
                item_code = None
                if not ln.skip_reason:
                    item_code = resolver.resolve(ln.identifier, ln.alt_identifier)
                    if item_code:
                        resolved.append((ln, item_code))
                    else:
                        unmapped.append(ln)
                entry["lines"].append(_preview_line(ln, item_code))

            if unmapped:
                # Never create a partial Sales Order: a missing line would be a silent shortfall.
                counts["blocked"] += 1
                entry["status"] = "Blocked: unmapped items"
                names = "; ".join(
                    f"{ln.identifier} ({ln.title[:40]})"
                    + (f" - {resolver.problem(ln.identifier)}" if resolver.problem(ln.identifier) else "")
                    for ln in unmapped
                )
                field_name = resolver.field or "the item code field"
                errors.append(
                    f"PO {po.po_no}: not created. Set {field_name} (or Customer Item Codes) on these items first: {names}"
                )
                preview.append(entry)
                continue

            savepoint = f"vri_po_{index}"
            frappe.db.savepoint(savepoint)
            try:
                so = frappe.new_doc("Sales Order")
                so.customer = customer
                so.company = company
                so.po_no = po.po_no
                so.po_date = po.po_date
                so.transaction_date = po.po_date
                so.delivery_date = po.delivery_date
                for ln, item_code in resolved:
                    so.append(
                        "items",
                        {
                            "item_code": item_code,
                            "qty": ln.qty,
                            "rate": ln.rate,
                            "delivery_date": po.delivery_date,
                        },
                    )
                so.insert(ignore_permissions=True)
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                counts["failed"] += 1
                entry["status"] = "Failed to save"
                errors.append(f"PO {po.po_no}: Sales Order could not be saved: {_short(exc)}")
                preview.append(entry)
                continue

            counts["created"] += 1
            created.append(so.name)
            entry["status"] = f"Created {so.name}"
            preview.append(entry)

        summary_lines = [
            f"Marketplace: {self.marketplace}  |  Document: {self.document_type}  |  Customer: {customer}",
            f"Purchase orders in file: {len(parsed.plans)}  (rows read: {parsed.rows_read})",
            f"Draft Sales Orders created: {counts['created']}",
            f"Already imported (skipped): {counts['already_exists']}",
            f"No open quantity (skipped): {counts['no_open_qty']}",
            f"Blocked by unmapped items: {counts['blocked']}",
            f"Failed to save: {counts['failed']}",
        ]
        if created:
            summary_lines.append("Created: " + ", ".join(created))

        status = "Completed" if counts["created"] and not errors else "Validated"
        indicator = "green" if status == "Completed" else "orange"

        error_text = "\n".join(errors)
        if warnings:
            error_text = (error_text + "\n\n" if error_text else "") + "Warnings:\n" + "\n".join(warnings)

        return {
            "status": status,
            "indicator": indicator,
            "summary": "\n".join(summary_lines),
            "errors": error_text,
            "preview": json.dumps(preview[:PREVIEW_PO_LIMIT], indent=1, ensure_ascii=False),
            "message": _("{0} draft Sales Order(s) created. {1} purchase order(s) need attention.").format(
                counts["created"], len(errors)
            ),
        }

    # ------------------------------------------------------------------
    # File and company helpers
    # ------------------------------------------------------------------

    def _read_table(self, header):
        if not self.report_file:
            raise ReportFormatError(_("Please attach a file before processing."))

        file_name = frappe.db.get_value("File", {"file_url": self.report_file}, "name")
        if not file_name:
            raise ReportFormatError(_("The attached file was not found: {0}").format(self.report_file))
        path = frappe.get_doc("File", file_name).get_full_path()

        with open(path, "rb") as fh:
            head = fh.read(8)

        if head.startswith(b"PK\x03\x04"):  # .xlsx
            return pd.read_excel(path, sheet_name=0, header=header, dtype=object, engine="openpyxl")
        if head.startswith(b"\xd0\xcf\x11\xe0"):  # legacy binary .xls
            try:
                return pd.read_excel(path, sheet_name=0, header=header, dtype=object, engine="xlrd")
            except ImportError:
                raise ReportFormatError(
                    _("Reading .xls files needs the xlrd package. Run: ./env/bin/pip install xlrd")
                )
        if head.lstrip().startswith(b"<"):
            raise ReportFormatError(
                _("This file is an HTML table with an .xls name. Open it in Excel and save as .xlsx, or export it as CSV.")
            )

                # Many marketplace ".xls" downloads and real .csv files are CSV text.
        return _read_csv_rows(path, header)

    def _default_company(self):
        company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
            "Global Defaults", "default_company"
        )
        if not company:
            raise ReportFormatError(_("No default Company is set in ERPNext."))
        return company


class _ItemResolver:
    """Finds the Item for a marketplace identifier, using only the Item master.

    Amazon:   Customer Item Codes (ref = ASIN), then Item.custom_asin
    Flipkart: Customer Item Codes (ref = FSN), then Item.custom_flipkart_item_code,
              then Item Barcode (EAN)
    """

    FIELD_BY_CUSTOMER = {
        AMAZON_CUSTOMER: "custom_asin",
        FLIPKART_CUSTOMER: "custom_flipkart_item_code",
    }

    def __init__(self, customer):
        self.customer = customer
        self.field = self.FIELD_BY_CUSTOMER.get(customer)
        self.cache = {}
        self.problems = {}
        self.has_field = bool(self.field) and frappe.db.has_column("Item", self.field)

    def resolve(self, identifier, alt_identifier=""):
        key = (identifier, alt_identifier)
        if key not in self.cache:
            self.cache[key] = self._lookup(identifier, alt_identifier)
        return self.cache[key]

    def problem(self, identifier):
        """Why an identifier could not be resolved (empty if it is simply not in the master)."""
        return self.problems.get(identifier, "")

    def _lookup(self, identifier, alt_identifier):
        if identifier:
            code = frappe.db.get_value(
                "Item Customer Detail",
                {"customer_name": self.customer, "ref_code": identifier},
                "parent",
            )
            if code:
                return code

            if self.has_field:
                matches = frappe.get_all(
                    "Item", filters={self.field: identifier}, pluck="name", limit=2
                )
                if len(matches) > 1:
                    self.problems[identifier] = f"this code is set on more than one Item ({self.field})"
                    return None
                if matches:
                    return matches[0]

        if alt_identifier:
            code = frappe.db.get_value("Item Barcode", {"barcode": alt_identifier}, "parent")
            if code:
                return code
        return None


def _preview_line(ln, item_code):
    return {
        "ref": ln.row_ref,
        "identifier": ln.identifier,
        "title": ln.title[:80],
        "qty": ln.qty,
        "rate": ln.rate,
        "item_code": item_code,
        "skip": ln.skip_reason,
    }


def _short(exc):
    text = str(exc) or exc.__class__.__name__
    return text[:1000]


def _read_csv_rows(path, header):
    """Read CSV text with the csv module, so rows of different widths are allowed.

    header=0 uses the first row as column names. header=None keeps every row.
    """
    rows = None
    for encoding in ("utf-8-sig", "cp1252", "latin1"):
        try:
            with open(path, newline="", encoding=encoding) as fh:
                rows = list(csv.reader(fh))
            break
        except UnicodeDecodeError:
            continue
    if rows is None:
        raise ReportFormatError(_("Could not read the CSV file. Save it as UTF-8 CSV and try again."))

    width = max((len(r) for r in rows), default=0)
    padded = [r + [""] * (width - len(r)) for r in rows]
    if header == 0:
        if not padded:
            return pd.DataFrame()
        return pd.DataFrame(padded[1:], columns=padded[0], dtype=object)
    return pd.DataFrame(padded, dtype=object)