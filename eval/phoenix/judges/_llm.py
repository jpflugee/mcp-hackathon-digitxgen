"""Shared LLM wrapper for the LLM-as-judge evaluators.

The judges talk to the USAi gateway through Phoenix's OpenAI-compatible LLM
wrapper. ``JUDGE_MODEL`` is kept separate from ``AGENT_MODEL`` so you can judge
with a stronger/different model than the agent-under-test.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def get_judge_llm():
    from phoenix.evals.llm import LLM

    base = os.getenv("USAI_BASE_URL", "").rstrip("/")
    if base and not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    # The OpenAI-compatible provider reads these env vars.
    os.environ.setdefault("OPENAI_API_KEY", os.getenv("USAI_API_KEY", ""))
    if base:
        os.environ.setdefault("OPENAI_BASE_URL", base)

    return LLM(
        provider="openai",
        model=os.getenv("JUDGE_MODEL", "claude_4_6_sonnet"),
    )
