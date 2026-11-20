"""run_experiment.py — run the GSA JDP agent against a Phoenix dataset.

Loads a dataset from Phoenix, drives the GSAJDPAgent against every question,
scores each answer with LLM-as-judge evaluators, and logs all traces to the
Phoenix UI.

Usage:
    # Run with default dataset and system prompt v1
    uv run run_experiment.py --dataset-name gsa-eval-0

    # Use system prompt v2
    uv run run_experiment.py --dataset-name gsa-eval-0 --system-prompt-version v2

    # Custom project name and Phoenix endpoint
    uv run run_experiment.py --dataset-name gsa-eval-0 \\
        --project-name my-run \\
        --phoenix-endpoint http://localhost:6006

    # Skip dataset version validation (use latest)
    uv run run_experiment.py --dataset-name gsa-eval-0 --no-validate

Run from eval/phoenix/:
    cd eval/phoenix && uv run run_experiment.py --dataset-name gsa-eval-0
"""
import argparse
import os
import pathlib
import sys

import yaml
from dotenv import load_dotenv

load_dotenv()

_HERE = pathlib.Path(__file__).parent
_DATASETS_YAML = _HERE / "datasets.yaml"
_PHOENIX_HOST = os.getenv("PHOENIX_HOST", "http://localhost:6006")
_PROJECT_NAME = os.getenv("PHOENIX_PROJECT_NAME", "gsa-jdp-toolkit-eval")


def _load_registry() -> dict:
    with open(_DATASETS_YAML) as f:
        return yaml.safe_load(f).get("datasets", {})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the GSA JDP agent eval against a Phoenix dataset."
    )
    parser.add_argument(
        "--dataset-name",
        required=True,
        help="Dataset name in Phoenix to evaluate against.",
    )
    parser.add_argument(
        "--system-prompt-version",
        default="v1",
        choices=["v1", "v2"],
        help="System prompt version to use (default: v1).",
    )
    parser.add_argument(
        "--project-name",
        default=_PROJECT_NAME,
        help=f"Phoenix project name (default: {_PROJECT_NAME}).",
    )
    parser.add_argument(
        "--phoenix-endpoint",
        default=_PHOENIX_HOST,
        help=f"Phoenix host URL (default: {_PHOENIX_HOST}).",
    )
    parser.add_argument(
        "--experiment-description",
        default=None,
        help="Optional human-readable description for this experiment run.",
    )
    parser.add_argument(
        "--dataset-version-id",
        default=None,
        help="Specific dataset version ID to pin the experiment to.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip pre-flight validation of the dataset CSV.",
    )
    args = parser.parse_args()

    registry = _load_registry()

    # ── Pre-flight: validate the source CSV exists ────────────────────────────
    if not args.no_validate and args.dataset_name in registry:
        csv_path = _HERE / registry[args.dataset_name]["csv_path"]
        if not csv_path.exists():
            print(
                f"ERROR: source CSV not found at {csv_path}.\n"
                "Run create_dataset.py first, or use --no-validate to skip.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"✓ Source CSV found: {csv_path}")

    # ── Phoenix client + dataset ──────────────────────────────────────────────
    from phoenix.client import Client  # noqa: PLC0415

    ph_client = Client(base_url=args.phoenix_endpoint)

    dataset_kwargs: dict = {"dataset": args.dataset_name}
    if args.dataset_version_id:
        dataset_kwargs["version_id"] = args.dataset_version_id

    dataset = ph_client.datasets.get_dataset(**dataset_kwargs)
    print(
        f"✓ Loaded dataset '{args.dataset_name}' "
        f"({dataset.example_count} examples)"
    )

    # ── Agent + judges ────────────────────────────────────────────────────────
    # Import here so missing deps surface as a clear error message.
    try:
        from agent import GSAJDPAgent  # noqa: PLC0415
        from judges import check_answer_scope, match_expected_response  # noqa: PLC0415
    except ImportError as exc:
        print(
            f"ERROR: Could not import agent or judges: {exc}\n"
            "Make sure you ran `uv sync --group dev` and are in eval/phoenix/.",
            file=sys.stderr,
        )
        sys.exit(1)

    agent = GSAJDPAgent(
        system_prompt_version=args.system_prompt_version,
        phoenix_endpoint=f"{args.phoenix_endpoint.rstrip('/')}/v1/traces",
        project_name=args.project_name,
    )
    task = agent.create_experiment_task()

    description = args.experiment_description or (
        f"system_prompt={args.system_prompt_version} "
        f"agent_model={os.getenv('AGENT_MODEL', 'gpt-4o')}"
    )

    # ── Run experiment ────────────────────────────────────────────────────────
    print(
        f"Starting experiment on dataset '{args.dataset_name}' "
        f"(prompt={args.system_prompt_version}, project={args.project_name}) …"
    )

    results = ph_client.experiments.run_experiment(
        dataset=dataset,
        task=task,
        evaluators=[check_answer_scope, match_expected_response],
        experiment_name=f"{args.dataset_name}-{args.system_prompt_version}",
        experiment_description=description,
    )

    print(f"\n✓ Experiment complete. Results in Phoenix UI: {args.phoenix_endpoint}")
    print(results)


if __name__ == "__main__":
    main()
