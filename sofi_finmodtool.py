#!/usr/bin/env python3
"""sofi_finmodtool.py — Camelot PDF table extraction: helpers, exports, and CLI.

All project Python code lives in this single file.
Depends on: camelot-py, pandas.  System: ghostscript or pdfium for lattice mode.

Usage as CLI:
  python sofi_finmodtool.py sample.pdf --pages all --flavor lattice --out tables
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from typing import Iterable, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def chunked(seq: List[int], size: int) -> Iterable[List[int]]:
    """Yield successive chunks of *size* from *seq*."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_tables_safely(
    pdf_path: str,
    pages: str = "all",
    flavor: str = "lattice",
    **kwargs,
):
    """Try *flavor* first; fall back to stream if lattice yields nothing.

    Returns a Camelot ``TableList``.  Raises on unrecoverable errors.
    """
    import camelot  # lazy import — keeps module importable without camelot installed

    try:
        tables = camelot.read_pdf(pdf_path, pages=pages, flavor=flavor, **kwargs)
        if len(tables) == 0 and flavor == "lattice":
            logger.debug("No tables with lattice; retrying with stream")
            tables = camelot.read_pdf(pdf_path, pages=pages, flavor="stream", **kwargs)
        return tables
    except Exception:
        logger.exception("Camelot extraction failed for %s", pdf_path)
        raise


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def export_tables_to_csv(tables, out_prefix: str = "tables") -> List[str]:
    """Write each table to ``<out_prefix>_<i>.csv``.  Returns list of paths."""
    paths: List[str] = []
    for i, table in enumerate(tables):
        path = f"{out_prefix}_{i}.csv"
        table.to_csv(path)
        logger.info("Wrote %s", path)
        paths.append(path)
    return paths


def export_tables_to_sqlite(
    tables,
    db_path: str = "tables.db",
    table_prefix: str = "t",
) -> None:
    """Export every table as a SQLite table named ``<table_prefix><i>``."""
    conn = sqlite3.connect(db_path)
    for i, table in enumerate(tables):
        name = f"{table_prefix}{i}"
        table.df.to_sql(name, conn, index=False, if_exists="replace")
        logger.info("Exported table %s → %s", name, db_path)
    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract tables from a PDF via Camelot (lattice → stream fallback).",
    )
    p.add_argument("pdf", help="Path to the input PDF")
    p.add_argument("--pages", default="1", help="Pages to process (default: 1)")
    p.add_argument(
        "--flavor",
        choices=["lattice", "stream"],
        default="lattice",
        help="Extraction mode (default: lattice)",
    )
    p.add_argument("--out", default="tables", help="Output file prefix (default: tables)")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _build_parser().parse_args()

    tables = extract_tables_safely(args.pdf, pages=args.pages, flavor=args.flavor)
    if len(tables) == 0:
        print("No tables found.")
    else:
        export_tables_to_csv(tables, out_prefix=args.out)
        print(f"Exported {len(tables)} table(s).")


if __name__ == "__main__":
    main()
