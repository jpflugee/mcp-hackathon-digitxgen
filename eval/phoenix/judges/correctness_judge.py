"""Correctness judge — compares the agent's answer to a ground-truth reference.

Returns "correct" if the agent's answer matches the expected response within
reasonable formatting and phrasing tolerance, "incorrect" otherwise.
"""
import os

from dotenv import load_dotenv
from phoenix.evals import ClassificationEvaluator, LLM

load_dotenv()

_TEMPLATE = """You are evaluating an AI assistant that answers questions about
GSA (General Services Administration) federal property locations, weather
conditions, hazard alerts, and ServiceNow record management.

Compare the assistant's answer to the reference answer below. The assistant's
answer is CORRECT if it conveys the same factual information as the reference,
even if the wording, units, or formatting differ (e.g. "1,234" vs "1234",
"°C" vs "degrees Celsius", extra surrounding context). It is INCORRECT if it
contradicts the reference, omits a key fact, or fabricates information not
present in the reference.

User question:
{input}

Reference answer (ground truth):
{reference}

Assistant's answer:
{output}

Respond with exactly one word — either "correct" or "incorrect" — with no
other text.
"""

_CHOICES = {"correct": 1, "incorrect": 0}


def match_expected_response(input: dict, output: dict, expected: dict) -> str:
    """Score an agent response against a ground-truth reference.

    Args:
        input:    Phoenix input dict — must contain key "query".
        output:   Phoenix output dict — must contain key "response".
        expected: Phoenix expected dict — must contain key "responses".

    Returns:
        "correct" or "incorrect"
    """
    judge_model = os.getenv("JUDGE_MODEL", "claude_4_6_sonnet")
    api_key = os.getenv("USAI_API_KEY", "")
    base_url = os.getenv("USAI_BASE_URL", "")

    llm = LLM(
        provider="openai",
        model=judge_model,
        api_key=api_key,
        base_url=base_url.rstrip('/') if base_url else None,
    )

    evaluator = ClassificationEvaluator(
        name="correctness",
        llm=llm,
        prompt_template=_TEMPLATE,
        choices=list(_CHOICES.keys()),
    )

    scores = evaluator.evaluate({
        "input": input.get("query", ""),
        "output": output.get("response", ""),
        "reference": expected.get("responses", ""),
    })
    return scores[0].label if scores else "incorrect"
