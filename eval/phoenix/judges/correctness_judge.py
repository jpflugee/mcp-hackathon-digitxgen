"""Correctness judge — compares the agent's answer to the gold answer."""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

TEMPLATE = """You are grading the answer of an AI agent that uses GSA MCP tools to
answer questions about GSA federal buildings (via the GSA IOLP building
dataset), their current, historical, and forecasted weather (Open-Meteo, NWS),
active hazard alerts, and ServiceNow weather-alert records.

Compare the agent's answer to the reference (gold) answer. The reference is
either the exact expected value or a description of what a correct answer must
contain (e.g. which tool to call and what the answer must state). Live data
changes, so when the reference describes a field the answer must report (a
temperature, a count, an address), any specific value consistent with that
description counts — you cannot verify live numbers, only that the agent
reported the requested kind of value rather than guessing or refusing.

Mark the answer "correct" if it contains the same essential fact(s) as the
reference, even when:
- units differ but are equivalent (e.g. 20°C vs 68°F), or extra units/symbols
  are added;
- formatting, capitalization, or punctuation differs (e.g. "DC0003" vs
  "dc0003", "Washington" vs "Washington, DC", "1,234" vs "1234");
- the agent adds surrounding prose, context, caveats, or explanation around
  the correct value;
- numbers are written differently but equal (e.g. "7" vs "seven").

Mark it "incorrect" only when the essential fact is wrong, missing, or
contradicts the reference. If the reference says the agent should refuse or
state the request is out of scope, the answer is "correct" when the agent
declines or redirects, and "incorrect" when it fabricates an answer anyway.

[BEGIN DATA]
Question: {input}
Reference answer: {reference}
Agent answer: {output}
[END DATA]

Respond with exactly one label: "correct" or "incorrect".
"""


def match_expected_response(input, output, expected):
    """Phoenix experiment evaluator: agent answer vs. ground truth."""
    from phoenix.evals import ClassificationEvaluator

    from ._llm import get_judge_llm

    evaluator = ClassificationEvaluator(
        name="match_expected_response",
        llm=get_judge_llm(),
        prompt_template=TEMPLATE,
        choices={"correct": 1, "incorrect": 0},
    )
    result = evaluator.evaluate(
        {
            "input": _text(input, "query"),
            "output": _text(output),
            "reference": _text(expected, "responses"),
        }
    )
    return result[0]


def _text(value, key: str | None = None) -> str:
    if isinstance(value, dict):
        if key and key in value:
            return str(value[key])
        return str(next(iter(value.values()), ""))
    return str(value)
