#!/usr/bin/env python3
from pathlib import Path
import json
import shutil


root = Path(__file__).resolve().parents[1]
source = root / "site"
destination = root / "dist"

catalog = json.loads((source / "data" / "catalog.json").read_text(encoding="utf-8"))
if catalog.get("schema_version") != 1:
    raise SystemExit("site catalog has an unsupported schema version")
if destination.exists():
    shutil.rmtree(destination)
shutil.copytree(source, destination)
print(f"built {destination}")
