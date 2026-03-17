# FinStatement Parser

A single-page Streamlit app that extracts financial tables from PDF files with Camelot, maps extracted data into a user-uploaded template CSV using an LLM, and exports a formatted single-sheet Excel workbook.

## Architecture
- Single-file app logic in `app.py`
- Streamlit UI for upload, extraction, mapping, and download
- Camelot for PDF table extraction
- Provider-selectable LLM mapping (OpenAI, Anthropic, Google Gemini)
- openpyxl-based Excel formatting and export

## Setup
1. Create and activate a virtual environment:
   - `python -m venv venv`
   - `source venv/bin/activate`
2. Install system dependencies:
   - macOS: `brew install ghostscript tcl-tk`
   - Ubuntu/Debian: `sudo apt-get install ghostscript python3-tk`
3. Install Python dependencies:
   - `pip install -r requirements.txt`
4. Run the app:
   - `streamlit run app.py`

## Usage
1. Configure provider, API key, and model in the sidebar.
2. Upload a PDF and template CSV.
3. Enter page numbers (example: `1,3,5-7`).
4. Extract tables and review accuracy.
5. Map extracted data to template using the LLM.
6. Download the generated Excel workbook.

## Troubleshooting
- Ghostscript missing: install OS-level Ghostscript binary.
- Empty extraction: verify page range and try `stream` flavor.
- Poor extraction quality: tune `line_scale`, `edge_tol`, and `row_tol`.
- Non-JSON LLM output: retry mapping; inspect raw response in debug expander.
- Scanned image PDF: Camelot requires text-based PDFs; OCR first.

## Notes
- API keys are entered in-session and are not persisted.
- All application logic is intentionally kept in `app.py`.
