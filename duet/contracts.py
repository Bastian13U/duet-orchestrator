"""Small closed JSON contracts. Remote output remains untrusted."""
from __future__ import annotations

import json


def obj(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
PLAN = obj({"tasks": {"type": "array", "items": obj({
    "agent": {"type": "string", "enum": ["openai", "anthropic"]},
    "instruction": STRING})}})
CHANGE = obj({"action": {"type": "string", "enum": ["changes", "delegate"]},
              "message": STRING,
              "changes": {"type": "array", "items": obj({"path": STRING, "content": STRING})}})
REVIEW = obj({"approved": {"type": "boolean"}, "feedback": STRING})


class ContractError(ValueError):
    pass


def validate(value, schema):
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["properties"]):
            raise ContractError("Invalid object fields")
        for key, sub in schema["properties"].items():
            validate(value[key], sub)
    elif kind == "array":
        if not isinstance(value, list) or len(value) > 64:
            raise ContractError("Invalid array")
        for item in value:
            validate(item, schema["items"])
    elif kind == "string":
        if not isinstance(value, str) or len(value.encode("utf-8")) > 262144:
            raise ContractError("Invalid string")
    elif kind == "boolean" and type(value) is not bool:
        raise ContractError("Invalid boolean")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError("Invalid enum")


def parse(text, schema):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("Duplicate JSON field")
            result[key] = value
        return result
    try:
        result = json.loads(text, object_pairs_hook=unique)
    except (ValueError, TypeError) as error:
        raise ContractError("Invalid JSON response") from error
    validate(result, schema)
    return result
