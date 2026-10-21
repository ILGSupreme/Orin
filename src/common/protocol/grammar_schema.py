"""Project response contracts into compact native JSON grammars."""


def grammar_schema(schema: dict) -> dict:
    """Retain structure/enums while leaving repetition limits to validation.

    llama.cpp converters can expand large bounded strings/arrays into thousands
    of rules. Output tokens are bounded independently; the original Pydantic
    model still validates lengths, item counts and semantic invariants afterward.
    """
    repetition_bounds = {'minLength', 'maxLength', 'minItems', 'maxItems'}

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in repetition_bounds:
                continue
            if key in {'properties', '$defs', 'definitions'}:
                result[key] = {name: visit(child) for name, child in item.items()}
            else:
                result[key] = visit(item)
        return result

    return visit(schema)
