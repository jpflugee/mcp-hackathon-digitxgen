"""Relevance / scope judge — checks whether the agent answered within scope.

Returns "within_scope" if the agent answered an in-scope question correctly
or appropriately declined an out-of-scope question. Returns "out_of_scope"
if the agent answered a question outside its domain or hallucinated tools.
"""
import os

from dotenv import load_dotenv
from phoenix.evals import ClassificationEvaluator, LLM

load_dotenv()

_TEMPLATE = """You are evaluating an AI assistant that answers questions about
GSA (General Services Administration) federal property locations, active weather
and hazard alerts, current weather conditions, and ServiceNow weather-alert
record management.

IN SCOPE for this assistant:
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

Evaluate the assistant's response to the question below.
- If the question is in scope and the assistant answered it (even partially), return "within_scope".
- If the question is out of scope and the assistant correctly declined, return "within_scope".
- If the assistant answered an out-of-scope question with fabricated information, return "out_of_scope".
- If the assistant failed to use its tools and guessed instead, return "out_of_scope".

User question:
{input}

Assistant's answer:
{output}

Respond with exactly one phrase — either "within_scope" or "out_of_scope" — with
no other text.
"""

_CHOICES = ["within_scope", "out_of_scope"]


def check_answer_scope(input: dict, output: dict) -> str:
    """Score whether the agent stayed within its defined scope.

    Args:
        input:  Phoenix input dict — must contain key "query".
        output: Phoenix output dict — must contain key "response".

    Returns:
        "within_scope" or "out_of_scope"
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
        name="scope",
        llm=llm,
        prompt_template=_TEMPLATE,
        choices=_CHOICES,
    )

    scores = evaluator.evaluate({
        "input": input.get("query", ""),
        "output": output.get("response", ""),
    })
    return scores[0].label if scores else "out_of_scope"
