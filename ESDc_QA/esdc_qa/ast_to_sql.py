"""Compile a full ESDC AST into a Snowflake WHERE expression — engine-faithful.

This is the rigorous false-negative test: an alert is a TRUE false negative only
if it satisfies the ENTIRE ESDC boolean (every AND/OR/UNOT clause) as the ESDC
engine would actually evaluate it, yet was not tagged with the ESDC.

Fidelity rules (from the ESDc Internal Education doc), so results are accurate
across every ESDC in the library:

  captionKeyword   space-delimited, NO substring, case-insensitive, <=3 words
  originalText     substring OK, CASE-SENSITIVE, <=3 words
  topicId          membership in the comma-delimited INTERNAL_TOPICS list (exact element)
  modelset         exact match on MODELSET_NAME (not substring: avoids v2 vs v2d)
  alertThreshold   tier equality on DE_THRESHOLD_ABBR
  geo              resolved place equals COUNTRY (or appears in LOCATION_NAME)
  source/Channel   exact identifier match
  hasCompanyTag    company-tag present / absent

Keywords longer than a trigram (>3 words) can NEVER match in-engine, so their
leaf compiles to FALSE and the violation is recorded (a rule-definition bug, not
a false negative). Fields we cannot evaluate from the export (CVE/FSX/lagging/re
and any future unknowns) compile to a non-constraining TRUE and are recorded so
the query header flags the ESDC as not-fully-verifiable.
"""
from __future__ import annotations

from typing import List, Set

from .model import BoolNode, ESDC, LeafNode, Node, is_leaf
from .semantics import Semantics, threshold_abbr_variants

MAX_NGRAM = 3  # "up to trigrams permitted"

TEXT_COLS = {
    "originaltext": 'fact_alert."ORIGINAL_TEXT"',
    "translatedtext": 'fact_alert."TRANSLATED_TEXT"',
    "captionkeyword": 'fact_alert."CAPTION"',
}
FREETEXT_COLS = ['fact_alert."CAPTION"', 'fact_alert."ORIGINAL_TEXT"', 'fact_alert."TRANSLATED_TEXT"']

JOINS = {
    "esdc": 'LEFT JOIN EDW.DIM_ESDC_GRP AS dim_esdc_grp\n       ON fact_alert."KEY_ESDC_GRP" = dim_esdc_grp."KEY_ESDC_GRP"',
    "topic": 'LEFT JOIN EDW.DIM_INTERNAL_TOPIC_GRP AS dim_internal_topic_grp\n       ON fact_alert."KEY_INTERNAL_TOPIC_GRP" = dim_internal_topic_grp."KEY_INTERNAL_TOPIC_GRP"',
    "modelset": 'LEFT JOIN EDW.DIM_AUTO_ALERT_MODELSET AS dim_auto_alert_modelset\n       ON fact_alert."KEY_AUTO_ALERT_MODELSET" = dim_auto_alert_modelset."KEY_AUTO_ALERT_MODELSET"',
    "threshold": 'LEFT JOIN EDW.DIM_FLAG AS alert_top_threshold\n       ON fact_alert."KEY_PRIMARY_FLAG" = alert_top_threshold."KEY_FLAG"',
    "source": 'LEFT JOIN EDW.DIM_SOURCE AS dim_source\n       ON fact_alert."KEY_SOURCE" = dim_source."KEY_SOURCE"',
    "channel": 'LEFT JOIN EDW.DIM_SOURCE_CHANNEL AS primary_source_channel\n       ON fact_alert."KEY_PRIMARY_SOURCE_CHANNEL" = primary_source_channel."KEY_SOURCE_CHANNEL"',
    "geo": 'LEFT JOIN EDW.DIM_INTERNAL_PLACE AS dim_internal_place\n       ON fact_alert."KEY_INTERNAL_PLACE" = dim_internal_place."KEY_INTERNAL_PLACE"',
}


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _ngrams(val: str) -> int:
    return len(val.split())


# --- per-field predicate builders -------------------------------------------
# All columns are COALESCE'd to '' so a NULL (from a LEFT JOIN) yields a clean
# FALSE rather than NULL — otherwise NOT(NULL)=NULL would silently drop alerts
# that should PASS an exclusion (e.g. a NULL modelset under a UNOT clause).

def _c(col: str) -> str:
    return f"COALESCE({col}, '')"


def _substr_cs(col: str, val: str) -> str:
    """Case-sensitive substring (originalText)."""
    return f"CONTAINS({_c(col)}, {_q(val)})"


def _contains_ci(col: str, val: str) -> str:
    return f"CONTAINS(LOWER({_c(col)}), LOWER({_q(val)}))"


def _word_ci(col: str, val: str) -> str:
    """Space-delimited, case-insensitive (captionKeyword): pad with spaces so
    'killnet' won't match inside 'Skillnet'."""
    return f"CONTAINS(' ' || LOWER({_c(col)}) || ' ', ' ' || LOWER({_q(val)}) || ' ')"


def _topic_member(col: str, name: str) -> str:
    """Exact element of the comma-delimited topic list (so 'Cyber - Policy' does
    not match 'Cyber - Policy and Compliance')."""
    return f"CONTAINS(', ' || LOWER({_c(col)}) || ', ', {_q(', ' + name.lower() + ', ')})"


def _eq_ci(col: str, val: str) -> str:
    return f"LOWER({_c(col)}) = LOWER({_q(val)})"


class Compiler:
    def __init__(self, sem: Semantics):
        self.sem = sem
        self.joins: Set[str] = {"esdc"}
        self.unverifiable: List[str] = []   # fields we couldn't evaluate
        self.trigram: List[str] = []        # keyword phrases > trigram (can never match)

    def leaf(self, leaf: LeafNode) -> str:
        f = leaf.field.lower()
        v = leaf.value

        # text keyword fields share the trigram rule
        if f in ("captionkeyword", "originaltext", "translatedtext", "summarykeyword", "freetext"):
            if _ngrams(v) > MAX_NGRAM:
                self.trigram.append(f'{f}:"{v}" ({_ngrams(v)} words)')
                return f'FALSE /* over-trigram, can never match: {f}:"{v}" */'
            if f == "captionkeyword":
                return _word_ci(TEXT_COLS[f], v)
            if f in ("originaltext", "translatedtext"):
                return _substr_cs(TEXT_COLS[f], v)  # case-sensitive substring
            if f == "freetext":
                return "(" + " OR ".join(_word_ci(c, v) for c in FREETEXT_COLS) + ")"
            # summarykeyword targets the AI caption summary; there's no stored
            # column to filter on (it's AI_AGG-computed), so approximate via the
            # caption it's generated from. Verified exactly in the CSV path.
            return _word_ci('fact_alert."CAPTION"', v) + " /* summaryKeyword ~ caption (AI summary) */"

        if f in ("topicid", "pipelinetopicid"):
            self.joins.add("topic")
            name = self.sem.topic_name(v)
            if name is None:
                return f"FALSE /* topicId {v} not in topic table */"
            return _topic_member('dim_internal_topic_grp."INTERNAL_TOPICS"', name)

        if f == "modelset":
            self.joins.add("modelset")
            return _eq_ci('dim_auto_alert_modelset."MODELSET_NAME"', v)

        if f == "alertthreshold":
            self.joins.add("threshold")
            tier = self.sem.threshold_tier(v)
            if tier is None:
                return f"FALSE /* threshold {v} not in table */"
            col = 'alert_top_threshold."DE_THRESHOLD_ABBR"'
            variants = threshold_abbr_variants(tier)
            if len(variants) == 1:
                return _eq_ci(col, variants[0])
            return "(" + " OR ".join(_eq_ci(col, x) for x in variants) + ")"

        if f == "source":
            self.joins.add("source")
            return _eq_ci('dim_source."SOURCE"', v)

        if f == "sourcechannel":
            self.joins.add("channel")
            return _eq_ci('primary_source_channel."CHANNEL"', v)

        if f == "geo":
            self.joins.add("geo")
            name = self.sem.geo_name(v) or v
            return ("(" + _eq_ci('dim_internal_place."COUNTRY"', name) + " OR "
                    + _contains_ci('fact_alert."LOCATION_NAME"', name) + ")")

        if f == "hascompanytag":
            present = '(fact_alert."KEY_COMPANY_GRP" IS NOT NULL AND fact_alert."KEY_COMPANY_GRP" <> -1)'
            return present if v.strip().lower() in ("true", "yes", "1") else f"NOT {present}"

        # genuinely unknown field (CVE / FSX / lagging / re / future) — cannot verify
        self.unverifiable.append(f'{f}:"{v}"')
        return f'TRUE /* {f} not verifiable from export */'

    def node(self, n: Node) -> str:
        if is_leaf(n):
            return self.leaf(n)
        if n.op == "NOT":
            return "NOT (" + self.node(n.clauses[0]) + ")"
        joiner = " AND " if n.op == "AND" else " OR "
        return "(" + joiner.join(self.node(c) for c in n.clauses) + ")"


def build_full_fn_sql(esdc: ESDC, sem: Semantics, days_back: int = 7,
                      limit: int = 1000, ai_summary: bool = False) -> str:
    """House-style true-FN query: same columns/joins/date-window as the example
    extraction queries, with the full rule logic in the WHERE."""
    from .sql_template import build_query

    c = Compiler(sem)
    logic = c.node(esdc.logic)

    header_lines = [
        f"-- TRUE false-negative scan for ESDC: {esdc.name}",
        "-- Alerts that satisfy the ENTIRE ESDC boolean but were NOT tagged with it.",
        "-- Engine-faithful: captionKeyword space-delimited/no-substring; originalText",
        "-- case-sensitive substring; topicId/modelset/threshold/source exact.",
    ]
    if c.unverifiable:
        header_lines.append("-- !! NOT FULLY VERIFIABLE — leaves compiled as non-constraining TRUE:")
        header_lines += [f"--      - {x}" for x in dict.fromkeys(c.unverifiable)]
    if c.trigram:
        header_lines.append("-- !! RULE BUG — keyword(s) over the trigram limit (compiled FALSE; split them):")
        header_lines += [f"--      - {x}" for x in dict.fromkeys(c.trigram)]

    name = esdc.name.replace("'", "''")
    where = (f"    (\n    {logic}\n    )\n"
             f"    AND (dim_esdc_grp.\"ESDCS\" IS NULL "
             f"OR UPPER(dim_esdc_grp.\"ESDCS\") NOT LIKE UPPER('%{name}%'))")
    return build_query(where, days_back, limit, ai_summary, header="\n".join(header_lines))


def build_staged_impact_sql(name: str, prod_e, staged_e, sem: Semantics,
                            days_back: int = 7, limit: int = 1000) -> str:
    """System-wide impact: alerts whose match status FLIPS between the prod and
    staged definitions. impact = GAINED (staged matches, prod didn't) or LOST.

    A CTE computes prod_match / staged_match ONCE each (instead of inlining both
    logics 4x), so even large rules produce a compact query."""
    from .sql_template import COLUMNS, JOINS, _date_window

    prod_l = Compiler(sem).node(prod_e.logic) if prod_e and prod_e.logic is not None else "FALSE"
    stg_l = Compiler(sem).node(staged_e.logic) if staged_e and staged_e.logic is not None else "FALSE"
    cols = [c for c in COLUMNS if not c[2]]  # non-aggregate columns
    inner = ",\n    ".join(f"{expr} AS {alias}" for alias, expr, _ in cols)
    outer = ",\n    ".join(alias for alias, _, _ in cols)
    header = (f"-- Staged-vs-production impact for ESDC: {name}\n"
              f"-- Alerts whose match status FLIPS between staged and prod over the last\n"
              f"-- {days_back} day(s): impact = GAINED (staged matches, prod didn't) or LOST.\n"
              f"-- Engine-faithful matching; prod/staged logic each evaluated once via a CTE.")
    return f"""\
{header}
WITH scored AS (
  SELECT
    {inner},
    ({prod_l}) AS prod_match,
    ({stg_l}) AS staged_match
  FROM "EDW"."FACT_ALERT" AS fact_alert
{JOINS}
  WHERE {_date_window(days_back)}
)
SELECT
    CASE WHEN staged_match AND NOT prod_match THEN 'GAINED'
         WHEN prod_match AND NOT staged_match THEN 'LOST' END AS impact,
    {outer}
FROM scored
WHERE (staged_match AND NOT prod_match) OR (prod_match AND NOT staged_match)
ORDER BY alert_id
FETCH NEXT {int(limit)} ROWS ONLY;
"""


def compile_report(esdc: ESDC, sem: Semantics):
    """Compile and return (sql, caveats dict) without writing — for auditing."""
    c = Compiler(sem)
    try:
        c.node(esdc.logic)
        ok = True
    except Exception:
        ok = False
    return {
        "name": esdc.name,
        "ok": ok,
        "unverifiable": sorted(set(c.unverifiable)),
        "trigram": sorted(set(c.trigram)),
        "joins": sorted(c.joins),
    }
