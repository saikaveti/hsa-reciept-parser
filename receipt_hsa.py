from __future__ import annotations

import argparse
import io
import os
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


HEADERS = [
    "Service Date",
    "Receipt Date",
    "Provider",
    "Description",
    "Amount",
    "Currency",
    "Patient",
    "HSA Category",
    "Source File",
    "Page",
    "Flags",
    "Source Key",
    "OCR Text",
    "Source URL",
]
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SUPPORTED_IMAGE_TYPES = {
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}
DATE_PATTERNS = (
    re.compile(r"\b(20\d{2}|19\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})\b"),
    re.compile(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})\b",
        re.IGNORECASE,
    ),
)
AMOUNT_PATTERN = re.compile(
    r"(?<![\d.])(?P<currency>USD|CAD|AUD|EUR|GBP|\$|€|£)?\s*"
    r"(?P<amount>-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})(?!\d)",
    re.IGNORECASE,
)
TOTAL_EXCLUSIONS = re.compile(r"\b(subtotal|tax|savings|discount|change|tip)\b", re.I)
TOTAL_LABELS = (
    (re.compile(r"\b(grand\s+total|total\s+due|balance\s+due|amount\s+due)\b", re.I), 7),
    (re.compile(r"\b(total\s+paid|transaction\s+total)\b", re.I), 6),
    (re.compile(r"\btotal\b", re.I), 5),
    (re.compile(r"\b(amount\s+paid|charged|payment)\b", re.I), 3),
)


def normalize_folder_id(value: str) -> str:
    match = re.search(r"(?:folders/|[?&]id=)([A-Za-z0-9_-]+)", value)
    folder_id = match.group(1) if match else value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
        raise ValueError("Provide a Google Drive folder ID or folder URL.")
    return folder_id


def extract_date(text: str) -> str:
    for line in text.splitlines():
        for pattern_index, pattern in enumerate(DATE_PATTERNS):
            match = pattern.search(line)
            if not match:
                continue
            try:
                if pattern_index == 0:
                    year, month, day = (int(part) for part in match.groups())
                elif pattern_index == 1:
                    month, day, year = (int(part) for part in match.groups())
                    if year < 100:
                        year += 2000 if year <= 68 else 1900
                else:
                    from datetime import datetime

                    month = datetime.strptime(match.group(1)[:3], "%b").month
                    day = int(match.group(2))
                    year = int(match.group(3))
                from datetime import date

                return date(year, month, day).isoformat()
            except ValueError:
                continue
    return ""


def extract_service_date(text: str) -> str:
    lines = text.splitlines()
    for line_index, line in enumerate(lines):
        if not re.search(r"\b(date\s+of\s+service|service\s+date|date\s+of\s+visit)\b", line, re.I):
            continue
        date_value = extract_date(line)
        if date_value:
            return date_value
        if line_index + 1 < len(lines):
            date_value = extract_date(lines[line_index + 1])
            if date_value:
                return date_value
    return ""


def amount_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "")).quantize(
            Decimal("0.01")
        )
    except (InvalidOperation, ValueError):
        return None


def extract_amount(text: str) -> tuple[Decimal | None, str]:
    candidates: list[tuple[int, int, int, Decimal, str]] = []
    currencies = {"$": "USD", "€": "EUR", "£": "GBP"}
    for line_index, line in enumerate(text.splitlines()):
        matches = list(AMOUNT_PATTERN.finditer(line))
        if not matches:
            continue
        score = 0
        if not TOTAL_EXCLUSIONS.search(line):
            for label, label_score in TOTAL_LABELS:
                if label.search(line):
                    score = label_score
                    break
        match = matches[-1]
        try:
            amount = Decimal(match.group("amount").replace(",", "")).quantize(
                Decimal("0.01")
            )
        except InvalidOperation:
            continue
        currency_token = (match.group("currency") or "").upper()
        currency = currencies.get(currency_token, currency_token or "USD")
        candidates.append((score, line_index, match.start(), amount, currency))
    if not candidates:
        return None, "USD"
    best = max(candidates, key=lambda candidate: (candidate[0], candidate[1], candidate[2]))
    return best[3], best[4]


def extract_merchant(text: str) -> str:
    for line in text.splitlines()[:8]:
        candidate = re.sub(r"\s+", " ", line).strip(" \t|:-")
        lowered = candidate.casefold()
        if not candidate or len(candidate) < 2:
            continue
        if any(
            marker in lowered
            for marker in ("receipt", "invoice", "www.", "http", "thank you", "tel:")
        ):
            continue
        if re.search(r"\b\d{3}[-.) ]?\d{3}[-. ]?\d{4}\b", candidate):
            continue
        if AMOUNT_PATTERN.search(candidate) or extract_date(candidate):
            continue
        return candidate[:120]
    return ""


def extract_description(text: str, merchant: str) -> str:
    descriptions: list[str] = []
    for line in text.splitlines():
        if TOTAL_EXCLUSIONS.search(line) or any(label.search(line) for label, _ in TOTAL_LABELS):
            continue
        candidate = AMOUNT_PATTERN.sub("", line)
        candidate = re.sub(r"[|:*\-]+", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if len(candidate) < 3 or extract_date(candidate):
            continue
        if merchant and candidate.casefold() == merchant.casefold():
            continue
        if re.search(r"\b(visa|mastercard|amex|debit|credit|cash|change|subtotal)\b", candidate, re.I):
            continue
        if re.search(r"\b\d{3}[-.) ]?\d{3}[-. ]?\d{4}\b", candidate):
            continue
        descriptions.append(candidate)
        if len(descriptions) == 3:
            break
    return "; ".join(descriptions)[:240]


def add_flag(flags: str, flag: str) -> str:
    current = [part for part in flags.split("; ") if part]
    if flag not in current:
        current.append(flag)
    return "; ".join(current)


def parse_receipt_text(
    text: str,
    filename: str,
    page_number: int,
    source_key: str,
    source_url: str,
) -> list[Any]:
    service_date = extract_service_date(text)
    receipt_date = extract_date(text)
    merchant = extract_merchant(text)
    description = extract_description(text, merchant)
    amount, currency = extract_amount(text)
    flags = ""
    if not service_date:
        flags = add_flag(flags, "CHECK SERVICE DATE")
    if not receipt_date:
        flags = add_flag(flags, "CHECK RECEIPT DATE")
    if not merchant:
        flags = add_flag(flags, "CHECK PROVIDER")
    if not description:
        flags = add_flag(flags, "CHECK DESCRIPTION")
    if amount is None:
        flags = add_flag(flags, "CHECK AMOUNT")
    elif amount == Decimal("0.00"):
        flags = add_flag(flags, "ZERO AMOUNT")
    flags = add_flag(flags, "ADD PATIENT")
    flags = add_flag(flags, "ADD HSA CATEGORY")
    return [
        service_date,
        receipt_date,
        merchant,
        description,
        float(amount) if amount is not None else "",
        currency,
        "",
        "",
        filename,
        page_number,
        flags,
        source_key,
        text.strip(),
        source_url,
    ]


def receipt_identity(row: list[Any]) -> tuple[str, Decimal] | None:
    if len(row) < 5 or not row[1]:
        return None
    amount = amount_decimal(row[4])
    if amount is None:
        return None
    return str(row[1]).strip(), amount


def flag_duplicates(
    existing_rows: list[list[Any]], new_rows: list[list[Any]]
) -> tuple[dict[int, str], list[list[Any]]]:
    existing_by_identity: dict[tuple[str, Decimal], list[tuple[int, str]]] = defaultdict(list)
    for row_number, row in enumerate(existing_rows, start=2):
        identity = receipt_identity(row)
        if identity:
            flags = str(row[10]) if len(row) > 10 else ""
            existing_by_identity[identity].append((row_number, flags))

    prepared_rows = [row.copy() for row in new_rows]
    new_by_identity: dict[tuple[str, Decimal], list[int]] = defaultdict(list)
    existing_flag_updates: dict[int, str] = {}
    for row_index, row in enumerate(prepared_rows):
        identity = receipt_identity(row)
        if not identity:
            continue
        existing_matches = existing_by_identity.get(identity, [])
        new_matches = new_by_identity.get(identity, [])
        if existing_matches or new_matches:
            row[10] = add_flag(str(row[10]), "POTENTIAL DUPLICATE")
            for prior_index in new_matches:
                prepared_rows[prior_index][10] = add_flag(
                    str(prepared_rows[prior_index][10]), "POTENTIAL DUPLICATE"
                )
            for row_number, old_flags in existing_matches:
                existing_flag_updates[row_number] = add_flag(
                    old_flags, "POTENTIAL DUPLICATE"
                )
        new_by_identity[identity].append(row_index)
    return existing_flag_updates, prepared_rows


def extract_page_images(data: bytes, mime_type: str) -> Iterable[bytes]:
    if mime_type == "application/pdf":
        import pymupdf

        document = pymupdf.open(stream=data, filetype="pdf")
        try:
            for page in document:
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False)
                yield pixmap.tobytes("png")
        finally:
            document.close()
        return

    from PIL import Image, ImageOps, ImageSequence

    with Image.open(io.BytesIO(data)) as image:
        for frame in ImageSequence.Iterator(image):
            normalized = ImageOps.exif_transpose(frame).convert("RGB")
            output = io.BytesIO()
            normalized.save(output, format="PNG")
            yield output.getvalue()


def ocr_image(data: bytes, language: str) -> str:
    import pytesseract
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return pytesseract.image_to_string(image, lang=language)


def authenticate(credentials_path: Path, token_path: Path) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            credentials = flow.run_local_server(port=0)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def build_services(credentials: Any) -> tuple[Any, Any]:
    from googleapiclient.discovery import build

    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    return drive, sheets


def list_drive_files(drive: Any, root_folder_id: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    pending = [root_folder_id]
    visited_folders: set[str] = set()
    while pending:
        folder_id = pending.pop()
        if folder_id in visited_folders:
            continue
        visited_folders.add(folder_id)
        page_token = None
        while True:
            response = (
                drive.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken,files(id,name,mimeType,webViewLink)",
                    pageSize=1000,
                    pageToken=page_token,
                )
                .execute()
            )
            for file_info in response.get("files", []):
                if file_info["mimeType"] == FOLDER_MIME_TYPE:
                    pending.append(file_info["id"])
                elif file_info["mimeType"] == "application/pdf" or file_info["mimeType"] in SUPPORTED_IMAGE_TYPES:
                    files.append(file_info)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return files


def download_drive_file(drive: Any, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    output = io.BytesIO()
    request = drive.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(output, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return output.getvalue()


def quoted_sheet_range(worksheet: str, cells: str) -> str:
    return f"'{worksheet.replace(chr(39), chr(39) * 2)}'!{cells}"


def ensure_worksheet(sheets: Any, spreadsheet_id: str, worksheet: str) -> None:
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    titles = [sheet["properties"]["title"] for sheet in spreadsheet.get("sheets", [])]
    if worksheet not in titles:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": worksheet}}}]},
        ).execute()
    header_range = quoted_sheet_range(worksheet, "A1:N1")
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=header_range)
        .execute()
    )
    current_headers = result.get("values", [[]])[0]
    if not current_headers:
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=header_range,
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
    elif current_headers != HEADERS:
        raise ValueError(
            f"Worksheet {worksheet!r} has different headers; use an empty worksheet or rename it."
        )


def sync_rows(sheets: Any, spreadsheet_id: str, worksheet: str, new_rows: list[list[Any]]) -> int:
    ensure_worksheet(sheets, spreadsheet_id, worksheet)
    data_range = quoted_sheet_range(worksheet, "A2:N")
    existing_rows = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=data_range,
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
        .get("values", [])
    )
    existing_source_keys = {
        str(row[11]) for row in existing_rows if len(row) > 11 and row[11]
    }
    unique_new_rows = [
        row for row in new_rows if str(row[11]) not in existing_source_keys
    ]
    flag_updates, prepared_rows = flag_duplicates(existing_rows, unique_new_rows)
    if flag_updates:
        updates = [
            {
                "range": quoted_sheet_range(worksheet, f"K{row_number}"),
                "values": [[flags]],
            }
            for row_number, flags in flag_updates.items()
        ]
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()
    if prepared_rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=quoted_sheet_range(worksheet, "A:N"),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": prepared_rows},
        ).execute()
    return len(prepared_rows)


def process_folder(
    drive: Any,
    root_folder_id: str,
    language: str,
) -> list[list[Any]]:
    records: list[list[Any]] = []
    for file_info in list_drive_files(drive, root_folder_id):
        file_id = file_info["id"]
        name = file_info["name"]
        print(f"Reading {name}...", file=sys.stderr)
        try:
            file_data = download_drive_file(drive, file_id)
            for page_number, page_image in enumerate(
                extract_page_images(file_data, file_info["mimeType"]), start=1
            ):
                try:
                    text = ocr_image(page_image, language)
                    record = parse_receipt_text(
                        text,
                        name,
                        page_number,
                        f"{file_id}:page:{page_number}",
                        file_info.get("webViewLink", ""),
                    )
                except Exception as error:
                    record = parse_receipt_text(
                        "",
                        name,
                        page_number,
                        f"{file_id}:page:{page_number}",
                        file_info.get("webViewLink", ""),
                    )
                    record[10] = add_flag(record[10], f"OCR ERROR: {error}")
                records.append(record)
        except Exception as error:
            record = parse_receipt_text(
                "",
                name,
                1,
                f"{file_id}:page:1",
                file_info.get("webViewLink", ""),
            )
            record[10] = add_flag(record[10], f"FILE ERROR: {error}")
            records.append(record)
    return records


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(
        description="OCR receipt images in Google Drive and append review rows to Google Sheets."
    )
    parser.add_argument(
        "--folder",
        default=os.getenv("GOOGLE_DRIVE_FOLDER_ID"),
        help="Drive folder ID or URL (or GOOGLE_DRIVE_FOLDER_ID).",
    )
    parser.add_argument(
        "--sheet",
        default=os.getenv("GOOGLE_SHEETS_ID"),
        help="Google spreadsheet ID or URL (or GOOGLE_SHEETS_ID).",
    )
    parser.add_argument("--worksheet", default="Receipts", help="Worksheet tab name.")
    parser.add_argument("--credentials", default="credentials.json", help="OAuth client JSON file.")
    parser.add_argument("--token", default="token.json", help="Local OAuth token cache file.")
    parser.add_argument("--language", default="eng", help="Tesseract language code.")
    parser.add_argument("--tesseract-cmd", help="Optional path to the Tesseract executable.")
    args = parser.parse_args()
    if not args.folder or not args.sheet:
        parser.error("--folder and --sheet are required (or set their environment variables).")
    try:
        folder_id = normalize_folder_id(args.folder)
        sheet_match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", args.sheet)
        spreadsheet_id = sheet_match.group(1) if sheet_match else args.sheet.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
            raise ValueError("Provide a Google Sheets spreadsheet ID or URL.")
        if args.tesseract_cmd:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = args.tesseract_cmd
        credentials = authenticate(Path(args.credentials), Path(args.token))
        drive, sheets = build_services(credentials)
        records = process_folder(drive, folder_id, args.language)
        added = sync_rows(sheets, spreadsheet_id, args.worksheet, records)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Done. Added {added} receipt page(s); existing source pages were skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())