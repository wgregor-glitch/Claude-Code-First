"""ESDC rule model — the AST the Dataminr expansion query parses into.

Two node kinds:
  BoolNode  - op in {AND, OR, NOT}  (Dataminr `UNOT` maps to NOT)
  LeafNode  - one `field:value` term, e.g. originalText:"РЭБ" or topicId:188275

The raw logic lives in expansion.json as a query STRING (see query_parser.py),
not as nested JSON. We parse it into this tree so we can both evaluate it and
explain which leaves fired.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import List, Union

VALID_OPS = {"AND", "OR", "NOT"}


@dataclass
class LeafNode:
    field: str          # Dataminr field, e.g. "originalText", "topicId", "alertThreshold"
    value: str          # the term (quotes already stripped)
    quoted: bool = False  # was it a quoted phrase in the source query

    @property
    def key(self) -> str:
        return self.field.lower()


@dataclass
class BoolNode:
    op: str
    clauses: List["Node"] = dc_field(default_factory=list)

    def __post_init__(self):
        if self.op not in VALID_OPS:
            raise ValueError(f"bool op must be one of {VALID_OPS}, got {self.op!r}")


Node = Union[BoolNode, LeafNode]


@dataclass
class ESDC:
    id: str
    name: str
    logic: Node
    stemming: bool = False
    raw_query: str = ""
    parse_error: str = ""


def is_leaf(node) -> bool:
    """True for a LeafNode. Duck-typed (no isinstance) so it stays correct even
    when a hot-reload gives the AST a different class identity than the caller."""
    return not hasattr(node, "clauses")


def iter_leaves(node: Node):
    if is_leaf(node):
        yield node
    else:
        for c in node.clauses:
            yield from iter_leaves(c)


def pretty(node: Node, indent: int = 0) -> str:
    pad = "  " * indent
    if is_leaf(node):
        v = f'"{node.value}"' if node.quoted else node.value
        return f"{pad}{node.field}:{v}"
    lines = [f"{pad}{node.op}"]
    for c in node.clauses:
        lines.append(pretty(c, indent + 1))
    return "\n".join(lines)
