"""House-style Snowflake template — mirrors the queries you actually run.

Same SELECT columns/aliases, the full DIM joins, and the timezone-aware date
window from the example extraction query. Two consumers:

  build_match_extraction()  WHERE ESDCS LIKE '%name%'         (pull matched alerts)
  (ast_to_sql) FN scan      WHERE <full rule> AND NOT tagged   (false negatives)

Both emit the same enrichment columns, so output CSVs feed straight back into the
explain / fn-report modes.
"""
from __future__ import annotations


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# ordered (alias, expression, is_aggregate) — matches the example export columns
COLUMNS = [
    ("esdcs", 'dim_esdc_grp."ESDCS"', False),
    ("alert_id", 'fact_alert."ALERT_ID"', False),
    ("original_text",
     'CASE WHEN LEN(fact_alert."ORIGINAL_TEXT") > 49000 '
     "THEN CONCAT(SUBSTR(fact_alert.\"ORIGINAL_TEXT\",1,49000),'... TEXT TRUNCATED AT 49K CHARS') "
     'ELSE fact_alert."ORIGINAL_TEXT" END', False),
    ("source_link", 'fact_alert."SOURCE_LINK"', False),
    ("caption", 'fact_alert."CAPTION"', False),
    ("internal_topics", 'dim_internal_topic_grp."INTERNAL_TOPICS"', False),
    ("top_threshold", 'alert_top_threshold."DE_THRESHOLD_ABBR"', False),
    ("translated_text",
     'CASE WHEN LEN(fact_alert."TRANSLATED_TEXT") > 49000 '
     "THEN CONCAT(SUBSTR(fact_alert.\"TRANSLATED_TEXT\",1,49000),'... TEXT TRUNCATED AT 49K CHARS') "
     'ELSE fact_alert."TRANSLATED_TEXT" END', False),
    ("modelset_name", 'dim_auto_alert_modelset."MODELSET_NAME"', False),
    ("source", 'dim_source."SOURCE"', False),
    ("channel", 'primary_source_channel."CHANNEL"', False),
    ("has_tagged_company",
     'CASE WHEN fact_alert."KEY_COMPANY_GRP" = -1 OR fact_alert."KEY_COMPANY_GRP" IS NULL '
     "THEN 'No' ELSE 'Yes' END", False),
    ("location_name", 'fact_alert."LOCATION_NAME"', False),
    ("country", 'dim_internal_place."COUNTRY"', False),
    ("coordinates",
     'CASE WHEN fact_alert."LAT" IS NOT NULL AND fact_alert."LONG" IS NOT NULL THEN '
     "COALESCE(CAST(fact_alert.\"LAT\" AS VARCHAR),'') || ',' || "
     "COALESCE(CAST(fact_alert.\"LONG\" AS VARCHAR),'') END", False),
    ("alert_caption_ai_summary", "AI_AGG(CONCAT(fact_alert.\"ALERT_OBJ_TIMESTAMP\",':',fact_alert.\"CAPTION\"), '')", True),
]

JOINS = """\
LEFT JOIN "EDW"."DIM_FLAG" AS alert_top_threshold
       ON fact_alert."KEY_PRIMARY_FLAG" = alert_top_threshold."KEY_FLAG"
LEFT JOIN "EDW"."DIM_INTERNAL_PLACE" AS dim_internal_place
       ON fact_alert."KEY_INTERNAL_PLACE" = dim_internal_place."KEY_INTERNAL_PLACE"
LEFT JOIN "EDW"."DIM_ESDC_GRP" AS dim_esdc_grp
       ON fact_alert."KEY_ESDC_GRP" = dim_esdc_grp."KEY_ESDC_GRP"
LEFT JOIN "EDW"."DIM_SOURCE" AS dim_source
       ON fact_alert."KEY_SOURCE" = dim_source."KEY_SOURCE"
LEFT JOIN "EDW"."DIM_SOURCE_CHANNEL" AS primary_source_channel
       ON fact_alert."KEY_PRIMARY_SOURCE_CHANNEL" = primary_source_channel."KEY_SOURCE_CHANNEL"
LEFT JOIN "EDW"."DIM_AUTO_ALERT_MODELSET" AS dim_auto_alert_modelset
       ON fact_alert."KEY_AUTO_ALERT_MODELSET" = dim_auto_alert_modelset."KEY_AUTO_ALERT_MODELSET"
LEFT JOIN "EDW"."DIM_INTERNAL_TOPIC_GRP" AS dim_internal_topic_grp
       ON fact_alert."KEY_INTERNAL_TOPIC_GRP" = dim_internal_topic_grp."KEY_INTERNAL_TOPIC_GRP\""""


def _date_window(days: int) -> str:
    return (
        'fact_alert."ALERT_OBJ_TIMESTAMP" >= '
        "CONVERT_TIMEZONE('America/New_York', 'UTC', CAST(DATEADD('day', -%d, "
        "DATE_TRUNC('day', CONVERT_TIMEZONE('UTC', 'America/New_York', "
        "CAST(CURRENT_TIMESTAMP() AS TIMESTAMP_NTZ)))) AS TIMESTAMP_NTZ))" % days
    )


def build_query(where_clause: str, days: int = 7, limit: int = 500,
                ai_summary: bool = False, header: str = "", extra_cols=None) -> str:
    """Assemble a house-style query with the given WHERE body. extra_cols is a
    list of pre-formatted 'expr AS alias' strings prepended to the SELECT."""
    cols = [c for c in COLUMNS if ai_summary or not c[2]]
    select = ",\n    ".join(list(extra_cols or []) + [f"{expr} AS {alias}" for alias, expr, _ in cols])
    group_by = ""
    if ai_summary:
        n = sum(1 for c in cols if not c[2])
        group_by = "\nGROUP BY\n    " + ",\n    ".join(str(i + 1) for i in range(n))
    head = (header + "\n") if header else ""
    return f"""\
{head}SELECT
    {select}
FROM "EDW"."FACT_ALERT" AS fact_alert
{JOINS}
WHERE {_date_window(days)}
  AND (
{where_clause}
  ){group_by}
ORDER BY 2
FETCH NEXT {int(limit)} ROWS ONLY;
"""


def build_match_extraction(esdc_name: str, days: int = 7, limit: int = 500,
                           ai_summary: bool = False) -> str:
    """Pull alerts that matched an ESDC (the input for Alert Match Query)."""
    where = f"    UPPER(dim_esdc_grp.\"ESDCS\") LIKE UPPER({_q('%' + esdc_name + '%')})"
    header = (f"-- Matched-alert extraction for ESDC: {esdc_name}\n"
              f"-- Alerts tagged with this ESDC over the last {days} day(s).")
    return build_query(where, days, limit, ai_summary, header)
