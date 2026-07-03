"""Annotate false-negative alerts with WHY each should have matched + a remedy.

Input: a CSV of FN candidates from the `fn-sql --full` scan (alerts that satisfy
the whole rule but weren't tagged). For each alert we re-derive, locally and with
WORD-DELIMITED matching (the engine's real captionKeyword behavior), which
keyword/topic actually fired, classify it, and propose a fix:

  QUOTE_KEYWORD        matched via an unquoted multi-word keyword -> add quotes
  SUBSTRING_ARTIFACT   only matched as a substring (killnet in Skillnet) -> not real
  INVESTIGATE          matched via a valid keyword -> rule looks right; why missed?

Also flags whether the alert already carries the ESDC's OUTPUT topic via some
other mechanism (so impact can be tracked).
"""
from __future__ import annotations

import re
from typing import Dict, List

from .model import ESDC, iter_leaves
from .semantics import Semantics


def _caption_keywords(esdc: ESDC):
    """From the parsed (repaired) ESDC: sets of caption keywords by class."""
    quoted, unquoted_multi, all_kw = set(), set(), set()
    raw = esdc.raw_query
    for v in re.findall(r'captionKeyword:"([^"]+)"', raw):
        quoted.add(v.strip())
    for v in re.findall(r'captionKeyword:([^"()][^)]*?)\s*\)', raw):
        v = v.strip()
        if " " in v:
            unquoted_multi.add(v)
    for leaf in iter_leaves(esdc.logic):
        if leaf.field.lower() == "captionkeyword":
            all_kw.add(leaf.value)
    # a phrase that's quoted somewhere already works — don't treat it as unquoted
    unquoted_multi -= quoted
    return quoted, unquoted_multi, all_kw


def _word_present(phrase: str, text: str) -> bool:
    return re.search(rf'(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])', text, re.IGNORECASE) is not None


def output_topic_names(esdc_name: str, expansion: dict, sem: Semantics) -> List[str]:
    """Resolve the ESDC's output internalTopicIds (from the mapping) to names."""
    for m in expansion["data"]["mapping"]:
        if m["esdcName"].strip() == esdc_name.strip():
            names = []
            for tid in m.get("internalTopicIds", []):
                tid = str(tid).strip()
                if tid:
                    names.append(sem.topic_name(tid) or f"topic {tid}")
            return names
    return []


def annotate(esdc: ESDC, rows: List[dict], sem: Semantics,
             output_topics: List[str], caption_col: str, topics_col: str) -> List[dict]:
    quoted, unquoted_multi, all_kw = _caption_keywords(esdc)
    out = []
    for r in rows:
        cap = r.get(caption_col, "") or ""
        topics = r.get(topics_col, "") or ""
        present = sorted({k for k in all_kw if _word_present(k, cap)}, key=len, reverse=True)
        unq_present = [k for k in present if k in unquoted_multi]
        out_present = any(ot.lower() in topics.lower() for ot in output_topics)

        # ESDC keywords allow at most TRIGRAMS (<=3 space-delimited words); a
        # longer phrase can NEVER match regardless of quoting.
        over_trigram = [k for k in present if len(k.split()) > 3]
        unq_ok = [k for k in unq_present if len(k.split()) <= 3]

        if not present:
            # substrings inside larger words don't count as matches -> not a miss
            verdict = "NOT_A_MISS"
            why = "No real keyword hit — only matched as a substring inside a larger word, which the engine does not count."
            remedy = "None — exclude. Not a false negative."
        elif over_trigram:
            verdict = "TRIGRAM_VIOLATION"
            kw = ", ".join(f'"{k}"' for k in over_trigram)
            why = (f"Keyword(s) {kw} exceed the trigram limit (>3 words), so the ESDC can NEVER match "
                   f"them — the owner is using an invalid long string.")
            remedy = "; ".join(
                f'split `{k}` ({len(k.split())} words) into <=3-word phrases, '
                f'e.g. captionKeyword:"{" ".join(k.split()[:3])}" or '
                f'captionKeyword:"{" ".join(k.split()[-3:])}"'
                for k in over_trigram)
        elif unq_ok:
            verdict = "QUOTE_KEYWORD"
            kw = ", ".join(f'"{k}"' for k in unq_ok)
            why = (f"Caption names group(s) {kw}; alert satisfies the rule, but these keywords are "
                   f"UNQUOTED in the ESDC so the engine can't phrase-match them.")
            remedy = "Quote the multi-word keyword(s): " + "; ".join(
                f'change `captionKeyword:{k}` to `captionKeyword:"{k}"`' for k in unq_ok)
        else:
            verdict = "INVESTIGATE"
            why = (f"Caption contains valid keyword(s) {present[:3]} and the alert satisfies the rule, "
                   f"yet it wasn't tagged — keyword syntax looks correct.")
            remedy = ("Investigate non-keyword cause: modelset exclusion, topic mismatch, or ESDC "
                      "enrichment timing. Not a quoting issue.")

        out.append({
            "alert_id": r.get("ALERT_ID") or r.get("Alert ID", ""),
            "source_link": r.get("SOURCE_LINK") or r.get("Source Link", ""),
            "matched_keywords": " | ".join(present) or "(none — substring only)",
            "matched_topics_sample": ", ".join(
                t for t in (topics.split(",")) if t.strip())[:120],
            "output_topic_present": "Yes" if out_present else "No",
            "verdict": verdict,
            "why_should_have_matched": why,
            "proposed_remedy": remedy,
            "caption": cap.replace("\n", " ")[:300],
        })
    return out
