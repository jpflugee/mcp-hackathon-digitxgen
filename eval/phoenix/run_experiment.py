"""CLI: run an experiment (agent + judges) against a Phoenix dataset.

Usage (from eval/phoenix/, with Phoenix running at :6006):
    uv run run_experiment.py --dataset-name gsa-weather-eval-0
    uv run run_experiment.py --dataset-name gsa-weather-eval-0 \
        --system-prompt-version v2
"""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

load_dotenv()

from agent import (  # noqa: E402
    DEFAULT_PHOENIX_ENDPOINT,
    DEFAULT_PROJECT_NAME,
    WeatherForecastAgent,
)
from judges import check_answer_scope, match_expected_response  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an eval experiment against the weather MCP server"
    )
    parser.add_argument("--dataset-name", required=True)
    parser.add_argument(
        "--system-prompt-version",
        default="v1",
        choices=["v1", "v2"],  # keep in sync with prompts/system_prompt_v*.txt
    )
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--phoenix-endpoint", default=DEFAULT_PHOENIX_ENDPOINT)
    parser.add_argument("--experiment-description", default="")
    parser.add_argument(
        "--dataset-version-id",
        default=None,
        help="Pin a specific Phoenix dataset version (default: latest)",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the pre-flight agent initialization check",
    )
    args = parser.parse_args()

    agent = WeatherForecastAgent(
        system_prompt_version=args.system_prompt_version,
        project_name=args.project_name,
        phoenix_endpoint=args.phoenix_endpoint,
    )

    if not args.no_validate:
        print("Pre-flight: launching MCP server + building agent...")
        asyncio.run(agent.initialize())
        print("Pre-flight OK.")

    from phoenix.client import Client

    client = Client(base_url=args.phoenix_endpoint)
    dataset = client.datasets.get_dataset(
        dataset=args.dataset_name,
        version_id=args.dataset_version_id,
    )

    description = args.experiment_description or (
        f"{args.dataset_name} | prompt {args.system_prompt_version}"
    )
    experiment = client.experiments.run_experiment(
        dataset=dataset,
        task=agent.create_experiment_task(),
        evaluators=[check_answer_scope, match_expected_response],
        experiment_name=f"{args.dataset_name}-{args.system_prompt_version}",
        experiment_description=description,
    )
    print("Experiment complete. Review results in the Phoenix UI "
          f"({args.phoenix_endpoint}).")
    print(experiment)


if __name__ == "__main__":
    main()
