# HSA Receipt Parser

A local Python CLI that reads receipt images and PDFs from a Google Drive folder, runs OCR on each page, and appends review rows to a Google Sheet. It recurses into subfolders. Each PDF page and each frame of a multi-frame image is treated as a separate receipt.

The program is an organizer, not an HSA eligibility decision-maker. OCR can be wrong, and each HSA administrator has its own claim requirements. Review the extracted values and confirm eligibility before submitting a claim.

## What it records

The `Receipts` worksheet contains service date, receipt date, provider, description, amount, currency, patient, HSA category, source file, page, review flags, a source key, OCR text, and a link to the Drive file. OCR text is retained but clipped in the cell, and used rows are kept at a fixed height. Service date, patient, and HSA category are not guessed when they cannot be reliably determined; the sheet flags them for review or leaves the entry blank.

Rows are flagged when a date, provider, description, or amount needs review. A zero amount gets `ZERO AMOUNT`. Rows with the same receipt date and amount get `POTENTIAL DUPLICATE`; both the new and existing rows are flagged. Each Drive file page has a stable source key, so rerunning the tool skips pages already in the sheet.

Supported inputs are PDF and common image formats (BMP, GIF, JPEG, PNG, TIFF, and WebP). OCR is performed locally with Tesseract; images are sent to Google Drive/Sheets only as part of the configured Google API operations. The OCR text is saved in the sheet to make corrections auditable.

## Setup

1. Install Python 3.10 or newer and [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) for Windows. Note the Tesseract executable path if it is not on `PATH`.
2. In Google Cloud Console, create a project and enable the Google Drive API and Google Sheets API.
3. Configure the OAuth consent screen for your account, then create an OAuth client ID of type **Desktop app**. Download its JSON file. By default, save it in this directory as `credentials.json`; alternatively, set `GOOGLE_OAUTH_CREDENTIALS_FILE` in `.env` to its local path. The credentials and generated `token.json` are ignored by Git.
4. Install the Python dependencies:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

5. Copy the Drive folder ID (or URL) and spreadsheet ID (or URL). The Google account used in the browser sign-in must have access to both.

## Run

On first run, the program opens a browser window for Google OAuth consent. The token is cached locally in `token.json`.

```powershell
\.venv\Scripts\python.exe receipt_hsa.py `
  --folder "https://drive.google.com/drive/folders/FOLDER_ID" `
  --sheet "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit"
```

The worksheet tab defaults to `Receipts` and is created with the expected headers if it does not exist. If it already exists with different headers, the program stops rather than overwriting it. Tesseract uses English by default; specify `--language` for another installed Tesseract language or set `TESSERACT_CMD` in `.env` (or pass `--tesseract-cmd`) if Tesseract is not on `PATH`.

The Drive folder must be accessible to the Google account used during OAuth. If Drive reports that the folder cannot be found, verify the folder ID and share it with that account; the program stops before writing to Sheets.

Folder and spreadsheet IDs can also be supplied through `GOOGLE_DRIVE_FOLDER_ID` and `GOOGLE_SHEETS_ID` environment variables.
For local setup, copy `.env.example` to `.env` and set those values there; `.env` is ignored by Git.

## Test

```powershell
\.venv\Scripts\python.exe -m unittest discover -s tests -v
```