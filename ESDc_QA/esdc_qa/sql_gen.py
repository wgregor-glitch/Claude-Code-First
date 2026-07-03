"""Generate Snowflake false-negative queries from an ESDC's keyword terms.

Given an ESDC, pull its text terms (captionKeyword / originalText / freetext) and
emit a Snowflake query that finds alerts whose CAPTION / ORIGINAL_TEXT contains
one of those phrases but that were NOT tagged with the ESDC — i.e. candidate
false negatives. This is the bridge to the system-wide hunt: the local CSV only
has alerts that already matched; this query reaches the whole alert table.

Especially useful for the unquoted-multi-word-keyword bug: pass only the phrases
the parser flagged as unquoted to test whether quoting them would recover misses.
"""
from __future__ import annotations

import re
from typing import List

# fields whose values are free-text phrases worth searching in alert text
TEXT_FIELDS = {"captionkeyword", "originaltext", "translatedtext", "summarykeyword", "freetext"}


def extract_text_terms(raw_query: str, only_unquoted_multiword: bool = False) -> List[str]:
    """Pull text-search terms straight from the raw query string.

    Works even on ESDCs that fail to parse (the unquoted-keyword cases), since it
    regexes the source rather than the AST.
    """
    terms = set()
    field_alt = "|".join(sorted(TEXT_FIELDS, key=len, reverse=True))
    # quoted:   field:"phrase"
    for m in re.finditer(rf'({field_alt}):"+([^"]+)"+', raw_query, re.IGNORECASE):
        if not only_unquoted_multiword:
            terms.add(m.group(2).strip())
    # unquoted: field:value...  up to the closing paren
    for m in re.finditer(rf'({field_alt}):([^"\)(][^)]*?)\s*\)', raw_query, re.IGNORECASE):
        val = m.group(2).strip()
        if only_unquoted_multiword:
            if " " in val:
                terms.add(val)
        else:
            terms.add(val)
    return sorted(t for t in terms if t)


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_fn_sql(esdc_name: str, terms: List[str], days_back: int = 7,
                 columns=("CAPTION", "ORIGINAL_TEXT", "TRANSLATED_TEXT"),
                 topic_names: List[str] = None) -> str:
    """Build a Snowflake query: alerts containing a term but NOT tagged with esdc_name.

    If `topic_names` is given, the alert must ALSO sit in one of those internal
    topics — matching the ESDC's `captionKeyword AND topicId` structure, which
    drops generic-keyword noise (e.g. "hacking group") and yields precise misses.
    """
    values = ",\n    ".join(f"({_sql_str(t)})" for t in terms)
    contains = " OR\n      ".join(
        f"CONTAINS(LOWER(fact_alert.\"{c}\"), LOWER(tp.phrase))" for c in columns
    )

    topic_join = ""
    topic_filter = ""
    topic_select = ""
    if topic_names:
        tvals = ",\n    ".join(f"({_sql_str(t)})" for t in topic_names)
        topic_select = "    dim_internal_topic_grp.\"INTERNAL_TOPICS\" AS internal_topics,\n"
        topic_join = (
            '\nLEFT JOIN EDW.DIM_INTERNAL_TOPIC_GRP AS dim_internal_topic_grp\n'
            '       ON fact_alert."KEY_INTERNAL_TOPIC_GRP" = dim_internal_topic_grp."KEY_INTERNAL_TOPIC_GRP"'
        )
        conds = " OR\n         ".join(
            "CONTAINS(LOWER(dim_internal_topic_grp.\"INTERNAL_TOPICS\"), LOWER(rt.topic))"
            for _ in range(1)
        )
        topic_filter = (
            f"\n  AND EXISTS (\n"
            f"        SELECT 1 FROM (SELECT column1 AS topic FROM VALUES\n    {tvals}) rt\n"
            f"        WHERE {conds}\n"
            f"      )"
        )

    return f"""\
-- False-negative scan for ESDC: {esdc_name}
-- Finds alerts whose text contains one of the ESDC's keyword phrases but that
-- were NOT tagged with this ESDC. Non-empty rows => candidate misses
-- (and, for unquoted multi-word phrases, evidence that quotes are needed).
{"-- Topic-constrained: alert must also be in one of the ESDC's required topics." if topic_names else "-- Keyword-only: broad; add --with-topics to require the topic clause too."}
WITH target_phrases AS (
  SELECT column1 AS phrase FROM VALUES
    {values}
)
SELECT
    tp.phrase                       AS matched_phrase,
    fact_alert."ALERT_ID"           AS alert_id,
    fact_alert."CAPTION"            AS caption,
{topic_select}    dim_esdc_grp."ESDCS"            AS esdcs,
    fact_alert."SOURCE_LINK"        AS source_link
FROM EDW.FACT_ALERT AS fact_alert
LEFT JOIN EDW.DIM_ESDC_GRP AS dim_esdc_grp
       ON fact_alert."KEY_ESDC_GRP" = dim_esdc_grp."KEY_ESDC_GRP"{topic_join}
JOIN target_phrases tp
  ON ({contains})
WHERE fact_alert."ALERT_OBJ_TIMESTAMP" >=
      DATEADD('day', -{days_back}, CURRENT_TIMESTAMP())
  AND (dim_esdc_grp."ESDCS" IS NULL
       OR UPPER(dim_esdc_grp."ESDCS") NOT LIKE UPPER('%{esdc_name}%')){topic_filter}
ORDER BY tp.phrase, alert_id
FETCH NEXT 1000 ROWS ONLY;
"""
