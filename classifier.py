"""
Public disruption event classifier.

Supports two backends:
  - LiteLLM proxy  (default) — routes to any model via the OpenAI-compatible
    interface at LITELLM_PROXY_URL.  Pass model="<provider>/<name>" or any
    model alias configured on the proxy.
  - Anthropic direct          — uses the Anthropic SDK with prompt caching;
    set use_proxy=False and optionally pass an anthropic.Anthropic client.
"""

import json
import os
from typing import Any

import anthropic
import openai
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LITELLM_PROXY_URL = "https://llm-proxy-test.dataminr.com/"
DEFAULT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a public disruption event classifier. Your task is to determine whether an event description represents an alertable public disruption — a kinetic event involving people that obstructs daily life or creates a meaningful public safety concern.

An event is alertable (Yes) if it meets at least ONE of the following criteria:
- Potential for harm to people or property
- Potential disruption to infrastructure (roads, bridges, transit, public facilities)
- Prevention of freedom of movement for people in their daily lives
- People gathering in public spaces (streets, intersections, overpasses, plazas, government buildings) to express political or social opposition

An event is NOT alertable (No) if:
- It uses words like "demonstration", "rally", "march", "strike", "action", or "protest" but in a non-political context (product demos, religious rallies, the calendar month of March, sports metaphors, etc.)
- It is a cultural celebration, commemoration, or parade without an oppositional/disruptive purpose
- It is preparation for a protest (sign-making, planning meetings) rather than the protest itself
- It is a cancelled event
- The description is too vague to confirm any disruption criteria are met
- It is an indoor organizational meeting, film screening, or awareness campaign without a public gathering component

IMPORTANT DISAMBIGUATION RULES:
- "Demonstration" can mean a political protest OR a product/skill showcase (cooking demo, floral demo, sewing demo, historical reenactment). Only political demonstrations are alertable.
- "Rally" can mean a political gathering OR a religious service, youth event, motorsport event, RV campout, or fundraiser. Only political rallies are alertable.
- "March" can mean a protest march (verb) OR the calendar month. Only protest marches are alertable.
- "Strike" can mean a labor strike OR a sports/metaphorical term ("Strike Out Cancer"). Only labor strikes are alertable.

Given an event description, output Yes if the event is an alertable public disruption, or No if it is not.

EXAMPLES:

--- Example 1 ---
Input:
Event Name: Broad Ripple Brigade Weekly Overpass Protest
Event Description: Park at Monon Place Apartments. Protest on the Kessler Blvd Overpass (where the Monon trail passes over Kessler near Carval Avenue). RSVP and more info at bripple@gmail.com
Event Start time: 2026-03-05 16:00:00
Event End Time: 2027-02-18 17:00:00
Event location: Monon Place Apartments

Output: Yes
Reasoning: This is a recurring protest on a highway overpass — a piece of public infrastructure. Protesters gathering on an overpass over a major road create potential disruption to traffic and freedom of movement.

--- Example 2 ---
Input:
Event Name: Abolish ICE Rally
Event Description: Robert Hunter will stand alongside community members, advocates, and local leaders at the Abolish ICE Rally to demand dignity, fairness, and humane immigration policies. This rally is about: Protecting families. Defending human rights. Calling for transparency and accountability. Building a system that reflects our shared values. Whether you are a longtime advocate or just beginning to get involved, your voice matters. Bring your friends, signs, and energy as we come together in solidarity.
Event Start time: 2026-03-07 19:00:00
Event End Time: 2026-03-07 21:00:00
Event location: Rainbow City Community Center

Output: Yes
Reasoning: This is a political rally mobilizing people to publicly oppose government immigration policy (ICE). Attendees are told to bring signs and energy, indicating a public demonstration with potential to disrupt the surrounding area.

--- Example 3 ---
Input:
Event Name: EMERGENCY ANTIWAR PROTEST
Event Description: EMERGENCY ANTIWAR PROTEST: Tomorrow, Sunday March 1st. Belleville Public Square. 2pm-4pm
Event Start time: 2026-03-01 14:00:00
Event End Time: 2026-03-01 16:00:00
Event location: Belleville Public Square

Output: Yes
Reasoning: An emergency anti-war protest at a public square. The urgency and political nature, combined with a public gathering location, indicate potential for disruption to the surrounding area and freedom of movement.

--- Example 4 ---
Input:
Event Name: NO KINGS III PROTEST
Event Description: Join our protest to shout out loud and clear that there are no kings in the USA and that we do NOT want ICE. Bring signs, music instruments, and friends.
Event Start time: 2026-03-28 12:00:00
Event End Time: 2026-03-28 14:00:00
Event location: The Commons in Strongsville Pearl Rd. and route 82

Output: Yes
Reasoning: A political protest at a public intersection (Pearl Rd. and route 82). Participants bringing signs, instruments, and gathering at a road intersection create clear potential for traffic disruption and public safety impact.

--- Example 5 ---
Input:
Event Name: Annual Open Floral Demonstration
Event Description: Our Annual Open Demonstration is coming soon.
Event Start time: 2026-03-19 19:30:00
Event End Time: 1970-01-01 01:00:00
Event location: 19 St Columbas Parish Hall Church Street Omagh BT78 1DG Northern Ireland

Output: No
Reasoning: Despite using the word "Demonstration", this is a floral arranging showcase at a parish hall — a skill/art demonstration, not a political protest. No political grievance, no public disruption.

--- Example 6 ---
Input:
Event Name: Hispanic Rally
Event Description: YOUTH RALLY – MARCH 28. Make plans now to join us for a powerful Youth Rally on Saturday, March 28 at 7:00 PM! 1940 N Campbell Avenue, Indianapolis, IN 46218. Pre-service prayer begins at 6:45 PM — come early and help us set the atmosphere. We are honored to have Pastor Nathan Cannon from Apostolic Christian Church in Indianapolis preaching for us. This will be a dynamic, Spirit-filled, bilingual service you do NOT want to miss. Bring a friend. Come expecting. Leave changed.
Event Start time: 2026-03-28 19:00:00
Event End Time: 1969-12-31 19:00:00
Event location: Iglesia Apostólica Pentecostal Casa de Restauración

Output: No
Reasoning: Despite using the word "Rally", this is a church youth service with a pastor preaching at a Pentecostal church. It is a religious gathering, not a political rally. No public disruption.

--- Example 7 ---
Input:
Event Name: Protest Sign-Making Party
Event Description: Put Love on Poster Board. Our faith affirms the democratic process. Our shared values call us toward equity and justice. And sometimes living those values looks like gathering around a table with markers, cardboard, and a slice of pizza. We are hosting sign-making parties for anyone who wants to prepare for the rallies happening on March 28 or any other rallies, demonstrations, vigils, etc. TWO Evenings! Monday, March 23 & Friday, March 27. We'll meet in the Fellowship Hall from 5 to 8 pm. We will provide supplies. We will provide pizza. We will provide music.
Event Start time: 2026-03-27 17:00:00
Event End Time: 1969-12-31 19:00:00
Event location: 95 N Jefferson St, Danville, IN, United States, Indiana 46122

Output: No
Reasoning: Although protest-related, this is a sign-making party held indoors in a fellowship hall — preparation for a future protest, not the protest itself. No public disruption is occurring at this event.

--- Example 8 ---
Input:
Event Name: 25th Annual Cesar Chavez and Dolores Huerta Marcha de Justicia and Celebration
Event Description: SAVE THE DATE! Join HABLA for the 25th Annual César Chávez and Dolores Huerta Marcha de Justicia and Celebration on Saturday, March 28, 2026, 10am to 12pm. Gathering at 9:15am Terrazas Library march to AB Cantu/Pan Am Hillside. Program and speakers 10:45am - Noon, more details coming soon! #SiSePuede. Grab your FREE Tickets Here to March with us!
Event Start time: 2026-03-28 10:00:00
Event End Time: 2026-03-28 12:00:00
Event location: 1105 E Cesar Chavez St, Austin, TX

Output: No
Reasoning: Despite involving a march, this is an annual cultural celebration and commemoration of historical civil rights figures (César Chávez and Dolores Huerta). It is a permitted, ticketed, celebratory event — not an oppositional protest targeting a current grievance.

--- Example 9 ---
Input:
Event Name: STRIKE OUT CANCER
Event Description:
Event Start time: 2026-03-16 17:00:00
Event End Time: 2026-03-16 20:00:00
Event location: Third Base Grill

Output: No
Reasoning: "Strike" here is a baseball metaphor ("Strike Out Cancer" at "Third Base Grill"). This is a restaurant/charity event, not a labor strike or public disruption.
"""

# Tool schema is identical for both the Anthropic and OpenAI call paths.
CLASSIFY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["Yes", "No"],
            "description": "Yes if the event is an alertable public disruption, No otherwise.",
        },
        "reasoning": {
            "type": "string",
            "description": "Brief explanation of why the event is or is not alertable.",
        },
    },
    "required": ["classification", "reasoning"],
}

# Anthropic-SDK tool definition (used by the direct path)
CLASSIFY_TOOL_ANTHROPIC = {
    "name": "classify_event",
    "description": "Classify whether an event is an alertable public disruption.",
    "input_schema": CLASSIFY_TOOL_SCHEMA,
}

# OpenAI-SDK tool definition (used by the proxy path)
CLASSIFY_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "classify_event",
        "description": "Classify whether an event is an alertable public disruption.",
        "parameters": CLASSIFY_TOOL_SCHEMA,
    },
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class ClassificationResult(BaseModel):
    classification: str  # "Yes" or "No"
    reasoning: str
    model: str = ""  # echoed back for transparency


# ---------------------------------------------------------------------------
# Core classifier
# ---------------------------------------------------------------------------


def _build_user_message(name: str, description: str, start_time: str, end_time: str, location: str) -> str:
    return (
        f"Event Name: {name}\n"
        f"Event Description: {description}\n"
        f"Event Start time: {start_time}\n"
        f"Event End Time: {end_time}\n"
        f"Event location: {location}"
    )


def _classify_via_proxy(
    user_message: str,
    model: str,
    proxy_url: str,
    api_key: str,
) -> ClassificationResult:
    """Call the LiteLLM proxy using the OpenAI-compatible interface."""
    oa_client = openai.OpenAI(base_url=proxy_url, api_key=api_key)

    response = oa_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        tools=[CLASSIFY_TOOL_OPENAI],
        tool_choice={"type": "function", "function": {"name": "classify_event"}},
        max_tokens=1024,
    )

    choice = response.choices[0]
    tool_calls = choice.message.tool_calls
    if not tool_calls:
        raise RuntimeError(f"Model did not call classify_event tool. Response: {response}")

    args = json.loads(tool_calls[0].function.arguments)
    return ClassificationResult(
        classification=args["classification"],
        reasoning=args["reasoning"],
        model=response.model,
    )


def _classify_via_anthropic(
    user_message: str,
    model: str,
    client: anthropic.Anthropic,
) -> ClassificationResult:
    """Call the Anthropic API directly with prompt caching."""
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # Cache the large static system prompt across calls
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[CLASSIFY_TOOL_ANTHROPIC],
        tool_choice={"type": "tool", "name": "classify_event"},
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "classify_event":
            return ClassificationResult(
                classification=block.input["classification"],
                reasoning=block.input["reasoning"],
                model=response.model,
            )

    raise RuntimeError(f"Model did not call classify_event tool. Response: {response}")


def classify_event(
    name: str,
    description: str,
    start_time: str,
    end_time: str,
    location: str,
    *,
    model: str = DEFAULT_MODEL,
    use_proxy: bool = True,
    proxy_url: str = LITELLM_PROXY_URL,
    api_key: str | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
) -> ClassificationResult:
    """Classify an event as an alertable public disruption or not.

    Args:
        name: Event name.
        description: Event description text.
        start_time: Event start datetime string.
        end_time: Event end datetime string.
        location: Event location string.
        model: Model identifier.  For the proxy this is any model alias the
               proxy supports (e.g. "claude-sonnet-4-6", "gpt-4o",
               "gemini/gemini-1.5-pro").  For the direct Anthropic path this
               must be a valid Anthropic model ID.
        use_proxy: Route through the LiteLLM proxy (default True).
        proxy_url: LiteLLM proxy base URL.
        api_key: API key for the proxy.  Falls back to the
                 LITELLM_API_KEY env var, then ANTHROPIC_API_KEY.
        anthropic_client: Pre-built Anthropic client for the direct path.

    Returns:
        ClassificationResult with classification ("Yes"/"No"), reasoning, and
        the model name echoed from the response.
    """
    user_message = _build_user_message(name, description, start_time, end_time, location)

    if use_proxy:
        resolved_key = (
            api_key
            or os.environ.get("LITELLM_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or "placeholder"
        )
        return _classify_via_proxy(user_message, model, proxy_url, resolved_key)

    # Direct Anthropic path
    client = anthropic_client or anthropic.Anthropic()
    return _classify_via_anthropic(user_message, model, client)


# ---------------------------------------------------------------------------
# Demo / CLI
# ---------------------------------------------------------------------------

DEMO_EVENTS = [
    {
        "name": "Broad Ripple Brigade Weekly Overpass Protest",
        "description": (
            "Park at Monon Place Apartments. Protest on the Kessler Blvd Overpass "
            "(where the Monon trail passes over Kessler near Carval Avenue). "
            "RSVP and more info at bripple@gmail.com"
        ),
        "start_time": "2026-03-05 16:00:00",
        "end_time": "2027-02-18 17:00:00",
        "location": "Monon Place Apartments",
    },
    {
        "name": "Annual Open Floral Demonstration",
        "description": "Our Annual Open Demonstration is coming soon.",
        "start_time": "2026-03-19 19:30:00",
        "end_time": "1970-01-01 01:00:00",
        "location": "19 St Columbas Parish Hall Church Street Omagh BT78 1DG Northern Ireland",
    },
    {
        "name": "STRIKE OUT CANCER",
        "description": "",
        "start_time": "2026-03-16 17:00:00",
        "end_time": "2026-03-16 20:00:00",
        "location": "Third Base Grill",
    },
    {
        "name": "NO KINGS III PROTEST",
        "description": (
            "Join our protest to shout out loud and clear that there are no kings "
            "in the USA and that we do NOT want ICE. Bring signs, music instruments, and friends."
        ),
        "start_time": "2026-03-28 12:00:00",
        "end_time": "2026-03-28 14:00:00",
        "location": "The Commons in Strongsville Pearl Rd. and route 82",
    },
]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Disruption event classifier demo")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Model to use via the LiteLLM proxy (default: %(default)s)",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Call the Anthropic API directly instead of the LiteLLM proxy",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"Disruption Event Classifier — Demo  [model: {args.model}]")
    if not args.no_proxy:
        print(f"Proxy: {LITELLM_PROXY_URL}")
    print("=" * 70)

    for event in DEMO_EVENTS:
        result = classify_event(
            name=event["name"],
            description=event["description"],
            start_time=event["start_time"],
            end_time=event["end_time"],
            location=event["location"],
            model=args.model,
            use_proxy=not args.no_proxy,
        )
        print(f"\nEvent:          {event['name']}")
        print(f"Classification: {result.classification}")
        print(f"Reasoning:      {result.reasoning}")
        if result.model:
            print(f"Model used:     {result.model}")
        print("-" * 70)


if __name__ == "__main__":
    main()
