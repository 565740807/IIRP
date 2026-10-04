"""Compact AI input contract rendered from the same Pydantic JSON schema as preview."""

import json


def _describe(value: dict, definitions: dict) -> str:
    if "$ref" in value:
        return _describe(definitions[value["$ref"].split("/")[-1]], definitions)
    if "anyOf" in value:
        return " | ".join(dict.fromkeys(_describe(item, definitions) for item in value["anyOf"]))
    if "enum" in value:
        return " / ".join(map(str, value["enum"]))
    if "const" in value:
        return str(value["const"])
    kind = value.get("type", "object")
    if kind == "array":
        return f"array<{_describe(value.get('items', {}), definitions)}>"
    return {"null": "null", "integer": "integer", "number": "number", "boolean": "boolean", "string": "string"}.get(kind, kind)


def schema_field_lines(schema: dict) -> list[str]:
    """Walk actual properties, including nested objects and array items."""
    definitions = schema.get("$defs", {})
    result: list[str] = []

    def visit(value: dict, prefix: str = "") -> None:
        if "$ref" in value:
            value = definitions[value["$ref"].split("/")[-1]]
        if value.get("type") == "array" and "items" in value:
            visit(value["items"], prefix + "[]")
            return
        properties = value.get("properties")
        if not properties:
            return
        required = set(value.get("required", []))
        for name, field in properties.items():
            path = f"{prefix}.{name}" if prefix else name
            rule = "required" if name in required else "optional"
            default = f"; default={json.dumps(field['default'], ensure_ascii=False)}" if "default" in field else ""
            result.append(f"{path}: {_describe(field, definitions)} ({rule}{default})")
            visit(field, path)

    visit(schema)
    return result


def field_contract(schema: dict) -> str:
    return "\n".join(schema_field_lines(schema))
