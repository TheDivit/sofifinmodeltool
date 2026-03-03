#!/usr/bin/env python3
"""
app.py — Streamlit web app for extracting financial statement tables from
digital PDF annual/quarterly reports into a user-provided Excel template.

Internal sections:
  1. Config & constants
  2. Numeric parsing utilities
  3. PDF text scanning (pdfplumber — keyword page detection)
  4. Table extraction (camelot — lattice→stream fallback)
  5. LLM helpers (classification & row mapping via OpenAI-compatible API)
  6. Excel writing (openpyxl — populate template)
  7. Streamlit UI

Run:
  streamlit run app.py
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import datetime
from copy import copy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Load .env from this dir *and* parent dir (workspace root) so API keys are available.
load_dotenv()                                   # sofifinmodeltool/.env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # agents/.env

# ---------------------------------------------------------------------------
# Ghostscript fix for macOS + Homebrew
# ctypes.util.find_library("gs") fails on macOS ARM because Homebrew installs
# libgs.dylib under /opt/homebrew/lib which isn't in the default dylib search
# path.  We patch camelot's detection so it finds the library correctly.
# ---------------------------------------------------------------------------
def _patch_ghostscript_detection() -> None:
    """Ensure camelot can find Homebrew-installed Ghostscript on macOS."""
    import sys
    if sys.platform != "darwin":
        return
    from ctypes.util import find_library
    if find_library("gs") is not None:
        return  # already findable — nothing to do

    # Add Homebrew lib path to DYLD_FALLBACK_LIBRARY_PATH so both
    # find_library and the ghostscript ctypes wrapper can locate libgs.
    homebrew_lib = "/opt/homebrew/lib"
    gs_lib = os.path.join(homebrew_lib, "libgs.dylib")
    if os.path.exists(gs_lib):
        existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
        if homebrew_lib not in existing:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = (
                f"{homebrew_lib}:{existing}" if existing else homebrew_lib
            )
        # Also monkey-patch camelot's detection to return True directly
        try:
            import camelot.backends.ghostscript_backend as _gsb
            _gsb.installed_posix = lambda: True
        except ImportError:
            pass

_patch_ghostscript_detection()

# ---------------------------------------------------------------------------
# 1. Config & constants
# ---------------------------------------------------------------------------

STATEMENT_TYPES: List[str] = [
    "Balance Sheet",
    "P&L / Income Statement",
    "Cash Flow Statement",
]

# Keywords used to detect which pages contain which statement (case-insensitive).
STATEMENT_KEYWORDS: Dict[str, List[str]] = {
    "Balance Sheet": [
        "balance sheet",
        "statement of financial position",
        "assets and liabilities",
    ],
    "P&L / Income Statement": [
        "profit and loss",
        "income statement",
        "statement of operations",
        "statement of comprehensive income",
        "statement of profit",
        "revenue from operations",
    ],
    "Cash Flow Statement": [
        "cash flow",
        "statement of cash flows",
        "cash and cash equivalents",
    ],
}

# Fuzzy-match threshold (0–100) for mapping Excel sheet names → statement types.
FUZZY_THRESHOLD = 60


# ---------------------------------------------------------------------------
# 2. Numeric parsing utilities
# ---------------------------------------------------------------------------

def parse_numeric(raw: str) -> Optional[float]:
    """Convert a financial-report cell value to a Python float.

    Handles:
      - commas: 1,234 → 1234
      - parentheses for negatives: (1,234) → -1234
      - dash/hyphen meaning zero: '-' → 0
      - whitespace / currency symbols stripped
    Returns None if not parseable (i.e. it's a label, not a number).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("", "-", "—", "–", "nil", "Nil", "NIL"):
        return 0.0
    # Detect negative-in-parens: (1,234.56)
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    # Strip currency/whitespace
    s = re.sub(r"[₹$€£¥,\s]", "", s)
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return None


def detect_numeric_columns(df: pd.DataFrame) -> List[int]:
    """Return column indices that contain at least 30 % numeric cells."""
    num_cols = []
    for col_idx in range(df.shape[1]):
        col = df.iloc[:, col_idx]
        parsed = col.apply(lambda x: parse_numeric(str(x)) is not None)
        if parsed.mean() >= 0.30:
            num_cols.append(col_idx)
    return num_cols


def detect_scale_from_header(df: pd.DataFrame) -> str:
    """Heuristic: look for '₹ in crores', 'in millions', etc. in first 3 rows."""
    header_text = " ".join(str(v) for v in df.iloc[:3].values.flatten())
    header_lower = header_text.lower()
    for kw in ("crore", "crores"):
        if kw in header_lower:
            return "Crores"
    for kw in ("lakh", "lakhs"):
        if kw in header_lower:
            return "Lakhs"
    for kw in ("million", "millions"):
        if kw in header_lower:
            return "Millions"
    for kw in ("billion", "billions"):
        if kw in header_lower:
            return "Billions"
    for kw in ("thousand", "thousands"):
        if kw in header_lower:
            return "Thousands"
    return "Unknown"


# ---------------------------------------------------------------------------
# 3. PDF text scanning — locate pages per statement type
# ---------------------------------------------------------------------------

def find_pages_for_statement(
    pdf_path: str, statement_type: str
) -> List[int]:
    """Scan every page's text layer for keywords; return 1-indexed page numbers."""
    import pdfplumber

    keywords = STATEMENT_KEYWORDS.get(statement_type, [])
    if not keywords:
        return []

    matched: List[int] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").lower()
            if any(kw in text for kw in keywords):
                matched.append(i)
    return matched


# ---------------------------------------------------------------------------
# 4. Table extraction (camelot)
# ---------------------------------------------------------------------------

def extract_tables_from_page(
    pdf_path: str,
    page: int,
    flavor: str = "lattice",
) -> List[pd.DataFrame]:
    """Extract tables from a single page.  Falls back lattice → stream.

    Lattice mode uses the default ghostscript backend (must be installed:
    `brew install ghostscript`).
    """
    import camelot

    tables = camelot.read_pdf(pdf_path, pages=str(page), flavor=flavor)
    if len(tables) == 0 and flavor == "lattice":
        tables = camelot.read_pdf(pdf_path, pages=str(page), flavor="stream")
    return [t.df for t in tables]


def pick_best_table(dfs: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """From candidate tables on a page, pick the one most likely to be the
    financial statement: most rows with ≥ 2 numeric columns.  If tables share
    column structure they may be continuation fragments → concatenate."""
    if not dfs:
        return None
    if len(dfs) == 1:
        return dfs[0]

    scored: List[Tuple[int, int, pd.DataFrame]] = []
    for df in dfs:
        n_num = len(detect_numeric_columns(df))
        scored.append((len(df), n_num, df))

    # Check if multiple tables share column count → concatenate
    col_counts = [df.shape[1] for df in dfs]
    if len(set(col_counts)) == 1:
        merged = pd.concat(dfs, ignore_index=True)
        return merged

    # Otherwise pick table with most rows that has ≥ 2 numeric columns
    candidates = [(rows, ncols, df) for rows, ncols, df in scored if ncols >= 2]
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][2]

    # Fallback: biggest table
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][2]


# ---------------------------------------------------------------------------
# 5. LLM helpers
# ---------------------------------------------------------------------------

def _llm_call(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    base_url: str | None = None,
) -> str:
    """Low-level chat completion wrapper (OpenAI-compatible)."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url or None)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        max_tokens=2048,
    )
    return resp.choices[0].message.content.strip()


def classify_table_as_statement(
    table_md: str,
    statement_type: str,
    api_key: str,
    model: str,
    base_url: str | None = None,
) -> bool:
    """Ask the LLM: does this table represent *statement_type*?  Returns bool."""
    prompt = (
        f"You are a financial document analyst.\n"
        f"Below is a table extracted from a PDF financial report (in Markdown).\n\n"
        f"{table_md}\n\n"
        f'Does this table represent a "{statement_type}"?\n'
        f"Reply ONLY with YES or NO on the first line, then a one-sentence explanation."
    )
    answer = _llm_call(
        [{"role": "user", "content": prompt}],
        api_key=api_key,
        model=model,
        base_url=base_url,
    )
    return answer.upper().startswith("YES")


def map_rows_with_llm(
    table_md: str,
    template_labels: List[str],
    api_key: str,
    model: str,
    base_url: str | None = None,
    retry: bool = True,
) -> Dict[str, Optional[str]]:
    """Ask the LLM to map each template row label to the best matching
    extracted row label.  Returns {template_label: matched_label_or_None}.
    """
    labels_block = "\n".join(f"- {lbl}" for lbl in template_labels)
    prompt = (
        "You are a financial data extraction assistant.\n\n"
        "## Extracted table (Markdown)\n\n"
        f"{table_md}\n\n"
        "## Template row labels\n\n"
        f"{labels_block}\n\n"
        "Map each template row label to the best matching row label in the "
        "extracted table. If no reasonable match exists, map it to null.\n\n"
        "Return ONLY a valid JSON object (no markdown fences) with this schema:\n"
        '{ "template_row_label": "matched_extracted_row_label_or_null" }\n'
    )
    raw = _llm_call(
        [{"role": "user", "content": prompt}],
        api_key=api_key,
        model=model,
        base_url=base_url,
    )
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        mapping: Dict[str, Optional[str]] = json.loads(raw)
        return mapping
    except json.JSONDecodeError:
        if retry:
            # Retry with stricter prompt
            strict = (
                "Your previous response was not valid JSON.  "
                "Reply with ONLY a raw JSON object, no explanation, no fences.\n\n"
                + prompt
            )
            raw2 = _llm_call(
                [{"role": "user", "content": strict}],
                api_key=api_key,
                model=model,
                base_url=base_url,
            )
            raw2 = re.sub(r"^```(?:json)?\s*", "", raw2)
            raw2 = re.sub(r"\s*```$", "", raw2)
            return json.loads(raw2)
        raise


# ---------------------------------------------------------------------------
# 6. Excel writing (openpyxl)
# ---------------------------------------------------------------------------

def fuzzy_match_sheet(
    sheet_names: List[str], statement_type: str
) -> Optional[str]:
    """Return the best-matching sheet name or None."""
    from thefuzz import fuzz

    best_name, best_score = None, 0
    for name in sheet_names:
        score = fuzz.token_sort_ratio(name.lower(), statement_type.lower())
        if score > best_score:
            best_score = score
            best_name = name
    if best_score >= FUZZY_THRESHOLD:
        return best_name
    return None


def write_to_template(
    template_bytes: bytes,
    results: Dict[str, Dict],
    pdf_filename: str,
) -> bytes:
    """Populate the Excel template with mapped data and return bytes.

    *results* structure per statement:
      {
        "statement_type": str,
        "page": int,
        "mapping": {template_label: matched_label},
        "values": {matched_label: [val1, val2, ...]},   # one per period column
        "scale": str,
      }
    """
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(template_bytes))
    sheet_names = wb.sheetnames

    for stmt_key, data in results.items():
        stmt_type = data["statement_type"]
        matched_sheet = fuzzy_match_sheet(sheet_names, stmt_type)
        if matched_sheet is None:
            continue

        ws = wb[matched_sheet]
        mapping: Dict[str, Optional[str]] = data.get("mapping", {})
        values: Dict[str, List[Optional[float]]] = data.get("values", {})
        scale = data.get("scale", "Unknown")
        page = data.get("page", "?")

        # Walk column A to find template labels, then fill values rightward.
        for row_idx in range(1, ws.max_row + 1):
            cell_val = ws.cell(row=row_idx, column=1).value
            if cell_val is None:
                continue
            label = str(cell_val).strip()
            matched_label = mapping.get(label)
            if matched_label is None:
                continue
            nums = values.get(matched_label, [])
            for col_offset, num in enumerate(nums):
                # Write starting from column B (2)
                ws.cell(row=row_idx, column=2 + col_offset, value=num)

        # Add metadata note in a cell below the data
        meta_row = ws.max_row + 2
        ws.cell(row=meta_row, column=1, value="— Source metadata —")
        ws.cell(row=meta_row + 1, column=1, value=f"PDF: {pdf_filename}")
        ws.cell(row=meta_row + 2, column=1, value=f"Page: {page}")
        ws.cell(
            row=meta_row + 3,
            column=1,
            value=f"Extracted: {datetime.datetime.now().isoformat(timespec='seconds')}",
        )
        ws.cell(row=meta_row + 4, column=1, value=f"Scale: {scale}")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 7. Streamlit UI
# ---------------------------------------------------------------------------

def _df_to_markdown(df: pd.DataFrame, max_rows: int = 60) -> str:
    """Convert a DataFrame to a compact Markdown table for LLM context."""
    return df.head(max_rows).to_markdown(index=False)


def _build_values_dict(
    df: pd.DataFrame,
    mapping: Dict[str, Optional[str]],
) -> Dict[str, List[Optional[float]]]:
    """For each *matched* extracted row label, pull numeric values from all
    numeric columns in the DataFrame.  Returns {label: [v1, v2, …]}."""
    num_cols = detect_numeric_columns(df)
    if not num_cols:
        # Fallback: treat all columns except the first as numeric
        num_cols = list(range(1, df.shape[1]))

    # Build a lookup: row_label → row_index (use column 0 as label column)
    label_col = 0
    label_to_row: Dict[str, int] = {}
    for idx in range(df.shape[0]):
        lbl = str(df.iloc[idx, label_col]).strip()
        if lbl and lbl not in label_to_row:
            label_to_row[lbl] = idx

    values: Dict[str, List[Optional[float]]] = {}
    for tpl_label, ext_label in mapping.items():
        if ext_label is None:
            continue
        row_idx = label_to_row.get(ext_label)
        if row_idx is None:
            # Try fuzzy substring match
            for k, v in label_to_row.items():
                if ext_label.lower() in k.lower() or k.lower() in ext_label.lower():
                    row_idx = v
                    break
        if row_idx is None:
            values[ext_label] = []
            continue
        row_vals: List[Optional[float]] = []
        for ci in num_cols:
            row_vals.append(parse_numeric(str(df.iloc[row_idx, ci])))
        values[ext_label] = row_vals
    return values


def main() -> None:
    st.set_page_config(page_title="SoFi FinModel Tool", layout="wide")
    st.title("📊 SoFi FinModel Tool")
    st.caption(
        "Extract financial statement tables from digital PDFs into your Excel template."
    )

    # ---- Sidebar: settings ------------------------------------------------
    with st.sidebar:
        st.header("Settings")
        api_key = st.text_input(
            "OpenAI API Key",
            type="password",
            value=os.getenv("OPENAI_API_KEY", ""),
            help="Also reads from OPENAI_API_KEY env var / .env",
        )
        base_url = st.text_input(
            "API Base URL (optional)",
            value=os.getenv("OPENAI_BASE_URL", ""),
            help="Leave blank for default OpenAI endpoint",
        )
        model = st.selectbox(
            "Model",
            ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
            index=0,
        )
        flavor_override = st.selectbox(
            "Camelot flavor",
            ["auto (lattice→stream)", "lattice", "stream"],
            index=0,
        )
        camelot_flavor = (
            "lattice"
            if flavor_override.startswith("auto")
            else flavor_override
        )

    # ---- Main area --------------------------------------------------------
    col_pdf, col_xl = st.columns(2)
    with col_pdf:
        pdf_file = st.file_uploader("Upload PDF report", type=["pdf"])
    with col_xl:
        xl_file = st.file_uploader("Upload Excel template", type=["xlsx"])

    selected_stmts = st.multiselect(
        "Statements to extract",
        STATEMENT_TYPES,
        default=STATEMENT_TYPES[:1],
    )

    # Page hints — one number input per selected statement
    page_hints: Dict[str, Optional[int]] = {}
    if selected_stmts:
        st.markdown("**Page hints** *(optional — leave 0 for auto-detection)*")
        hint_cols = st.columns(len(selected_stmts))
        for i, stmt in enumerate(selected_stmts):
            with hint_cols[i]:
                val = st.number_input(
                    stmt, min_value=0, value=0, step=1, key=f"page_{stmt}"
                )
                page_hints[stmt] = val if val > 0 else None

    extract_btn = st.button("🚀 Extract", type="primary", disabled=not (pdf_file and xl_file and api_key))

    if not extract_btn:
        st.info("Upload both files, enter your API key, then click **Extract**.")
        return

    # ---- Pipeline ---------------------------------------------------------
    # Write uploaded PDF to a temp file (camelot needs a path)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_file.read())
        pdf_path = tmp.name

    xl_bytes = xl_file.read()

    # Read template labels per sheet
    template_sheets: Dict[str, List[str]] = {}
    from openpyxl import load_workbook as _lwb

    twb = _lwb(io.BytesIO(xl_bytes), data_only=True)
    for sn in twb.sheetnames:
        ws = twb[sn]
        labels = []
        for row in ws.iter_rows(min_col=1, max_col=1, values_only=True):
            val = row[0]
            if val is not None:
                labels.append(str(val).strip())
        template_sheets[sn] = labels

    all_results: Dict[str, Dict[str, Any]] = {}

    for stmt in selected_stmts:
        st.subheader(f"📄 {stmt}")
        progress = st.status(f"Processing {stmt}…", expanded=True)

        # --- Step 1: locate pages -----------------------------------------
        progress.write("🔍 Locating pages…")
        hint = page_hints.get(stmt)
        if hint:
            candidate_pages = [hint]
        else:
            candidate_pages = find_pages_for_statement(pdf_path, stmt)
            if not candidate_pages:
                progress.warning(
                    f"No pages with keyword matches for **{stmt}**. "
                    "Provide a page hint and retry."
                )
                progress.update(label=f"{stmt} — no pages found", state="error")
                continue

        progress.write(f"Candidate pages: {candidate_pages}")

        # --- Step 2: extract tables ---------------------------------------
        progress.write("📐 Extracting tables with Camelot…")
        best_df: Optional[pd.DataFrame] = None
        chosen_page: Optional[int] = None

        for pg in candidate_pages:
            dfs = extract_tables_from_page(pdf_path, pg, flavor=camelot_flavor)
            if not dfs:
                continue
            cand = pick_best_table(dfs)
            if cand is not None and (best_df is None or len(cand) > len(best_df)):
                best_df = cand
                chosen_page = pg

        if best_df is None:
            progress.warning(
                f"No tables extracted on pages {candidate_pages} for **{stmt}**."
            )
            progress.update(label=f"{stmt} — no tables", state="error")
            continue

        progress.write(f"Best table from page {chosen_page}: {best_df.shape[0]} rows × {best_df.shape[1]} cols")

        # Show raw extracted table
        with st.expander(f"Raw extracted table — page {chosen_page}", expanded=False):
            st.dataframe(best_df, use_container_width=True)

        # --- Step 2b (optional): LLM classification ----------------------
        if not hint:
            progress.write("🤖 Confirming statement type via LLM…")
            table_md = _df_to_markdown(best_df)
            confirmed = classify_table_as_statement(
                table_md, stmt, api_key, model, base_url or None
            )
            if not confirmed:
                progress.warning(
                    f"LLM did not confirm this table as **{stmt}**. Proceeding anyway — review carefully."
                )

        # --- Step 3: LLM row mapping --------------------------------------
        progress.write("🗂️ Mapping rows via LLM…")
        # Find template sheet for this statement
        matched_sheet = fuzzy_match_sheet(list(template_sheets.keys()), stmt)
        if matched_sheet is None:
            progress.warning(
                f"No matching sheet in template for **{stmt}**. Skipping."
            )
            progress.update(label=f"{stmt} — no matching sheet", state="error")
            continue

        tpl_labels = template_sheets[matched_sheet]
        table_md = _df_to_markdown(best_df)
        try:
            mapping = map_rows_with_llm(
                table_md, tpl_labels, api_key, model, base_url or None
            )
        except (json.JSONDecodeError, Exception) as exc:
            progress.error(f"LLM mapping failed: {exc}")
            progress.update(label=f"{stmt} — mapping error", state="error")
            continue

        # --- Build values dict -----------------------------------------------
        values = _build_values_dict(best_df, mapping)
        scale = detect_scale_from_header(best_df)

        # --- Show mapping preview & allow overrides --------------------------
        preview_rows = []
        for tpl_lbl in tpl_labels:
            ext_lbl = mapping.get(tpl_lbl)
            vals = values.get(ext_lbl, []) if ext_lbl else []
            preview_rows.append(
                {
                    "Template Row": tpl_lbl,
                    "Matched PDF Row": ext_lbl or "—",
                    "Values": ", ".join(
                        str(v) if v is not None else "" for v in vals
                    ),
                }
            )
        preview_df = pd.DataFrame(preview_rows)

        st.markdown(f"**Mapping preview** (sheet: *{matched_sheet}*)")
        edited_preview = st.data_editor(
            preview_df,
            key=f"editor_{stmt}",
            use_container_width=True,
            num_rows="fixed",
        )

        # Rebuild mapping from (possibly user-edited) preview
        final_mapping: Dict[str, Optional[str]] = {}
        for _, row in edited_preview.iterrows():
            tpl = row["Template Row"]
            ext = row["Matched PDF Row"]
            final_mapping[tpl] = ext if ext != "—" else None

        # Rebuild values in case user changed matched rows
        final_values = _build_values_dict(best_df, final_mapping)

        all_results[stmt] = {
            "statement_type": stmt,
            "page": chosen_page,
            "mapping": final_mapping,
            "values": final_values,
            "scale": scale,
        }

        progress.update(label=f"{stmt} ✅", state="complete")

    # --- Step 4: write to Excel & offer download --------------------------
    if all_results:
        st.divider()
        st.subheader("📥 Download populated Excel")
        out_bytes = write_to_template(
            xl_bytes, all_results, pdf_file.name
        )
        st.download_button(
            label="Download Excel",
            data=out_bytes,
            file_name=f"populated_{pdf_file.name.replace('.pdf', '.xlsx')}",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        st.warning("No statements were successfully extracted.")

    # Cleanup temp file
    try:
        os.unlink(pdf_path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
