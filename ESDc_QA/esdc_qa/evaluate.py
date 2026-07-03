"""Evaluate an ESDC against an alert row AND explain *why* it matched.

Three-valued per leaf:
  TRUE      condition satisfied (with evidence: matched text + snippet, or resolved topic/tier)
  FALSE     condition not satisfied
  UNKNOWN   field's column isn't in the export -> can't verify locally

Bool combination treats UNKNOWN optimistically for rows that already matched
in-system: an AND with a satisfied-or-unknown set is reported as matched-with-caveats,
so the why-output shows exactly which leaves are confirmed vs assumed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import List, Optional

from .model import LeafNode, Node, is_leaf
from .semantics import (GEO, TEXT, THRESHOLD, TOPIC, UNVERIFIABLE, Semantics,
                        field_spec, threshold_abbr_variants)

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
SNIPPET_PAD = 35


@dataclass
class LeafEval:
    leaf: LeafNode
    state: str                     # TRUE / FALSE / UNKNOWN
    column: Optional[str] = None   # resolved CSV column tested
    evidence: str = ""             # matched substring / resolved name
    snippet: str = ""              # surrounding context
    note: str = ""                 # e.g. why UNKNOWN, or resolved meaning
    blocking: bool = False         # lives under a NOT -> firing EXCLUDES the alert

    def explain(self) -> str:
        f = self.leaf.field
        if self.state == UNKNOWN:
            return f'{f}:"{self.leaf.value}" [UNVERIFIED — {self.note}]'
        verb = "EXCLUDES" if self.blocking else "matched"
        ev = f' -> "{self.evidence}"' if self.evidence else ""
        col = f" ({self.column})" if self.column else ""
        return f'{f}:"{self.leaf.value}" {verb}{col}{ev}'


@dataclass
class EvalResult:
    matched: bool
    confirmed: List[LeafEval] = dc_field(default_factory=list)   # TRUE positive leaves
    blockers: List[LeafEval] = dc_field(default_factory=list)    # TRUE leaves under NOT (exclusions)
    unknowns: List[LeafEval] = dc_field(default_factory=list)    # UNKNOWN leaves
    failed: List[LeafEval] = dc_field(default_factory=list)      # FALSE leaves (why a FN missed)

    @property
    def fully_verified(self) -> bool:
        return not self.unknowns

    def why(self) -> str:
        parts = [e.explain() for e in self.confirmed]
        parts += [e.explain() for e in self.blockers]
        parts += [e.explain() for e in self.unknowns]
        return " ; ".join(parts) if parts else "(nothing fired)"


def _text(row: dict, col: str) -> str:
    return str(row.get(col, "") or "")


def _eval_leaf(leaf: LeafNode, row: dict, sem: Semantics, headers: List[str]) -> LeafEval:
    spec = field_spec(leaf.field)
    col = spec.resolve_column(headers)

    if spec.kind == UNVERIFIABLE or (spec.kind != THRESHOLD and col is None):
        # geo with no resolvable column also lands here
        reason = "no column in export" if col is None else "field not verifiable"
        return LeafEval(leaf, UNKNOWN, note=reason)

    if spec.kind == TEXT:
        text = _text(row, col)
        hay = text if spec.case_sensitive else text.lower()
        needle = leaf.value if spec.case_sensitive else leaf.value.lower()
        span = None
        if spec.word_boundary:
            flags = 0 if spec.case_sensitive else re.IGNORECASE
            m = re.search(rf"(?<!\w){re.escape(leaf.value)}(?!\w)", text, flags)
            if m:
                span = m.span()
        else:
            i = hay.find(needle)
            if i >= 0:
                span = (i, i + len(needle))
        if span is None:
            return LeafEval(leaf, FALSE, column=col)
        s, e = span
        snip = text[max(0, s - SNIPPET_PAD): e + SNIPPET_PAD].replace("\n", " ").strip()
        return LeafEval(leaf, TRUE, column=col, evidence=text[s:e], snippet=snip)

    if spec.kind == TOPIC:
        name = sem.topic_name(leaf.value)
        if name is None:
            return LeafEval(leaf, UNKNOWN, column=col, note=f"topicId {leaf.value} not in topic table")
        present = name.lower() in _text(row, col).lower()
        st = TRUE if present else FALSE
        return LeafEval(leaf, st, column=col, evidence=name if present else "",
                        note=f"topicId {leaf.value} = {name!r}")

    if spec.kind == THRESHOLD:
        tier = sem.threshold_tier(leaf.value)
        if col is None:
            return LeafEval(leaf, UNKNOWN, note=f"threshold {leaf.value} = {tier!r}; no column")
        if tier is None:
            return LeafEval(leaf, UNKNOWN, column=col, note=f"threshold code {leaf.value} not in table")
        actual = _text(row, col).strip().lower()
        present = any(v.strip().lower() == actual for v in threshold_abbr_variants(tier))
        st = TRUE if present else FALSE
        return LeafEval(leaf, st, column=col, evidence=tier if present else "",
                        note=f"{leaf.value} = tier {tier!r}")

    if spec.kind == GEO:
        name = sem.geo_name(leaf.value) or leaf.value
        present = name.lower() in _text(row, col).lower()
        st = TRUE if present else FALSE
        return LeafEval(leaf, st, column=col, evidence=name if present else "",
                        note=f"geo {leaf.value} = {name!r}")

    return LeafEval(leaf, UNKNOWN, note="unhandled field kind")


def _collect(node: Node, row, sem, headers, under_not, acc: List[LeafEval]):
    """Walk tree, evaluate leaves, return tri-state for the node."""
    if is_leaf(node):
        le = _eval_leaf(node, row, sem, headers)
        le.blocking = under_not
        acc.append(le)
        return le.state

    if node.op == "NOT":
        inner = _collect(node.clauses[0], row, sem, headers, not under_not, acc)
        return {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}[inner]

    states = [_collect(c, row, sem, headers, under_not, acc) for c in node.clauses]
    if node.op == "AND":
        if FALSE in states:
            return FALSE
        return UNKNOWN if UNKNOWN in states else TRUE
    else:  # OR
        if TRUE in states:
            return TRUE
        return UNKNOWN if UNKNOWN in states else FALSE


def evaluate(node: Node, row: dict, sem: Semantics, headers: List[str]) -> EvalResult:
    acc: List[LeafEval] = []
    state = _collect(node, row, sem, headers, False, acc)
    res = EvalResult(matched=(state != FALSE))  # TRUE or UNKNOWN counts as matched-with-caveats
    for le in acc:
        if le.state == UNKNOWN:
            res.unknowns.append(le)
        elif le.state == TRUE and le.blocking:
            res.blockers.append(le)
        elif le.state == TRUE:
            res.confirmed.append(le)
        else:
            res.failed.append(le)
    return res
