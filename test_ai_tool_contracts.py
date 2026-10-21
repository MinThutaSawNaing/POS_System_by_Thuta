"""Detect schema/signature/documentation drift without importing the Flask app."""

import inspect
from pathlib import Path

from ai_tools import AITools, TOOL_METADATA, TOOL_SCHEMAS
from generate_ai_tool_docs import render_reference


def test_all_registered_schemas_match_callable_signatures():
    assert set(TOOL_SCHEMAS) == set(TOOL_METADATA)
    for name, meta in TOOL_METADATA.items():
        signature = inspect.signature(getattr(AITools, name))
        args = {k: v for k, v in signature.parameters.items() if k != 'self'}
        schema = meta['parameters']
        assert set(schema.get('properties', {})) == set(args), name
        required = {k for k, v in args.items() if v.default is inspect.Parameter.empty}
        assert set(schema.get('required', [])) == required, name


def test_generated_reference_matches_executable_registry():
    reference = Path(__file__).resolve().parent / 'docs' / 'AI_TOOL_REFERENCE.md'
    assert reference.read_text(encoding='utf-8') == render_reference()