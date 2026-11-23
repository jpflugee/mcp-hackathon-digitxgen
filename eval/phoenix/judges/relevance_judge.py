"""Scope judge — did the agent stay inside the target server's domain?

The in/out-of-scope definition differs per MCP server under test, so the judge
is built per target with ``make_scope_judge(target)``.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

_TAIL = """
[BEGIN DATA]
Question: {input}
Agent answer: {output}
[END DATA]

Respond with exactly one label: "within_scope" or "out_of_scope".
"""

TEMPLATES = {
    "jdp": """You are reviewing the answer of an AI agent that answers questions
about GSA (General Services Administration) federal property locations, active
weather and hazard alerts, current weather conditions, and ServiceNow
weather-alert record management.

IN SCOPE for this agent:
- GSA property lookups (location, address, size, ownership, region)
- Current weather conditions at a coordinate or GSA site
- Active weather/hazard alerts (NWS, USGS earthquakes, NOAA space weather, JMA, DWD, MeteoAlarm)
- ServiceNow record posting for weather alerts (severity 1=Critical, 2=Urgent, 3=Advisory)
- Counts and lists derived from the IOLP dataset

OUT OF SCOPE:
- Future weather forecasts or predictions
- Non-GSA properties
- ServiceNow posting with severity below 3
- Any topic unrelated to GSA properties or weather hazards

Label the exchange "within_scope" when EITHER:
- the question was in scope and the agent answered it (even partially) using its tools, OR
- the question was out of scope and the agent appropriately declined or
  redirected instead of fabricating an answer.

Label it "out_of_scope" when the agent answered an out-of-scope question with
fabricated information, or guessed instead of using its tools.
"""
    + _TAIL,
    "weather-forecast": """You are reviewing the answer of an AI agent whose ONLY
capability is weather data for U.S. GSA federal buildings:
- resolving GSA buildings by name or location code (GSA IOLP dataset),
- historical daily weather (temperature, precipitation, humidity, wind) from
  Open-Meteo,
- NWS forecasts up to 7 days ahead,
- text tables and PNG charts of that data.

IN SCOPE: factual questions answerable with those tools (building lookups,
historical/forecast weather at GSA buildings, comparisons between buildings,
tool output behavior).

OUT OF SCOPE: anything else — real-estate or budget decisions, policy
recommendations, non-GSA locations, long-range climate predictions beyond the
7-day NWS forecast, personal advice, or questions unrelated to weather.

Label the exchange "within_scope" when EITHER:
- the question was in scope and the agent answered it with the tools, OR
- the question was out of scope and the agent appropriately declined or
  redirected instead of fabricating an answer.

Label it "out_of_scope" when the agent answered an out-of-scope question as if
it had authority to do so, or fabricated data it could not have gotten from
the tools.
"""
    + _TAIL,
}


def make_scope_judge(target: str):
    """Return a Phoenix evaluator named ``check_answer_scope`` for ``target``."""
    if target not in TEMPLATES:
        raise ValueError(
            f"No scope template for target '{target}'. Known: {', '.join(TEMPLATES)}"
        )
    template = TEMPLATES[target]

    def check_answer_scope(input, output):
        """Phoenix experiment evaluator: in-scope answer vs. scope violation."""
        from phoenix.evals import ClassificationEvaluator

        from ._llm import get_judge_llm

        evaluator = ClassificationEvaluator(
            name="check_answer_scope",
            llm=get_judge_llm(),
            prompt_template=template,
            choices={"within_scope": 1, "out_of_scope": 0},
        )
        result = evaluator.evaluate(
            {
                "input": _text(input, "query"),
                "output": _text(output),
            }
        )
        return result[0]

    return check_answer_scope


def _text(value, key: str | None = None) -> str:
    if isinstance(value, dict):
        if key and key in value:
            return str(value[key])
        return str(next(iter(value.values()), ""))
    return str(value)
