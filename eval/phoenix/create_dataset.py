"""CLI: validate a CSV dataset and upload it to Phoenix.

Usage (from eval/phoenix/):
    uv run create_dataset.py --list
    uv run create_dataset.py --dataset-name gsa-weather-eval-0 --dry-run
    uv run create_dataset.py --dataset-name gsa-weather-eval-0
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(HERE, "datasets.yaml")


def load_registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["datasets"]


def load_and_validate(entry: dict, csv_override: str | None = None) -> pd.DataFrame:
    csv_path = csv_override or os.path.join(HERE, entry["csv_path"])
    if not os.path.exists(csv_path):
        sys.exit(f"CSV not found: {csv_path}")
    df = pd.read_csv(csv_path)
    required = list(entry["input_keys"]) + list(entry["output_keys"])
    missing = [c for c in required if c not in df.columns]
    if missing:
        sys.exit(f"CSV {csv_path} is missing required column(s): {missing}")
    empty = [c for c in required if df[c].isna().any()]
    if empty:
        sys.exit(f"CSV {csv_path} has empty values in column(s): {empty}")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a CSV dataset to Phoenix")
    parser.add_argument("--list", action="store_true", help="List registered datasets")
    parser.add_argument("--dataset-name", help="Dataset name from datasets.yaml")
    parser.add_argument(
        "--phoenix-name", help="Name to use in Phoenix (default: --dataset-name)"
    )
    parser.add_argument("--csv-path", help="Override the registered CSV path")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate the CSV without uploading"
    )
    args = parser.parse_args()

    registry = load_registry()

    if args.list:
        for name, entry in registry.items():
            print(f"{name}: {entry['csv_path']}\n    {entry.get('description', '')}")
        return

    if not args.dataset_name:
        parser.error("--dataset-name is required (or use --list)")
    if args.dataset_name not in registry:
        sys.exit(
            f"Unknown dataset '{args.dataset_name}'. "
            f"Registered: {', '.join(registry)}"
        )

    entry = registry[args.dataset_name]
    df = load_and_validate(entry, args.csv_path)
    print(f"Loaded {len(df)} examples from {args.csv_path or entry['csv_path']}")

    if args.dry_run:
        print("Dry run: validation passed, nothing uploaded.")
        return

    from phoenix.client import Client

    client = Client()
    dataset = client.datasets.create_dataset(
        name=args.phoenix_name or args.dataset_name,
        dataframe=df,
        input_keys=list(entry["input_keys"]),
        output_keys=list(entry["output_keys"]),
    )
    print(f"Uploaded dataset '{args.phoenix_name or args.dataset_name}' to Phoenix.")
    print(dataset)


if __name__ == "__main__":
    main()
