import io
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

import pymupdf

from receipt_hsa import (
    HEADERS,
    extract_amount,
    extract_date,
    extract_page_images,
    flag_duplicates,
    normalize_folder_id,
    parse_receipt_text,
    sync_rows,
)


class ReceiptParsingTests(unittest.TestCase):
    def test_extracts_date_and_total_instead_of_subtotal(self):
        text = """Northside Pharmacy
2026-04-03
Vitamin D 12.50
Subtotal $12.50
Tax $0.00
Grand Total $12.50
"""

        self.assertEqual(extract_date(text), "2026-04-03")
        self.assertEqual(extract_amount(text), (Decimal("12.50"), "USD"))

    def test_extracts_abbreviated_month_date(self):
        self.assertEqual(extract_date("Jan 4, 2026"), "2026-01-04")

    def test_description_does_not_repeat_merchant(self):
        row = parse_receipt_text(
            "Clinic\n01/02/2026\nCopay $20.00\nTotal $20.00",
            "receipt.jpg",
            1,
            "file-1:page:1",
            "",
        )

        self.assertEqual(row[2], "Clinic")
        self.assertEqual(row[3], "Copay")

    def test_unknown_merchant_is_flagged(self):
        row = parse_receipt_text("", "scan.png", 1, "file-2:page:1", "")

        self.assertIn("CHECK PROVIDER", row[10])

    def test_zero_amount_is_flagged(self):
        row = parse_receipt_text(
            "Clinic\n01/02/2026\nCopay $0.00\nTotal $0.00",
            "receipt.jpg",
            1,
            "file-1:page:1",
            "https://drive.google.com/file/d/file-1/view",
        )

        self.assertIn("ZERO AMOUNT", row[10])
        self.assertEqual(row[4], 0.0)

    def test_missing_fields_are_flagged_for_review(self):
        row = parse_receipt_text("", "scan.png", 1, "file-2:page:1", "")

        self.assertIn("CHECK RECEIPT DATE", row[10])
        self.assertIn("CHECK SERVICE DATE", row[10])
        self.assertIn("CHECK AMOUNT", row[10])
        self.assertIn("CHECK DESCRIPTION", row[10])

    def test_duplicate_marks_both_new_rows(self):
        first = ["", "2026-04-03", "Store", "Item", 12.5, "USD", "", "", "a.jpg", 1, "", "a:1", "", ""]
        second = ["", "2026-04-03", "Store", "Item", 12.5, "USD", "", "", "b.jpg", 1, "", "b:1", "", ""]

        updates, rows = flag_duplicates([], [first, second])

        self.assertEqual(updates, {})
        self.assertIn("POTENTIAL DUPLICATE", rows[0][10])
        self.assertIn("POTENTIAL DUPLICATE", rows[1][10])

    def test_duplicate_marks_existing_row(self):
        existing = [
            ["", "2026-04-03", "Store", "Item", 12.5, "USD", "", "", "a.jpg", 1, "", "a:1", "", ""]
        ]
        new = [
            ["", "2026-04-03", "Other Store", "Item", 12.5, "USD", "", "", "b.jpg", 1, "", "b:1", "", ""]
        ]

        updates, rows = flag_duplicates(existing, new)

        self.assertEqual(updates[2], "POTENTIAL DUPLICATE")
        self.assertIn("POTENTIAL DUPLICATE", rows[0][10])

    def test_date_and_amount_must_both_match_for_duplicate(self):
        existing = [
            ["", "2026-04-03", "Store", "Item", 12.5, "USD", "", "", "a.jpg", 1, "", "a:1", "", ""]
        ]
        new = [
            ["", "2026-04-04", "Store", "Item", 12.5, "USD", "", "", "b.jpg", 1, "", "b:1", "", ""]
        ]

        updates, rows = flag_duplicates(existing, new)

        self.assertEqual(updates, {})
        self.assertNotIn("POTENTIAL DUPLICATE", rows[0][10])

    def test_folder_url_normalizes_to_id(self):
        self.assertEqual(
            normalize_folder_id("https://drive.google.com/drive/folders/abc_123"),
            "abc_123",
        )

    def test_each_pdf_page_becomes_a_separate_image(self):
        document = pymupdf.open()
        document.new_page()
        document.new_page()
        pdf_bytes = document.tobytes()
        document.close()

        pages = list(extract_page_images(pdf_bytes, "application/pdf"))

        self.assertEqual(len(pages), 2)

    def test_sheet_sync_flags_existing_duplicate_and_appends_new_row(self):
        existing = [
            "2026-04-03",
            "2026-04-03",
            "Store",
            "Item",
            12.5,
            "USD",
            "",
            "",
            "a.jpg",
            1,
            "",
            "file-a:page:1",
            "OCR text",
            "",
        ]
        new = [
            "2026-04-03",
            "2026-04-03",
            "Another Store",
            "Item",
            12.5,
            "USD",
            "",
            "",
            "b.jpg",
            1,
            "",
            "file-b:page:1",
            "OCR text",
            "",
        ]
        sheets = MagicMock()
        api = sheets.spreadsheets.return_value
        api.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Receipts"}}]
        }
        values_api = api.values.return_value

        def get_values(**kwargs):
            values = [HEADERS] if kwargs["range"] == "'Receipts'!A1:N1" else [existing]
            return MagicMock(execute=MagicMock(return_value={"values": values}))

        values_api.get.side_effect = get_values

        added = sync_rows(sheets, "spreadsheet-id", "Receipts", [new])

        self.assertEqual(added, 1)
        update_body = values_api.batchUpdate.call_args.kwargs["body"]
        self.assertEqual(update_body["data"][0]["range"], "'Receipts'!K2")
        self.assertEqual(
            update_body["data"][0]["values"], [["POTENTIAL DUPLICATE"]]
        )
        append_call = values_api.append.call_args.kwargs
        self.assertEqual(append_call["range"], "'Receipts'!A:N")
        self.assertIn("POTENTIAL DUPLICATE", append_call["body"]["values"][0][10])

    def test_sheet_sync_skips_an_already_imported_page(self):
        existing = [
            "2026-04-03",
            "2026-04-03",
            "Store",
            "Item",
            12.5,
            "USD",
            "",
            "",
            "a.jpg",
            1,
            "",
            "file-a:page:1",
            "OCR text",
            "",
        ]
        sheets = MagicMock()
        api = sheets.spreadsheets.return_value
        api.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Receipts"}}]
        }
        values_api = api.values.return_value

        def get_values(**kwargs):
            values = [HEADERS] if kwargs["range"] == "'Receipts'!A1:N1" else [existing]
            return MagicMock(execute=MagicMock(return_value={"values": values}))

        values_api.get.side_effect = get_values

        added = sync_rows(sheets, "spreadsheet-id", "Receipts", [existing])

        self.assertEqual(added, 0)
        values_api.append.assert_not_called()


if __name__ == "__main__":
    unittest.main()