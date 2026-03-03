# Sofi FinModel Tool

Extract financial statement tables from digital PDF reports into your Excel template — powered by Camelot + LLM row mapping.

## Files

| File | Purpose |
|---|---|
| `app.py` | **Streamlit web app** — full extraction pipeline |
| `sofi_finmodtool.py` | Low-level Camelot helpers & CLI |
| `requirements.txt` | Pinned dependencies |
| `.env.example` | Config template (API key, etc.) |

## Quick Start

```bash
cd sofifinmodeltool

# 1. Install deps
pip install -r requirements.txt
brew install ghostscript   # required for Camelot lattice mode

# 2. Configure
cp .env.example .env       # add your OPENAI_API_KEY

# 3. Run the app
streamlit run app.py
```

## How It Works

1. **Upload** a digital (non-scanned) PDF report + an Excel template with fixed row labels per sheet.
2. **Select** which statements to extract (Balance Sheet, P&L, Cash Flow).
3. **Optionally provide** page numbers — otherwise the app auto-detects pages via keyword scanning.
4. **Camelot** extracts tables (lattice → stream fallback).
5. An **LLM** classifies tables & maps extracted rows to your template's row labels.
6. **Preview & override** any mapping before the final export.
7. **Download** the populated Excel file.

## Camelot Cheat Sheet

| Option | Mode | Purpose |
|---|---|---|
| `process_background=True` | lattice | detect background lines |
| `table_areas=["x1,y1,x2,y2"]` | both | exact table coords (origin = bottom-left) |
| `columns=["72,95,209"]` | stream | column separator x-positions |
| `split_text=True` | stream | un-merge adjacent headers |
| `edge_tol=500` | stream | edge detection tolerance (default 50) |
| `line_scale=40` | lattice | short-line detection (careful >150) |
| `shift_text=['r','b']` | lattice | shift text in spanning cells |
| `copy_text=['v']` | lattice | copy text across spanning cells |

### CLI (standalone)

```bash
python sofi_finmodtool.py report.pdf --pages all --flavor lattice --out tables
```

## Edge Cases Handled

- **No tables found** → clear per-statement warning.
- **LLM returns bad JSON** → automatic retry with stricter prompt, then surfaces error.
- **Sheet name mismatch** → fuzzy matching (thefuzz, threshold 60).
- **Numeric formats** → commas, parentheses-negatives, currency symbols, dash=zero.
- **Scale detection** → lakhs/crores/millions/billions extracted from header; stored as metadata (no auto-conversion).
- **Multi-period columns** → all numeric columns extracted; mapped to template columns left-to-right.

## Limitations

- Only works on **text-based** PDFs (not scanned images — run OCR first).
- Encrypted PDFs: use `password=` param or decrypt externally with `qpdf`.

---

## Lessons

1. After any user correction, append the mistake, root cause, and corrective rule here.
2. Keep rules prescriptive and actionable (one-sentence guardrails).
3. Review at session start for tasks in the same domain.

**Log:**

- 2026-03-02: Plan-first workflow adopted.
- 2026-03-03: Consolidated to minimal file set. Keep count low; organise internally with clear sections.
- 2026-03-03: Built Streamlit app (`app.py`) — full pipeline: Camelot extraction, LLM classification + row mapping, Excel template population.
