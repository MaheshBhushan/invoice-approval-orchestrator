import datetime
import json
import pathlib
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "contracts/v1/openapi.json").read_text())
BASELINE = json.loads((ROOT / "contracts/compatibility/v1-baseline.json").read_text())


def validate(schema, value, label):
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    valid = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if kind and not any(valid[t](value) for t in kinds):
        raise AssertionError(f"{label}: expected {kind}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise AssertionError(f"{label}: {value!r} is outside enum")
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - schema.get("properties", {}).keys()
        if missing or (schema.get("additionalProperties") is False and extra):
            raise AssertionError(f"{label}: missing={sorted(missing)}, extra={sorted(extra)}")
        for key, item in value.items():
            child = schema.get("properties", {}).get(key)
            if child:
                validate(child, item, f"{label}.{key}")
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate(schema["items"], item, f"{label}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise AssertionError(f"{label}: string length outside contract")
        if schema.get("format") == "date":
            datetime.date.fromisoformat(value)
        if schema.get("format") == "uuid":
            uuid.UUID(value)
    if isinstance(value, (int, float)) and value < schema.get("minimum", float("-inf")):
        raise AssertionError(f"{label}: below minimum")


def assert_backward_compatible(old, new, label, request):
    assert old.get("additionalProperties", True) == new.get("additionalProperties", True), label
    if request:
        assert old["required"] == new["required"], label
    else:
        assert set(old["required"]) <= set(new["required"]), label
    for name, old_schema in old["properties"].items():
        if name not in new["properties"] or new["properties"][name] != old_schema:
            raise AssertionError(f"{label}.{name} changed or was removed")


class SharedContractTests(unittest.TestCase):
    def test_versioned_spec_and_operations(self):
        self.assertEqual(CONTRACT["info"]["version"], "1.0.0")
        operations = {
            (path, method): operation["operationId"]
            for path, methods in CONTRACT["paths"].items()
            for method, operation in methods.items()
        }
        self.assertEqual(
            operations,
            {
                ("/invoices", "post"): "submitInvoice",
                ("/v2/user-tasks/{userTaskKey}/completion", "post"): "completeHumanReview",
            },
        )

    def test_consumer_fixtures_validate_independently(self):
        schemas = CONTRACT["components"]["schemas"]
        fixtures = sorted((ROOT / "contracts/consumers").glob("*.json"))
        self.assertEqual(len(fixtures), 2)
        for fixture_path in fixtures:
            fixture = json.loads(fixture_path.read_text())
            self.assertTrue(fixture["consumer"])
            for operation, body in fixture["requests"].items():
                name = "InvoiceIn" if operation == "submitInvoice" else "HumanReviewCompletion"
                validate(schemas[name], body, f"{fixture['consumer']}.{operation}")

    def test_v1_schema_is_backward_compatible_with_frozen_baseline(self):
        schemas = CONTRACT["components"]["schemas"]
        for name, baseline in BASELINE.items():
            current = schemas[name]
            assert_backward_compatible(baseline, current, name, request=name != "InvoiceAccepted")
            if name == "HumanReviewCompletion":
                assert_backward_compatible(baseline["properties"]["variables"], current["properties"]["variables"], name + ".variables", request=True)

    def test_compatibility_gate_rejects_removed_or_tightened_fields(self):
        baseline = BASELINE["InvoiceIn"]
        for candidate in (
            {"required": baseline["required"][:-1], "properties": baseline["properties"]},
            {"required": baseline["required"] + ["optional_new"], "properties": baseline["properties"]},
            {"required": baseline["required"], "properties": {**baseline["properties"], "currency": {"type": "integer"}}},
        ):
            with self.assertRaises(AssertionError):
                assert_backward_compatible(baseline, candidate, "InvoiceIn", request=True)


if __name__ == "__main__":
    unittest.main()
