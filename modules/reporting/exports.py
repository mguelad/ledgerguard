"""PII-free reporting with inert spreadsheet cells and escaped document text."""

import csv
import html
import io
from functools import lru_cache
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from packages.reconciliation_core.money import Money

PLAYBOOKS = {
    "PI-001": (
        "Stripe payment succeeded; Woo order remains unpaid",
        "Check the exact payment and Woo transaction IDs. Inspect gateway and Action Scheduler failures. Search for a second order before correcting fulfilment in the source system.",
    ),
    "PI-002": (
        "Stripe payment has no confirmed Woo order",
        "Search Woo admin for the PaymentIntent and Charge IDs. Confirm store attribution, archive/trash filters and gateway logs. Do not create an order or refund until duplicate fulfilment has been ruled out.",
    ),
    "PI-003": (
        "Woo order is paid without a succeeded Stripe attempt",
        "Check all attempts and manual-capture state in Stripe. Confirm that another gateway or an offline payment was not used. Review fulfilment before taking action in Woo.",
    ),
    "PI-004": (
        "Multiple successful payments match one order",
        "Verify each distinct PaymentIntent. Charges belonging to the same PaymentIntent are one attempt. Check authorised split payments and prior refunds before deciding on remediation.",
    ),
    "PI-005": (
        "Payment amount or currency differs from the order",
        "Compare the original transaction currency, captured amount and Woo total. Review partial captures, currency conversion extensions and manually edited totals.",
    ),
    "PI-006": (
        "Stripe refund is missing from Woo",
        "Confirm that the Stripe refund succeeded. Inspect Woo refund records and job failures. Avoid issuing a second refund while synchronising the records.",
    ),
    "PI-007": (
        "Woo refund has no succeeded Stripe refund",
        "Woo can show a manual refund without money movement. Review refund IDs, status and the operator history in each source before issuing any new refund.",
    ),
    "PI-008": (
        "Refund totals or currencies disagree",
        "Compare all succeeded Stripe refunds with Woo refund records. Exclude pending and failed Stripe refunds. Verify partial refunds individually.",
    ),
    "PI-009": (
        "Connector coverage is stale or incomplete",
        "Check connector authorization, plugin job failures, clock drift and scan progress. Restore contiguous coverage before interpreting payment mismatches.",
    ),
    "PI-010": (
        "Exact identifiers conflict or cannot be verified",
        "Check the linked Stripe account and live/test mode. Compare the Woo transaction, PaymentIntent, Charge and Session IDs. Correct the explicit link; do not guess another account.",
    ),
}


def safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # Leading whitespace can bypass formula detection in spreadsheet applications.
    if text.lstrip().startswith(("=", "+", "-", "@")) or any(c in text[:1] for c in "\t\r\n"):
        return "'" + text
    return text


def csv_report(rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    columns = [
        "finding_key",
        "rule",
        "severity",
        "state",
        "store",
        "mode",
        "amount_minor",
        "currency",
        "exponent",
        "confidence",
        "first_seen",
        "last_seen",
        "identities",
        "next_step",
    ]
    writer.writerow(columns)
    for row in rows:
        writer.writerow([safe_cell(row.get(k, "")) for k in columns])
    return stream.getvalue().encode("utf-8-sig")


def display_fields(row: dict[str, Any]) -> dict[str, str]:
    risk = (
        "—"
        if row.get("amount_minor") is None
        else f"{row['currency']} {Money(row['amount_minor'], row['currency'], row['exponent']).decimal()}"
    )
    return {
        "State": str(row.get("state", "")).title(),
        "Severity": str(row.get("severity", "")).title(),
        "Store": str(row.get("store", "")),
        "Mode": str(row.get("mode", "")).title(),
        "Amount at risk": risk,
        "Source identities": str(row.get("identities", "")),
        "Match confidence": str(row.get("confidence", "")) + " / 100",
        "First observed (UTC)": str(row.get("first_seen", "")),
        "Last observed (UTC)": str(row.get("last_seen", "")),
    }


def html_report(rows: list[dict[str, Any]], title: str = "Payment integrity report") -> bytes:
    sections = []
    for row in rows:
        rule = row["rule"]
        label, step = PLAYBOOKS[rule]
        details = "".join(
            f"<dt>{html.escape(str(k))}</dt><dd>{html.escape(str(v))}</dd>" for k, v in display_fields(row).items()
        )
        sections.append(
            f"<section><h2>{rule}: {html.escape(label)}</h2><dl>{details}</dl><h3>Diagnostic steps</h3><p>{html.escape(step)}</p></section>"
        )
    content = "".join(sections) or "<p>No eligible mismatch was found in the supplied coverage window.</p>"
    return f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>{html.escape(title)}</title><style>body{{font:16px/1.5 system-ui;max-width:900px;margin:40px auto;padding:0 24px;color:#142431}}section{{border-top:1px solid #bbc6cc;padding:20px 0;break-inside:avoid}}dt{{font-weight:600}}dd{{margin:0 0 8px;overflow-wrap:anywhere}}h1{{font-size:32px}}@media print{{body{{margin:0}}}}</style><body><h1>{html.escape(title)}</h1><p>Read-only reconciliation. Financial actions require verification in the source systems.</p>{content}</body></html>'.encode()


@lru_cache(maxsize=1)
def register_report_fonts() -> None:
    fonts = Path(__file__).with_name("fonts")
    pdfmetrics.registerFont(TTFont("LedgerGuard", str(fonts / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("LedgerGuard-Bold", str(fonts / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily(
        "LedgerGuard",
        normal="LedgerGuard",
        bold="LedgerGuard-Bold",
        italic="LedgerGuard",
        boldItalic="LedgerGuard-Bold",
    )


def pdf_report(rows: list[dict[str, Any]], title: str = "Payment integrity report") -> bytes:
    register_report_fonts()
    stream = io.BytesIO()
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        if hasattr(style, "fontName"):
            style.fontName = "LedgerGuard-Bold" if "Bold" in style.fontName else "LedgerGuard"
    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        leftMargin=42,
        rightMargin=42,
        topMargin=40,
        bottomMargin=40,
        title=title,
        author="LedgerGuard",
    )
    story = [
        Paragraph(html.escape(title), styles["Title"]),
        Spacer(1, 14),
        Paragraph(
            "Read-only reconciliation. Verify source evidence before taking financial action.", styles["BodyText"]
        ),
        Spacer(1, 20),
    ]
    for row in rows:
        label, step = PLAYBOOKS[row["rule"]]
        story.extend([Paragraph(html.escape(row["rule"] + " | " + label), styles["Heading2"])])
        data = []
        for key, value in display_fields(row).items():
            data.append(
                [
                    Paragraph(key, styles["BodyText"]),
                    Paragraph(html.escape(value), styles["BodyText"]),
                ]
            )
        table = Table(data, colWidths=[110, 400 - 12], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f4")),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.extend([table, Spacer(1, 8), Paragraph(html.escape(step), styles["BodyText"]), Spacer(1, 16)])
    if not rows:
        story.append(Paragraph("No eligible mismatch was found in the supplied coverage window.", styles["BodyText"]))

    def footer(canvas: Any, document: Any) -> None:
        canvas.setFont("LedgerGuard", 9)
        canvas.setFillColor(colors.HexColor("#5c6d77"))
        canvas.drawString(42, 22, "LedgerGuard | Payment integrity")
        canvas.drawRightString(A4[0] - 42, 22, str(document.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
