# -*- coding: utf-8 -*-
"""Parsers for marketplace purchase-order exports.

This module has no Frappe imports on purpose: it only turns a DataFrame into
plain Python objects, so it can be unit-tested outside a bench. The DocType
controller (vendor_report_import.py) does the database lookups and creates the
Sales Orders.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

AMAZON_CUSTOMER = "Amazon India"
FLIPKART_CUSTOMER = "Flipkart"

# Date formats differ between columns of the same Amazon export (confirmed on
# PO_amazon.xlsx): Order date and Modified date are month-first, while the
# Window dates are day-first. Every window_start >= order_date only under this
# reading, so the formats are kept per column instead of guessing with dayfirst.
AMAZON_ORDER_DATE_FORMATS = ("%m/%d/%Y",)
AMAZON_WINDOW_DATE_FORMATS = ("%d/%m/%Y",)
AMAZON_MODIFIED_DATE_FORMATS = ("%m/%d/%Y",)
FLIPKART_ORDER_DATE_FORMATS = ("%d-%m-%y", "%d-%m-%Y")  # "01-09-26" = 1 Sep 2026

AMAZON_REQUIRED_COLUMNS = (
    "PO",
    "ASIN",
    "Product title",
    "Previously accepted quantity",
    "Cancelled quantity",
    "Cancelled cost",
    "Order date",
    "Window end",
    "Modified date",
)

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class ReportFormatError(Exception):
    """The uploaded file is not in a format this parser understands."""


@dataclass
class ParsedLine:
    row_ref: str  # where the line came from, for error messages
    identifier: str  # Amazon ASIN or Flipkart FSN
    alt_identifier: str  # Flipkart EAN (barcode); empty for Amazon
    title: str
    qty: float  # open quantity to put on the Sales Order
    rate: float  # unit price
    skip_reason: str = ""  # non-empty means the line is not imported


@dataclass
class ParsedPO:
    po_no: str
    customer: str
    po_date: date
    delivery_date: date
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class ParseResult:
    plans: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # informational, e.g. superseded revisions
    rows_read: int = 0


# ---------------------------------------------------------------------------
# Cell helpers
# ---------------------------------------------------------------------------

def _is_missing(value):
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _clean(value):
    """Cell -> stripped string. Whole-number floats lose the '.0' (Excel IDs)."""
    if _is_missing(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return "" if text.lower() in ("nan", "nat", "none") else text


def _to_float(value):
    """Numbers, including text such as '707708.4 INR' or '₹1,250.00'."""
    if _is_missing(value):
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _NUMBER_RE.search(_clean(value))
    return float(match.group(0).replace(",", "")) if match else 0.0


def _parse_date(value, formats, label):
    """Strict date parsing. Raises instead of guessing when a value is unreadable."""
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel serial date number
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = _clean(value)
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text[:10])
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ReportFormatError(f"Could not read {label} '{text}'.")


# ---------------------------------------------------------------------------
# Amazon
# ---------------------------------------------------------------------------

def parse_amazon_po(df):
    """Parse an Amazon vendor PO / PO-modification export (ag-grid xlsx or csv).

    Quantity rule: each line's open quantity is
        previously accepted - cancelled
    so a line cancelled in full produces no Sales Order quantity. Lines that
    were never accepted are skipped too. The unit price is the cancelled cost
    divided by the cancelled quantity (Amazon reports the cost as a total).
    """
    if df is None:
        raise ReportFormatError("The uploaded file could not be read.")

    df = df.copy()
    df.columns = [_clean(c) for c in df.columns]
    missing = [c for c in AMAZON_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ReportFormatError(
            "This does not look like an Amazon PO export. Missing columns: "
            + ", ".join(missing)
            + "."
        )
    if df.empty:
        raise ReportFormatError("The uploaded Amazon PO file has no rows.")

    rows_read = len(df)
    work = df.copy()
    work["_row"] = [f"row {i + 2}" for i in range(len(work))]  # header is Excel row 1
    work["_po"] = work["PO"].map(_clean)
    work["_asin"] = work["ASIN"].map(_clean)
    work = work[(work["_po"] != "") & (work["_asin"] != "")].copy()
    work["_modified"] = work["Modified date"].map(
        lambda v: _parse_date(v, AMAZON_MODIFIED_DATE_FORMATS, "Modified date")
    )

    # The same PO can be exported more than once as it is revised (PO 3TV9TKFR
    # appears with two modified dates). Keep only the newest revision of each
    # PO/ASIN pair, otherwise quantities are counted twice.
    work = work.sort_values("_modified", ascending=False, na_position="last", kind="stable")
    superseded = work.duplicated(["_po", "_asin"], keep="first")
    notes = [
        f"{r['_row']}: PO {r['_po']} / {r['_asin']} is an older revision "
        f"(modified {r['Modified date']}); ignored."
        for _, r in work[superseded].iterrows()
    ]
    work = work[~superseded].sort_index()

    plans = []
    for po_no, group in work.groupby("_po", sort=False):
        warnings = []
        po_date = _parse_date(group.iloc[0]["Order date"], AMAZON_ORDER_DATE_FORMATS, "Order date")
        if po_date is None:
            raise ReportFormatError(f"PO {po_no} has no Order date.")

        windows = [
            w
            for w in (
                _parse_date(v, AMAZON_WINDOW_DATE_FORMATS, "Window end") for v in group["Window end"]
            )
            if w
        ]
        delivery = max(windows) if windows else po_date
        if len(set(windows)) > 1:
            warnings.append("lines have different window end dates; the latest one is used")
        if delivery < po_date:
            warnings.append("window end is before the order date; delivery set to the order date")
            delivery = po_date

        lines = []
        for _, row in group.iterrows():
            accepted = _to_float(row["Previously accepted quantity"])
            cancelled = _to_float(row["Cancelled quantity"])
            cost = _to_float(row["Cancelled cost"])
            open_qty = max(accepted - cancelled, 0.0)
            if open_qty > 0:
                reason = ""
            elif accepted <= 0:
                reason = "never accepted by Amazon"
            else:
                reason = "fully cancelled"
            rate = round(cost / cancelled, 2) if cancelled > 0 and cost > 0 else 0.0
            lines.append(
                ParsedLine(
                    row_ref=row["_row"],
                    identifier=row["_asin"],
                    alt_identifier="",
                    title=_clean(row["Product title"]),
                    qty=open_qty,
                    rate=rate,
                    skip_reason=reason,
                )
            )

        if any(not ln.skip_reason and ln.rate == 0 for ln in lines):
            warnings.append("some open lines have no unit cost")

        plans.append(
            ParsedPO(
                po_no=po_no,
                customer=AMAZON_CUSTOMER,
                po_date=po_date,
                delivery_date=delivery,
                lines=lines,
                warnings=warnings,
            )
        )

    return ParseResult(plans=plans, notes=notes, rows_read=rows_read)


# ---------------------------------------------------------------------------
# Flipkart
# ---------------------------------------------------------------------------

def _label_value(rows, label):
    """Value in the cell right of a label cell, e.g. 'ORDER DATE' -> '01-09-26'."""
    wanted = label.lower()
    for row in rows:
        for i, cell in enumerate(row[:-1]):
            if cell.lower() == wanted and row[i + 1]:
                return row[i + 1]
    return ""


def _find_label_rows(rows, label):
    wanted = label.lower()
    return [i for i, row in enumerate(rows) if any(c.lower() == wanted for c in row)]


def parse_flipkart_po_detail(raw):
    """Parse Flipkart PO detail exports (one PO per file, or several POs stacked in one CSV).

    Each block starts at a 'PO#' label. Pass the sheet read with header=None.
    The Flipkart Summary List (one row per PO, no line items) is rejected with a clear message.
    """
    rows = [[_clean(v) for v in r] for r in raw.itertuples(index=False, name=None)]
    if not rows:
        raise ReportFormatError("The uploaded file is empty.")

    if any(c.lower() == "purchase order id" for c in rows[0]):
        raise ReportFormatError(
            "This is the Flipkart Summary List (one row per PO). It has no line items, so it cannot "
            "create Sales Orders. Download the PO detail for each PO instead. Its file name starts "
            "with purchase_order_, for example purchase_order_FBSWN08483828.xls."
        )

    starts = _find_label_rows(rows, "PO#")
    if starts:
        blocks = [rows[s : (starts[k + 1] if k + 1 < len(starts) else len(rows))] for k, s in enumerate(starts)]
    else:
        blocks = [rows]

    plans, notes, seen = [], [], set()
    for block in blocks:
        plan = _parse_flipkart_block(block)
        if plan.po_no in seen:
            notes.append(f"PO {plan.po_no} appears more than once in this file; only the first copy is used.")
            continue
        seen.add(plan.po_no)
        plans.append(plan)

    return ParseResult(plans=plans, notes=notes, rows_read=sum(len(p.lines) for p in plans))


def _parse_flipkart_block(rows):
    po_no = _label_value(rows, "PO#")
    if not po_no:
        for row in rows:
            for cell in row:
                match = re.search(r"PURCHASE ORDER\s*#\s*([A-Z0-9]+)", cell, re.IGNORECASE)
                if match:
                    po_no = match.group(1)
                    break
            if po_no:
                break
    if not po_no:
        raise ReportFormatError("Could not find the PO number (PO#) in this Flipkart file.")

    order_text = _label_value(rows, "ORDER DATE")
    po_date = _parse_date(order_text, FLIPKART_ORDER_DATE_FORMATS, "ORDER DATE") if order_text else None
    if po_date is None:
        raise ReportFormatError(f"PO {po_no} has no ORDER DATE.")

    header_idx = None
    for i, row in enumerate(rows):
        lowered = [c.lower() for c in row]
        if "fsn/isbn13" in lowered and "quantity" in lowered:
            header_idx = i
            break
    if header_idx is None:
        raise ReportFormatError(
            f"Could not find the item table for PO {po_no} (no 'FSN/ISBN13' and 'Quantity' columns)."
        )

    headers = [c.lower() for c in rows[header_idx]]

    def col(name):
        return headers.index(name) if name in headers else None

    c_fsn, c_qty, c_ean = col("fsn/isbn13"), col("quantity"), col("ean")
    c_title, c_taxable, c_required = col("title"), col("taxable value"), col("required by date")

    def cell(row, idx):
        return row[idx] if idx is not None and idx < len(row) else ""

    lines, delivery_dates, warnings = [], [], []
    total_qty_in_file = None

    for row in rows[header_idx + 1 :]:
        if not any(row):
            continue
        if any(c.lower().startswith("total quantity") for c in row):
            idx = next(i for i, c in enumerate(row) if c.lower().startswith("total quantity"))
            total_qty_in_file = next((_to_float(c) for c in row[idx + 1 :] if re.search(r"\d", c)), None)
            break
        if not re.fullmatch(r"\d+", row[0] or ""):
            continue  # not an item line

        qty = _to_float(cell(row, c_qty))
        taxable = _to_float(cell(row, c_taxable))
        required = None
        if cell(row, c_required):
            try:
                required = _parse_date(cell(row, c_required), (), "Required by Date")
            except ReportFormatError:
                warnings.append(f"line {row[0]}: unreadable Required by Date; PO delivery date used instead")
        if required:
            delivery_dates.append(required)

        # Taxable value is ex-GST, which is what a Sales Order rate should be.
        # Supplier Price in this export already includes 18% IGST.
        rate = round(taxable / qty, 2) if qty > 0 and taxable > 0 else 0.0
        reason = "" if qty > 0 else "quantity 0 on the PO"
        if qty > 0 and rate == 0:
            warnings.append(f"line {row[0]} ({cell(row, c_fsn)}) has no taxable value")

        lines.append(
            ParsedLine(
                row_ref=f"line {row[0]}",
                identifier=cell(row, c_fsn),
                alt_identifier=cell(row, c_ean),
                title=cell(row, c_title),
                qty=qty,
                rate=rate,
                skip_reason=reason,
            )
        )

    if not lines:
        raise ReportFormatError(f"No item lines were found under PO {po_no}.")

    open_total = sum(ln.qty for ln in lines)
    if total_qty_in_file is not None and abs(open_total - total_qty_in_file) > 0.001:
        warnings.append(
            f"line quantities add up to {open_total:g} but the file's total is {total_qty_in_file:g}"
        )

    delivery = max(delivery_dates) if delivery_dates else po_date
    if delivery < po_date:
        warnings.append("required-by date is before the order date; delivery set to the order date")
        delivery = po_date

    return ParsedPO(
        po_no=po_no,
        customer=FLIPKART_CUSTOMER,
        po_date=po_date,
        delivery_date=delivery,
        lines=lines,
        warnings=warnings,
    )