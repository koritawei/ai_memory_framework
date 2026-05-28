""" 验收：JSON Schema 校验。"""

from __future__ import annotations

import pytest

from memory_app.config_center.base import ConfigValidationError
from memory_app.config_center.schema import fill_defaults, validate_params


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["k"],
    "properties": {
        "k": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 60},
        "weight": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
    },
}


def test_validate_passes_on_valid():
    out = validate_params({"k": 60, "weight": 0.7}, SCHEMA)
    assert out == {"k": 60, "weight": 0.7}


def test_validate_missing_required():
    with pytest.raises(ConfigValidationError) as exc:
        validate_params({"weight": 0.5}, SCHEMA)
    assert "k" in str(exc.value)


def test_validate_type_error():
    with pytest.raises(ConfigValidationError):
        validate_params({"k": "not-an-int"}, SCHEMA)


def test_validate_out_of_range():
    with pytest.raises(ConfigValidationError):
        validate_params({"k": 9999}, SCHEMA)


def test_validate_unknown_field_with_additional_false():
    with pytest.raises(ConfigValidationError):
        validate_params({"k": 60, "extra": 1}, SCHEMA)


def test_fill_defaults_top_level():
    filled = fill_defaults(SCHEMA, {"k": 80})
    assert filled["k"] == 80
    assert filled["weight"] == 0.5


def test_validate_skips_when_no_schema():
    assert validate_params({"anything": "ok"}, None) == {"anything": "ok"}
