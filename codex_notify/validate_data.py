from __future__ import annotations

import argparse
from pathlib import Path

from .models import AppState, Settings


def validate_data(root: Path) -> None:
    Settings.model_validate_json((root / "settings.json").read_bytes())
    AppState.model_validate_json((root / "state.json").read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/app/data")
    arguments = parser.parse_args()
    root = Path(arguments.data_dir)
    validate_data(root)
    print("JSON data is valid")


if __name__ == "__main__":
    main()
