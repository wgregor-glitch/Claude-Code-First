"""
Windward AI search-query definitions.

This module encodes Windward "AI search" style queries as structured,
machine-readable data instead of leaving them as free-text notes or as
UI-only configuration.  Each query is split into the same two groups that
the Windward query builder exposes:

  - Vessel Criteria   — filters on the vessel itself (risk assessment,
                        additional risk types, regime affiliation, …).
  - Activity Criteria — filters on vessel behaviour (dark activity,
                        location, time range, …), combined with a boolean
                        operator.

The design mirrors ``classifier.py``: pydantic models with explicit enums
so the definitions are self-documenting, validated on construction, and
serialisable to JSON for hand-off to the Windward API or to a UI.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enumerations (the fixed vocabulary the Windward builder offers)
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    """Vessel Risk Assessment levels."""

    HIGH = "High risk"
    MODERATE = "Moderate risk"
    LOW = "Low risk"


class RiskType(str, Enum):
    """Additional risk types that can be attached to a risk assessment."""

    SMUGGLING = "Smuggling"
    IUU_FISHING = "IUU fishing"
    MILITARY_AFFILIATION = "Military affiliation"
    SANCTIONS = "Sanctions"


class Regime(str, Enum):
    """Regime affiliations that qualify a Military-affiliation risk type."""

    CHINA = "China regime"
    RUSSIA = "Russia regime"
    IRAN = "Iran regime"
    NORTH_KOREA = "North Korea regime"


class ActivityType(str, Enum):
    """Vessel activity / behaviour filters."""

    DARK_ACTIVITY = "Dark activity"
    STS = "Ship-to-ship transfer"
    PORT_CALL = "Port call"
    AIS_SPOOFING = "AIS spoofing"


class BooleanOperator(str, Enum):
    """How multiple activity criteria are combined."""

    AND = "AND"
    OR = "OR"


class TimeRange(str, Enum):
    """Relative time windows offered by the builder."""

    TODAY = "Today"
    LAST_7_DAYS = "Last 7 days"
    LAST_30_DAYS = "Last 30 days"
    CUSTOM = "Custom"


# ---------------------------------------------------------------------------
# Query components
# ---------------------------------------------------------------------------


class AdditionalRiskType(BaseModel):
    """One entry in the 'Add additional risk types' list.

    ``regime`` is only meaningful for ``MILITARY_AFFILIATION`` (e.g. the
    "Military affiliation — China regime" chip shown in the UI).
    """

    risk_type: RiskType
    regime: Regime | None = None


class VesselCriteria(BaseModel):
    """The 'Vessel Criteria' group of a Windward search query."""

    # Vessel Risk Assessment — one or more selected risk levels.
    risk_levels: list[RiskLevel] = Field(default_factory=list)
    # 'Add additional risk types' — at least one indicator must match for the
    # selected risk levels.
    additional_risk_types: list[AdditionalRiskType] = Field(default_factory=list)


class ActivityCriteria(BaseModel):
    """The 'Activity Criteria' group of a Windward search query."""

    # Boolean operator combining the activity filters below.
    operator: BooleanOperator = BooleanOperator.AND
    activities: list[ActivityType] = Field(default_factory=list)
    # No location selection == None (search everywhere).
    location: str | None = None
    time_range: TimeRange = TimeRange.TODAY


class WindwardSearchQuery(BaseModel):
    """A complete Windward AI search query."""

    name: str
    vessel_criteria: VesselCriteria
    activity_criteria: ActivityCriteria


# ---------------------------------------------------------------------------
# Query definitions
# ---------------------------------------------------------------------------

# High/Moderate-risk vessels with a China-regime military affiliation that
# have gone dark anywhere in the world today.
CHINA_MILITARY_DARK_ACTIVITY = WindwardSearchQuery(
    name="China military affiliation — dark activity (today)",
    vessel_criteria=VesselCriteria(
        risk_levels=[RiskLevel.HIGH, RiskLevel.MODERATE],
        additional_risk_types=[
            AdditionalRiskType(
                risk_type=RiskType.MILITARY_AFFILIATION,
                regime=Regime.CHINA,
            ),
        ],
    ),
    activity_criteria=ActivityCriteria(
        operator=BooleanOperator.AND,
        activities=[ActivityType.DARK_ACTIVITY],
        location=None,  # no location selection — search everywhere
        time_range=TimeRange.TODAY,
    ),
)


# All defined queries, keyed by a short slug.
QUERIES: dict[str, WindwardSearchQuery] = {
    "china_military_dark_activity": CHINA_MILITARY_DARK_ACTIVITY,
}


# ---------------------------------------------------------------------------
# CLI — print the query definitions as JSON
# ---------------------------------------------------------------------------


def main() -> None:
    import json

    for slug, query in QUERIES.items():
        print("=" * 70)
        print(slug)
        print("=" * 70)
        print(json.dumps(query.model_dump(mode="json"), indent=2))
        print()


if __name__ == "__main__":
    main()
