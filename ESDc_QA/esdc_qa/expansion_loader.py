"""Load ESDC rules from expansion.json (v1) and parse their query strings.

expansion.json shape:
  data.expansionQuery[]: {id, name, query, isStemming}
  data.mapping[]:        {esdcId, esdcName, powerstreamId, ...}

(v2) load_from_git() in loader.py will fetch the same JSON from a git ref.
"""
from __future__ import annotations

import json
from typing import Dict, List

from .model import ESDC
from .query_parser import parse_query


def load_expansion(path: str) -> Dict[str, ESDC]:
    """Return {esdc_name: ESDC} parsed from an expansion.json file."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    return _parse_expansion(data)


def load_expansion_text(text: str) -> Dict[str, ESDC]:
    """Parse expansion JSON from a string (e.g. an uploaded staged file)."""
    if text and text[0] == "﻿":
        text = text[1:]
    return _parse_expansion(json.loads(text))


def _parse_expansion(data: dict) -> Dict[str, ESDC]:
    out: Dict[str, ESDC] = {}
    for q in data["data"]["expansionQuery"]:
        try:
            logic = parse_query(q["query"])
        except Exception as e:  # keep going; report parse failures
            logic = None
            err = str(e)
        else:
            err = ""
        esdc = ESDC(
            id=str(q.get("id", "")),
            name=q.get("name", "").strip(),  # ESDC names can have stray trailing spaces
            logic=logic,
            stemming=bool(q.get("isStemming", False)),
            raw_query=q.get("query", ""),
            parse_error=err,
        )
        out[esdc.name] = esdc
    return out


def find_esdc(esdcs: Dict[str, ESDC], needle: str) -> List[ESDC]:
    """Fuzzy lookup by case-insensitive substring of the ESDC name."""
    n = needle.strip().lower()
    return [e for name, e in esdcs.items() if n in name.lower()]
