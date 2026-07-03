"""False-negative / coverage-gap discovery (rule-based).

Take an ESDC and produce *relaxed* variants of its logic. Any alert in a broad
corpus that matches a relaxed variant but NOT the original is a candidate false
negative — it "almost" matched. The relaxation that let it through tells you WHY
it was missed:

  drop_not      : the NOT/threshold exclusion knocked it out (e.g. it was a low tier)
  drop_and      : it satisfied all-but-one required AND clause (rule too strict)
  stem_keyword  : it contained a morphological variant of a keyword (stem the term)

Confidence is higher when the relaxation was smaller / gentler. This is the
engine behind both the FN hunt and the v2 "should-match-but-excluded" report.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import List

from .model import BoolNode, ESDC, LeafNode, Node, is_leaf, iter_leaves


@dataclass
class Variant:
    kind: str
    description: str
    logic: Node
    confidence: float   # 0..1, higher = closer to the original rule


def _all_nodes(node: Node):
    yield node
    if not is_leaf(node):
        for c in node.clauses:
            yield from _all_nodes(c)


def _replace(root: Node, target: Node, new: Node) -> Node:
    if root is target:
        return new
    if is_leaf(root):
        return copy.deepcopy(root)
    return BoolNode(root.op, [_replace(c, target, new) for c in root.clauses])


def _remove(root: Node, target: Node):
    if root is target:
        return None
    if is_leaf(root):
        return copy.deepcopy(root)
    kept = [_remove(c, target) for c in root.clauses if c is not target]
    kept = [c for c in kept if c is not None]
    return BoolNode(root.op, kept) if kept else None


def _summ(node: Node) -> str:
    if is_leaf(node):
        return f'{node.field}:"{node.value}"'
    return node.op + "(" + ", ".join(_summ(c) for c in node.clauses[:3]) + ("…" if len(node.clauses) > 3 else "") + ")"


def generate_variants(esdc: ESDC) -> List[Variant]:
    root = esdc.logic
    variants: List[Variant] = []

    # 1. Drop each NOT subtree -> alerts excluded purely by an exclusion clause
    for node in _all_nodes(root):
        if not is_leaf(node) and node.op == "NOT":
            relaxed = _remove(root, node)
            if relaxed is not None:
                variants.append(Variant("drop_not",
                    f"removed exclusion NOT({_summ(node.clauses[0])})", relaxed, 0.9))

    # 2. Drop each top-level AND clause -> rule too strict on one requirement
    if not is_leaf(root) and root.op == "AND":
        for clause in root.clauses:
            if not is_leaf(clause) and clause.op == "NOT":
                continue
            relaxed = _remove(root, clause)
            if relaxed is not None:
                variants.append(Variant("drop_and",
                    f"dropped one required AND clause: {_summ(clause)}", relaxed, 0.6))

    # 3. Stem each text keyword -> morphological near-miss
    for leaf in iter_leaves(root):
        if leaf.field.lower() in ("originaltext", "captionkeyword", "translatedtext") and len(leaf.value) >= 5:
            keep = max(4, math.ceil(len(leaf.value) * 0.7))
            stem = leaf.value[:keep]
            if stem != leaf.value:
                new_leaf = LeafNode(field=leaf.field, value=stem, quoted=leaf.quoted)
                relaxed = _replace(root, leaf, new_leaf)
                variants.append(Variant("stem_keyword",
                    f'stemmed "{leaf.value}" -> "{stem}*"', relaxed, 0.75))

    return variants
