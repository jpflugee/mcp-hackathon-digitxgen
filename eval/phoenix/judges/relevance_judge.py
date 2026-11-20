"""Scope judge — did the agent stay inside the weather-tools domain?"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

TEMPLATE = """You are reviewing the answer of an AI agent whose ONLY capability
is weather data for U.S. GSA federal buildings:
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

[BEGIN DATA]
Question: {input}
Agent answer: {output}
[END DATA]

Respond with exactly one label: "within_scope" or "out_of_scope".
"""


def check_answer_scope(input, output):
    """Phoenix experiment evaluator: in-scope answer vs. scope violation."""
    from phoenix.evals import ClassificationEvaluator

    from ._llm import get_judge_llm

    evaluator = ClassificationEvaluator(
        name="check_answer_scope",
        llm=get_judge_llm(),
        prompt_template=TEMPLATE,
        choices={"within_scope": 1, "out_of_scope": 0},
    )
    result = evaluator.evaluate(
        {
            "input": _text(input, "query"),
            "output": _text(output),
        }
    )
    return result[0]


def _text(value, key: str | None = None) -> str:
    if isinstance(value, dict):
        if key and key in value:
            return str(value[key])
        return str(next(iter(value.values()), ""))
    return str(value)
