"""Resolve ESDC leaf syntax to human meaning, using the reference tables.

Field syntax (from "ESDc - Internal Education 2024"):
  originalText:{term}      term from Original Text. CASE SENSITIVE, substrings/trigrams ok.
  captionKeyword:{"term"}  term from Caption. space-delineated, NO substring match.
  topicId:{id}             internal topic GUID  -> name via Topic Abstractions
  pipelineTopicId:{id}     same resolution as topicId
  alertThreshold:[code]    general.alertN syntax -> tier via Alert Threshold Mapping
  geo:[GUID]               location GUID -> name via Geo GUIDs.txt
  modelset:{id|name}       R&D classifier -- NOT present in result export (unverifiable here)
  source / sourceChannel   NOT present in result export (unverifiable here)

The reference files live alongside this module.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field as dc_field
from typing import Dict, Optional

HERE = os.path.dirname(__file__)

# leaf kinds
TEXT, TOPIC, THRESHOLD, GEO, UNVERIFIABLE = "text", "topic", "threshold", "geo", "unverifiable"


@dataclass
class FieldSpec:
    kind: str
    columns: list           # CANDIDATE result-CSV column names (first present one wins)
    case_sensitive: bool = False
    word_boundary: bool = False

    def resolve_column(self, available: list) -> Optional[str]:
        """Pick the first candidate column that actually exists in the CSV.

        `available` is the list of stripped header names. Match is case/space
        insensitive so the export can name columns however it likes.
        """
        def n(s):
            return s.strip().lower().replace("_", " ")

        # each header yields its full normalized form AND the part after the last
        # dot, so both friendly ("Original Text") and raw SQL aliases
        # ("fact_alert.original_text") resolve. First header wins on collision.
        norm = {}
        for h in available:
            f = n(h)
            for form in (f, f.split(".")[-1].strip()):
                norm.setdefault(form, h)

        # 1) exact normalized match
        for cand in self.columns:
            if n(cand) in norm:
                return norm[n(cand)]
        # 2) tolerate trailing annotations, e.g. "Has Tagged Company (Yes / No)"
        for cand in self.columns:
            key = n(cand)
            for hk, orig in norm.items():
                if hk.startswith(key):
                    return orig
        return None


# Dataminr field (lowercased) -> how to evaluate it against the result CSV.
# `columns` lists CANDIDATE header names; the export can use any of them. As you
# enrich the SQL with modelset/source/geo/etc., add the chosen header here (or it
# may already be covered by the aliases below).
FIELD_SPECS: Dict[str, FieldSpec] = {
    "freetext":        FieldSpec(TEXT, ["Caption", "Original Text", "Translated Text"]),
    "originaltext":    FieldSpec(TEXT, ["Original Text"], case_sensitive=True),
    "translatedtext":  FieldSpec(TEXT, ["Translated Text"]),
    "captionkeyword":  FieldSpec(TEXT, ["Caption", "Caption Keywords"], word_boundary=True),
    "summarykeyword":  FieldSpec(TEXT, ["Alert Caption Ai Summary", "Summary", "Abstract", "Subcaption"], word_boundary=True),
    "topicid":         FieldSpec(TOPIC, ["Internal Topics", "Topics"]),
    "pipelinetopicid": FieldSpec(TOPIC, ["Internal Topics", "Pipeline Topics", "Topics"]),
    "alertthreshold":  FieldSpec(THRESHOLD, ["Internal Alert Threshold", "Top Threshold", "Alert Threshold", "Threshold"]),
    "geo":             FieldSpec(GEO, ["Country", "Location Name", "Geo", "Geos", "Geo GUID", "Location", "Locations"]),
    # membership-style: verifies once the column exists, else marked unverified at runtime
    "modelset":        FieldSpec(TEXT, ["Modelset Name", "Modelset", "Modelsets", "Model Set", "Model Sets"], word_boundary=True),
    "source":          FieldSpec(TEXT, ["Source", "Source Name", "Sources"], word_boundary=True),  # NOT Source Link
    "sourcechannel":   FieldSpec(TEXT, ["Channel", "Source Channel"], word_boundary=True),
    "hascompanytag":   FieldSpec(TEXT, ["Has Tagged Company", "Company Tags", "Company Tag", "Has Company Tag", "Companies"], word_boundary=True),
}


def field_spec(field: str) -> FieldSpec:
    return FIELD_SPECS.get(field.lower(), FieldSpec(UNVERIFIABLE, []))


# The Alert Threshold Mapping's "Threshold Type" doesn't always equal the actual
# DE_THRESHOLD_ABBR in FACT_ALERT. Known divergence: the mapping says
# "Hyperspecific Signal" but the alert column reads "Hyperspecific". Match both.
_THRESHOLD_ABBR_ALIASES = {"hyperspecific signal": "Hyperspecific"}


def threshold_abbr_variants(tier: str):
    """Acceptable DE_THRESHOLD_ABBR strings for a resolved threshold tier."""
    variants = [tier]
    alias = _THRESHOLD_ABBR_ALIASES.get(tier.strip().lower())
    if alias and alias.strip().lower() != tier.strip().lower():
        variants.append(alias)
    return variants


@dataclass
class Semantics:
    topic_id_to_name: Dict[str, str] = dc_field(default_factory=dict)
    threshold_code_to_tier: Dict[str, str] = dc_field(default_factory=dict)
    geo_guid_to_name: Dict[str, str] = dc_field(default_factory=dict)

    def topic_name(self, tid: str) -> Optional[str]:
        return self.topic_id_to_name.get(str(tid).strip())

    def threshold_tier(self, code: str) -> Optional[str]:
        return self.threshold_code_to_tier.get(code.strip())

    def geo_name(self, guid: str) -> Optional[str]:
        return self.geo_guid_to_name.get(guid.strip())


def _load_topics(path: str) -> Dict[str, str]:
    m = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            guid = str(row.get("DE GUID", "")).strip()
            name = str(row.get("DE TOPICS", "")).strip()
            if guid and name:
                m[guid] = name
    return m


def _load_thresholds(path: str) -> Dict[str, str]:
    # columns: Threshold, Threshold Label, Threshold Type, ...
    m = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = str(row.get("Threshold Label", "")).strip()
            tier = str(row.get("Threshold Type", "")).strip()
            if code:
                m[code] = tier
    return m


def _load_geo(path: str) -> Dict[str, str]:
    import re
    with open(path, encoding="utf-8-sig") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
        return {str(d["guid"]).strip(): d["name"] for d in data if d.get("guid")}
    except json.JSONDecodeError:
        # file may hold concatenated arrays / NDJSON — pull name/guid pairs directly
        pairs = re.findall(r'"name"\s*:\s*"([^"]*)"\s*,\s*"guid"\s*:\s*"([^"]*)"', raw)
        return {guid.strip(): name for name, guid in pairs}


def load_semantics(base: str = HERE) -> Semantics:
    def _try(loader, name):
        p = os.path.join(base, name)
        try:
            return loader(p)
        except FileNotFoundError:
            return {}
    return Semantics(
        topic_id_to_name=_try(_load_topics, "Topic Abstractions 3.0 - Official Abstractions Copy.csv"),
        threshold_code_to_tier=_try(_load_thresholds, "Alert Threshold Mapping - Sheet1.csv"),
        geo_guid_to_name=_try(_load_geo, "Geo GUIDs.txt"),
    )
