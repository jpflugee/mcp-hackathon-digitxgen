"""CLI: run an experiment (agent + judges) against a Phoenix dataset.

The MCP server under test comes from the dataset's `target` in datasets.yaml
(override with --target).

Usage (from eval/phoenix/, with Phoenix running at :6006):
    uv run run_experiment.py --dataset-name gsa-eval-0
    uv run run_experiment.py --dataset-name gsa-weather-forecast-eval-0 \
        --system-prompt-version v2
"""

from __future__ import annotations

import argparse
import asyncio
import os

import yaml
from dotenv import load_dotenv

load_dotenv()

from agent import (  # noqa: E402
    DEFAULT_PHOENIX_ENDPOINT,
    DEFAULT_TARGET,
    TARGETS,
    MCPToolkitAgent,
)
from judges import make_scope_judge, match_expected_response  # noqa: E402

DATASETS_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datasets.yaml")


def _registry_target(dataset_name: str) -> str | None:
    with open(DATASETS_YAML, encoding="utf-8") as fh:
        entry = yaml.safe_load(fh)["datasets"].get(dataset_name, {})
    return entry.get("target")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an eval experiment against a GSA MCP server"
    )
    parser.add_argument("--dataset-name", required=True)
    parser.add_argument(
        "--target",
        choices=list(TARGETS),
        help="MCP server under test (default: the dataset's `target` in "
        f"datasets.yaml, else '{DEFAULT_TARGET}')",
    )
    parser.add_argument(
        "--system-prompt-version",
        default="v1",
        choices=["v1", "v2"],  # keep in sync with prompts/<target>/system_prompt_v*.txt
    )
    parser.add_argument(
        "--project-name", help="Phoenix project (default: per-target project name)"
    )
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

    target = args.target or _registry_target(args.dataset_name) or DEFAULT_TARGET

    agent = MCPToolkitAgent(
        target=target,
        system_prompt_version=args.system_prompt_version,
        project_name=args.project_name,
        phoenix_endpoint=args.phoenix_endpoint,
    )

    if not args.no_validate:
        print(f"Pre-flight ({target}): launching MCP server + building agent...")
        asyncio.run(agent.initialize())
        print("Pre-flight OK.")

    from phoenix.client import Client

    client = Client(base_url=args.phoenix_endpoint)
    dataset = client.datasets.get_dataset(
        dataset=args.dataset_name,
        version_id=args.dataset_version_id,
    )

    description = args.experiment_description or (
        f"{args.dataset_name} | target {target} | prompt {args.system_prompt_version}"
    )
    experiment = client.experiments.run_experiment(
        dataset=dataset,
        task=agent.create_experiment_task(),
        evaluators=[make_scope_judge(target), match_expected_response],
        experiment_name=f"{args.dataset_name}-{args.system_prompt_version}",
        experiment_description=description,
    )
    print("Experiment complete. Review results in the Phoenix UI "
          f"({args.phoenix_endpoint}).")
    print(experiment)


if __name__ == "__main__":
    main()
