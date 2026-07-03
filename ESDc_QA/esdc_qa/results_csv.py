"""Read an ESDC results-export CSV into normalized rows.

Headers are stripped of surrounding whitespace; the leading blank index column
is dropped. Returns (headers, rows) where each row is a dict keyed by header.
"""
from __future__ import annotations

import csv
import io
from typing import List, Tuple


def _read(reader) -> Tuple[List[str], List[dict]]:
    raw_headers = next(reader)
    headers = [h.strip() for h in raw_headers]
    rows = []
    for rec in reader:
        if not any(c.strip() for c in rec):
            continue
        rows.append({h: v for h, v in zip(headers, rec)})
    headers = [h for h in headers if h]  # drop blank index col from the logical header list
    return headers, rows


def load_results(path: str) -> Tuple[List[str], List[dict]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return _read(csv.reader(f))


def load_results_text(text: str) -> Tuple[List[str], List[dict]]:
    """Parse a results CSV from an uploaded string (for the web UI)."""
    if text and text[0] == "﻿":
        text = text[1:]
    return _read(csv.reader(io.StringIO(text)))


def esdc_names_column(headers: List[str]) -> str:
    """Find the ESDC-names column in either header style: friendly 'ESDC Names'
    or raw alias 'dim_esdc_grp.esdcs'."""
    for h in headers:
        seg = h.split(".")[-1].strip().lower()
        if seg == "esdcs" or h.strip().lower() in ("esdc names", "esdcs"):
            return h
    return "ESDC Names"


def which_esdcs(row: dict, col: str = "ESDC Names") -> List[str]:
    """Split the ESDC-names cell (comma-separated) into individual ESDC names."""
    cell = row.get(col, "") or ""
    return [s.strip() for s in cell.split(",") if s.strip()]
