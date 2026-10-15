import io
import unittest
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import base64
from unittest.mock import MagicMock, patch

import pymupdf
from googleapiclient.errors import HttpError

from receipt_hsa import (
    HEADERS,
    LEGACY_HEADERS,
    authenticate,
    build_document_page_link,
    build_ollama_payload,
    extract_amount,
    extract_date,
    extract_page_images,
    flag_duplicates,
    normalize_folder_id,
    ollama_api_base,
    parse_receipt_text,
    parse_ollama_fields,
    sync_rows,
    ensure_worksheet,
    validate_drive_folder,
)


class ReceiptParsingTests(unittest.TestCase):
    def test_revoked_oauth_token_falls_back_to_browser_authorization(self):
        from google.auth.exceptions import RefreshError

        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "token.json"
            expired_credentials = MagicMock(
                valid=False,
                expired=True,
                refresh_token="refresh-token",
            )
            expired_credentials.refresh.side_effect = RefreshError("invalid_grant")
            new_credentials = MagicMock(
                valid=True,
                to_json=MagicMock(return_value='{"valid": true}'),
            )
            flow = MagicMock()
            flow.run_local_server.return_value = new_credentials

            with (
                patch(
                    "google.oauth2.credentials.Credentials.from_authorized_user_file",
                    return_value=expired_credentials,
                ),
                patch(
                    "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                    return_value=flow,
                ),
            ):
                credentials = authenticate(Path(temp_dir) / "credentials.json", token_path)

            self.assertIs(credentials, new_credentials)
            flow.run_local_server.assert_called_once_with(port=0)
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"valid": true}')

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

    def test_ocr_text_is_retained_in_its_sheet_column(self):
        row = parse_receipt_text(
            "Clinic\nTotal $20.00",
            "receipt.jpg",
            1,
            "file-1:page:1",
            "https://drive.google.com/file/d/file-1/view",
        )

        self.assertEqual(row[12], "Clinic\nTotal $20.00")
        self.assertEqual(row[13], "https://drive.google.com/file/d/file-1/view")

    def test_model_fields_improve_description_and_provider_but_preserve_amount(self):
        fields = {
            "service_date": "2026-04-02",
            "receipt_date": "2026-04-03",
            "provider": "Northside Pharmacy",
            "description": "Prescription medication",
        }
        row = parse_receipt_text(
            "NORTHSIDE PHARMACY\n2026-04-03\nRx 12.50\nTotal $12.50",
            "receipt.pdf",
            1,
            "file-id:page:1",
            "https://drive.google.com/file/d/file-id/view",
            llm_fields=fields,
        )

        self.assertEqual(row[0], "2026-04-02")
        self.assertEqual(row[2], "Northside Pharmacy")
        self.assertEqual(row[3], "Prescription medication")
        self.assertEqual(row[4], 12.5)

    def test_ollama_response_fields_are_normalized(self):
        fields = parse_ollama_fields(
            '{"service_date":"Apr 2, 2026","receipt_date":"2026-04-03",'
            '"provider":" Clinic ","description":" Office visit "}'
        )

        self.assertEqual(fields["service_date"], "2026-04-02")
        self.assertEqual(fields["receipt_date"], "2026-04-03")
        self.assertEqual(fields["provider"], "Clinic")
        self.assertEqual(fields["description"], "Office visit")

    def test_ollama_host_accepts_standard_windows_host_value(self):
        self.assertEqual(ollama_api_base("127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertEqual(
            ollama_api_base("http://127.0.0.1:11434/"),
            "http://127.0.0.1:11434",
        )

    def test_ollama_payload_includes_page_image_and_json_schema(self):
        payload = build_ollama_payload(b"page-image", "OCR aid", "qwen3-vl:4b")

        self.assertEqual(payload["model"], "qwen3-vl:4b")
        self.assertEqual(base64.b64decode(payload["messages"][0]["images"][0]), b"page-image")
        self.assertEqual(payload["format"]["type"], "object")
        self.assertIn("OCR aid", payload["messages"][0]["content"])

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

    def test_pdf_page_link_targets_the_receipt_page(self):
        self.assertEqual(
            build_document_page_link(
                "https://drive.google.com/file/d/file-id/view?usp=drivesdk",
                3,
                "application/pdf",
            ),
            "https://drive.google.com/file/d/file-id/view?usp=drivesdk#page=3",
        )

    def test_image_page_link_keeps_the_source_url(self):
        self.assertEqual(
            build_document_page_link(
                "https://drive.google.com/file/d/image-id/view#old-fragment",
                1,
                "image/jpeg",
            ),
            "https://drive.google.com/file/d/image-id/view",
        )

    def test_each_pdf_page_becomes_a_separate_image(self):
        document = pymupdf.open()
        document.new_page()
        document.new_page()
        pdf_bytes = document.tobytes()
        document.close()

        pages = list(extract_page_images(pdf_bytes, "application/pdf"))

        self.assertEqual(len(pages), 2)

    def test_inaccessible_drive_folder_reports_account_access(self):
        drive = MagicMock()
        drive.files.return_value.get.return_value.execute.side_effect = HttpError(
            SimpleNamespace(status=404, reason="Not Found"),
            b"File not found",
        )

        with self.assertRaisesRegex(ValueError, "not accessible to the Google account"):
            validate_drive_folder(drive, "folder-id")

    def test_drive_folder_validation_rejects_non_folder_ids(self):
        drive = MagicMock()
        drive.files.return_value.get.return_value.execute.return_value = {
            "id": "file-id",
            "mimeType": "application/pdf",
        }

        with self.assertRaisesRegex(ValueError, "not a folder"):
            validate_drive_folder(drive, "file-id")

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
            "https://drive.google.com/file/d/file-a/view",
            "https://drive.google.com/file/d/file-a/view#page=1",
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
            "https://drive.google.com/file/d/file-b/view",
            "https://drive.google.com/file/d/file-b/view#page=1",
        ]
        sheets = MagicMock()
        api = sheets.spreadsheets.return_value
        api.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Receipts", "sheetId": 42}}]
        }
        values_api = api.values.return_value

        def get_values(**kwargs):
            values = [HEADERS] if kwargs["range"] == "'Receipts'!A1:O1" else [existing]
            return MagicMock(execute=MagicMock(return_value={"values": values}))

        values_api.get.side_effect = get_values

        added = sync_rows(sheets, "spreadsheet-id", "Receipts", [new])

        self.assertEqual(added, (1, 0))
        update_body = values_api.batchUpdate.call_args.kwargs["body"]
        self.assertEqual(update_body["data"][0]["range"], "'Receipts'!K2")
        self.assertEqual(
            update_body["data"][0]["values"], [["POTENTIAL DUPLICATE"]]
        )
        append_call = values_api.append.call_args.kwargs
        self.assertEqual(append_call["range"], "'Receipts'!A:O")
        self.assertIn("POTENTIAL DUPLICATE", append_call["body"]["values"][0][10])
        formatting = api.batchUpdate.call_args.kwargs["body"]["requests"]
        self.assertEqual(
            formatting[0]["updateDimensionProperties"]["properties"]["pixelSize"],
            21,
        )
        self.assertEqual(
            formatting[1]["repeatCell"]["cell"]["userEnteredFormat"]["wrapStrategy"],
            "CLIP",
        )

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
            "https://drive.google.com/file/d/file-a/view",
            "https://drive.google.com/file/d/file-a/view#page=1",
        ]
        sheets = MagicMock()
        api = sheets.spreadsheets.return_value
        api.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Receipts", "sheetId": 42}}]
        }
        values_api = api.values.return_value

        def get_values(**kwargs):
            values = [HEADERS] if kwargs["range"] == "'Receipts'!A1:O1" else [existing]
            return MagicMock(execute=MagicMock(return_value={"values": values}))

        values_api.get.side_effect = get_values

        added = sync_rows(sheets, "spreadsheet-id", "Receipts", [existing])

        self.assertEqual(added, (0, 0))
        values_api.append.assert_not_called()

    def test_sheet_sync_backfills_page_link_without_appending_duplicate(self):
        existing_without_page_link = [
            "2026-04-03",
            "2026-04-03",
            "Store",
            "Item",
            12.5,
            "USD",
            "",
            "",
            "a.pdf",
            2,
            "",
            "file-a:page:2",
            "OCR text",
            "https://drive.google.com/file/d/file-a/view",
        ]
        new = existing_without_page_link + [
            "https://drive.google.com/file/d/file-a/view#page=2"
        ]
        sheets = MagicMock()
        api = sheets.spreadsheets.return_value
        api.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Receipts", "sheetId": 42}}]
        }
        values_api = api.values.return_value

        def get_values(**kwargs):
            values = (
                [HEADERS]
                if kwargs["range"] == "'Receipts'!A1:O1"
                else [existing_without_page_link]
            )
            return MagicMock(execute=MagicMock(return_value={"values": values}))

        values_api.get.side_effect = get_values

        added = sync_rows(sheets, "spreadsheet-id", "Receipts", [new])

        self.assertEqual(added, (0, 0))
        values_api.append.assert_not_called()
        update_body = values_api.batchUpdate.call_args.kwargs["body"]
        self.assertEqual(update_body["data"][0]["range"], "'Receipts'!O2")
        self.assertEqual(
            update_body["data"][0]["values"],
            [["https://drive.google.com/file/d/file-a/view#page=2"]],
        )

    def test_refresh_updates_extracted_fields_not_amount_or_manual_fields(self):
        existing = [
            "",
            "2026-04-03",
            "Old pharmacy guess",
            "Old item guess",
            12.5,
            "USD",
            "Patient-entered",
            "Category-entered",
            "a.pdf",
            1,
            "CHECK SERVICE DATE",
            "file-a:page:1",
            "OCR text",
            "https://drive.google.com/file/d/a/view",
            "https://drive.google.com/file/d/a/view#page=1",
        ]
        new = existing.copy()
        new[0] = "2026-04-02"
        new[2] = "Northside Pharmacy"
        new[3] = "Prescription medication"
        sheets = MagicMock()
        api = sheets.spreadsheets.return_value
        api.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Receipts", "sheetId": 42}}]
        }
        values_api = api.values.return_value

        def get_values(**kwargs):
            values = [HEADERS] if kwargs["range"] == "'Receipts'!A1:O1" else [existing]
            return MagicMock(execute=MagicMock(return_value={"values": values}))

        values_api.get.side_effect = get_values

        result = sync_rows(
            sheets, "spreadsheet-id", "Receipts", [new], refresh_existing=True
        )

        self.assertEqual(result, (0, 1))
        update_data = values_api.batchUpdate.call_args.kwargs["body"]["data"]
        updates = {item["range"]: item["values"][0][0] for item in update_data}
        self.assertEqual(updates["'Receipts'!A2"], "2026-04-02")
        self.assertEqual(updates["'Receipts'!C2"], "Northside Pharmacy")
        self.assertEqual(updates["'Receipts'!D2"], "Prescription medication")
        self.assertNotIn("'Receipts'!E2", updates)
        self.assertNotIn("'Receipts'!G2", updates)
        self.assertNotIn("'Receipts'!H2", updates)

    def test_existing_sheet_gets_new_document_page_link_header(self):
        sheets = MagicMock()
        sheets.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Receipts", "sheetId": 42}}]
        }
        values_api = sheets.spreadsheets.return_value.values.return_value
        values_api.get.side_effect = [
            MagicMock(execute=MagicMock(return_value={"values": [LEGACY_HEADERS]})),
            MagicMock(execute=MagicMock(return_value={"values": [HEADERS]})),
        ]

        sheet_id = ensure_worksheet(sheets, "spreadsheet-id", "Receipts")

        self.assertEqual(sheet_id, 42)
        values_api.update.assert_called_once_with(
            spreadsheetId="spreadsheet-id",
            range="'Receipts'!O1",
            valueInputOption="RAW",
            body={"values": [["Document Page Link"]]},
        )


if __name__ == "__main__":
    unittest.main()