"""create_dataset.py — upload a GSA eval CSV dataset to Arize Phoenix.

Usage:
    # List registered datasets
    uv run create_dataset.py --list

    # Validate CSV without uploading (dry-run)
    uv run create_dataset.py --dataset-name gsa-eval-0 --dry-run

    # Upload to Phoenix
    uv run create_dataset.py --dataset-name gsa-eval-0

    # Upload with a custom Phoenix dataset name
    uv run create_dataset.py --dataset-name gsa-eval-0 --phoenix-name my-gsa-eval

    # Override CSV path
    uv run create_dataset.py --dataset-name gsa-eval-0 --csv-path /tmp/custom.csv

Run from eval/phoenix/:
    cd eval/phoenix && uv run create_dataset.py --dataset-name gsa-eval-0
"""
import argparse
import os
import pathlib
import sys

import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv()

_HERE = pathlib.Path(__file__).parent
_DATASETS_YAML = _HERE / "datasets.yaml"
_PHOENIX_ENDPOINT = os.getenv("PHOENIX_HOST", "http://localhost:6006")


def _load_registry() -> dict:
    with open(_DATASETS_YAML) as f:
        return yaml.safe_load(f).get("datasets", {})


def _resolve_csv(registry: dict, dataset_name: str, csv_override: str | None) -> pathlib.Path:
    if csv_override:
        return pathlib.Path(csv_override)
    if dataset_name not in registry:
        print(f"ERROR: dataset '{dataset_name}' not found in datasets.yaml.", file=sys.stderr)
        sys.exit(1)
    return _HERE / registry[dataset_name]["csv_path"]


def _validate(csv_path: pathlib.Path, registry: dict, dataset_name: str) -> pd.DataFrame:
    if not csv_path.exists():
        print(f"ERROR: CSV not found at {csv_path}", file=sys.stderr)
        sys.exit(1)

    df = pd.read_csv(csv_path)
    entry = registry.get(dataset_name, {})
    required_cols = (entry.get("input_keys", []) + entry.get("output_keys", []))

    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        print(
            f"ERROR: CSV is missing required columns: {missing}\n"
            f"Available columns: {list(df.columns)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"✓ CSV validated: {csv_path} ({len(df)} rows, columns: {list(df.columns)})")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a GSA eval CSV dataset to Arize Phoenix."
    )
    parser.add_argument("--list", action="store_true", help="List registered datasets and exit.")
    parser.add_argument("--dataset-name", help="Key in datasets.yaml to upload.")
    parser.add_argument(
        "--phoenix-name",
        default=None,
        help="Dataset name in Phoenix (defaults to --dataset-name).",
    )
    parser.add_argument("--csv-path", default=None, help="Override CSV path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate CSV without uploading to Phoenix.",
    )
    parser.add_argument(
        "--phoenix-endpoint",
        default=_PHOENIX_ENDPOINT,
        help=f"Phoenix host URL (default: {_PHOENIX_ENDPOINT}).",
    )
    args = parser.parse_args()

    registry = _load_registry()

    if args.list:
        print("Registered datasets:")
        for name, meta in registry.items():
            print(f"  {name}: {meta.get('description', '')}")
        return

    if not args.dataset_name:
        parser.error("--dataset-name is required (or use --list).")

    csv_path = _resolve_csv(registry, args.dataset_name, args.csv_path)
    df = _validate(csv_path, registry, args.dataset_name)

    if args.dry_run:
        print("Dry-run complete — no data uploaded.")
        return

    # ── Upload to Phoenix ─────────────────────────────────────────────────────
    from phoenix.client import Client  # noqa: PLC0415

    phoenix_name = args.phoenix_name or args.dataset_name
    client = Client(base_url=args.phoenix_endpoint)

    dataset = client.datasets.create_dataset(
        name=phoenix_name,
        dataframe=df,
        input_keys=registry.get(args.dataset_name, {}).get("input_keys", ["query"]),
        output_keys=registry.get(args.dataset_name, {}).get("output_keys", ["responses"]),
    )
    print(f"✓ Dataset uploaded to Phoenix as '{phoenix_name}' (id={dataset.id})")


if __name__ == "__main__":
    main()
