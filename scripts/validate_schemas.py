#!/usr/bin/env python3
"""Validate the JSON Schemas in schemas/ and their examples.

Checks that every schema is a valid Draft 2020-12 schema, that every file in
schemas/examples/ validates against its schema, and that every file in
schemas/examples/invalid/ is rejected.

Requires: jsonschema>=4.18, PyYAML.
Usage: python3 scripts/validate_schemas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
EXAMPLES = SCHEMAS / "examples"

# Example filename prefix -> schema file.
SCHEMA_BY_PREFIX = {
    "project": "project.schema.json",
    "capability": "capability.schema.json",
    "task": "task.schema.json",
    "manifest": "manifest.schema.json",
}


def load(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def validator_for(example: Path) -> Draft202012Validator:
    prefix = example.name.split("-", 1)[0].split(".", 1)[0]
    schema_name = SCHEMA_BY_PREFIX.get(prefix)
    if schema_name is None:
        raise SystemExit(f"No schema mapped for example {example.relative_to(ROOT)}")
    schema = load(SCHEMAS / schema_name)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def main() -> int:
    failures: list[str] = []

    for schema_path in sorted(SCHEMAS.glob("*.schema.json")):
        try:
            Draft202012Validator.check_schema(load(schema_path))
            print(f"ok      schema   {schema_path.relative_to(ROOT)}")
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures.append(f"invalid schema {schema_path.name}: {exc}")

    for example in sorted(p for p in EXAMPLES.iterdir() if p.is_file()):
        errors = list(validator_for(example).iter_errors(load(example)))
        if errors:
            for error in errors:
                location = "/".join(str(part) for part in error.absolute_path) or "<root>"
                failures.append(f"{example.relative_to(ROOT)} at {location}: {error.message}")
        else:
            print(f"ok      valid    {example.relative_to(ROOT)}")

    for example in sorted((EXAMPLES / "invalid").iterdir()):
        errors = list(validator_for(example).iter_errors(load(example)))
        if errors:
            print(f"ok      rejected {example.relative_to(ROOT)} ({errors[0].message[:70]})")
        else:
            failures.append(f"{example.relative_to(ROOT)} was accepted but must be rejected")

    for failure in failures:
        print(f"FAIL    {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
