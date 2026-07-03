"""Parse a Dataminr expansion query string into the ESDC AST.

Grammar (operators left-assoc; parens explicit in real queries):

  expr    := or_expr
  or_expr := and_expr (OR and_expr)*
  and_expr:= not_expr (AND not_expr)*
  not_expr:= UNOT not_expr | atom
  atom    := '(' expr ')' | TERM
  TERM    := field ':' value           # value quoted ("...") or bare (188275, general.alert4)

`UNOT` is Dataminr's NOT. Quoted values may appear with doubled quotes
(""phrase"") — we strip all surrounding quotes.
"""
from __future__ import annotations

import re
from typing import List, Tuple

from .model import BoolNode, LeafNode, Node

_TOKEN_RE = re.compile(
    r"""
      (?P<LPAREN>\() |
      (?P<RPAREN>\)) |
      (?P<NOT>\bUNOT\b) |
      (?P<AND>\bAND\b) |
      (?P<OR>\bOR\b) |
      (?P<TERM>(?P<field>\w+):?[ \t]*(?P<val>"+[^"]*"+|[^()\s]+)) |
      (?P<QSTRING>"+[^"]*"+) |
      (?P<WS>\s+)
    """,
    re.VERBOSE,
)


def tokenize(s: str) -> List[Tuple[str, str, str]]:
    """Return list of (kind, field, value). field/value only set for TERM."""
    toks = []
    pos = 0
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            raise ValueError(f"cannot tokenize at {pos}: {s[pos:pos+40]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "WS":
            continue
        if kind == "TERM":
            val = m.group("val")
            quoted = val.startswith('"')
            val = val.strip('"') if quoted else val
            toks.append(("TERM", m.group("field"), val))
        elif kind == "QSTRING":
            # bare quoted phrase with no field: (e.g. the deliberate World Cup
            # "...blockerrr" kill-switch). Treat as a free-text term.
            toks.append(("TERM", "freetext", m.group("QSTRING").strip('"')))
        else:
            toks.append((kind, "", ""))
    return toks


class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i][0] if self.i < len(self.toks) else None

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self) -> Node:
        node = self.or_expr()
        if self.i != len(self.toks):
            raise ValueError(f"trailing tokens at {self.i}: {self.toks[self.i:self.i+3]}")
        return node

    def or_expr(self) -> Node:
        nodes = [self.and_expr()]
        while self.peek() == "OR":
            self.next()
            nodes.append(self.and_expr())
        return nodes[0] if len(nodes) == 1 else BoolNode("OR", nodes)

    def and_expr(self) -> Node:
        nodes = [self.not_expr()]
        while self.peek() == "AND":
            self.next()
            nodes.append(self.not_expr())
        return nodes[0] if len(nodes) == 1 else BoolNode("AND", nodes)

    def not_expr(self) -> Node:
        if self.peek() == "NOT":
            self.next()
            return BoolNode("NOT", [self.not_expr()])
        return self.atom()

    def atom(self) -> Node:
        t = self.peek()
        if t == "LPAREN":
            self.next()
            node = self.or_expr()
            if self.peek() != "RPAREN":
                raise ValueError("expected )")
            self.next()
            return node
        if t == "TERM":
            _, field, val = self.next()
            return LeafNode(field=field, value=val, quoted=True)
        raise ValueError(f"unexpected token {t} at {self.i}")


def parse_query(s: str) -> Node:
    return _Parser(tokenize(s)).parse()


_TEXT_FIELDS_RE = "captionKeyword|originalText|translatedText|summaryKeyword"


def repair_query(raw: str) -> str:
    """Best-effort repair of common authoring bugs so a query parses.

    Currently: quote unquoted multi-word text-field values, e.g.
    `(captionKeyword:anonymous italia)` -> `(captionKeyword:"anonymous italia")`.
    Single-word values and already-quoted values are left untouched.
    """
    def fix(m):
        field, val = m.group(1), m.group(2).strip()
        if " " in val and not val.startswith('"'):
            return f'({field}:"{val}")'
        return m.group(0)
    out = re.sub(rf'\(({_TEXT_FIELDS_RE}):([^"()][^)]*)\)', fix, raw)
    # missing operator between adjacent terms: `"dies" captionKeyword:` -> `"dies" OR captionKeyword:`
    out = re.sub(r'"[ \t]+(?=\w+:)', '" OR ', out)
    return out
