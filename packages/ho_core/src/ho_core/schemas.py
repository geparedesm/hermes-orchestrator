"""Loading and validating the JSON Schemas in the repository's schemas/ directory."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_DIR_ENV = "HO_SCHEMA_DIR"


class SchemaValidationError(ValueError):
    def __init__(self, schema: str, errors: list[str]) -> None:
        super().__init__(f"{schema}: " + "; ".join(errors))
        self.schema = schema
        self.errors = errors


def schema_dir() -> Path:
    """Locate schemas/: $HO_SCHEMA_DIR, else the nearest ancestor containing it."""
    configured = os.environ.get(SCHEMA_DIR_ENV)
    if configured:
        return Path(configured)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "schemas"
        if (candidate / "project.schema.json").is_file():
            return candidate
    raise FileNotFoundError(f"schemas/ not found; set {SCHEMA_DIR_ENV}")


@lru_cache(maxsize=None)
def validator(name: str) -> Draft202012Validator:
    """Return a validator for schemas/<name>.schema.json (for example 'project')."""
    schema = json.loads((schema_dir() / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def errors_for(name: str, instance: Any) -> list[str]:
    result = []
    for error in sorted(validator(name).iter_errors(instance), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in error.absolute_path) or "<root>"
        result.append(f"{location}: {error.message}")
    return result


def validate(name: str, instance: Any) -> None:
    errors = errors_for(name, instance)
    if errors:
        raise SchemaValidationError(name, errors)
