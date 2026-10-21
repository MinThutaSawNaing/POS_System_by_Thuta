"""Deterministic guards for model-produced tool calls.

The model only learns tool names from the schemas and prompt text we send it,
so nothing stops it from inventing a plausible API (for example
``get_sales_forecast``) or from filling arguments that no tool accepts. Both
failures used to end the turn with a raw error string, or worse, with a
plausible-sounding answer the database never produced.

This module validates a tool call against the functions that are actually
executable for the current turn *before* any database code runs. A rejection is
phrased as an instruction the model can act on in one bounded retry: the real
tool names, a close-match suggestion, and the exact reason the call failed.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

# Keep rejection text small enough to travel inside a tool-result message.
MAX_LISTED_TOOLS = 25


@dataclass(frozen=True)
class GuardedToolCall:
    """One validated tool call: cleaned arguments, or a model-facing error."""

    name: str
    arguments: Dict[str, Any]
    error: Optional[str] = None
    dropped_arguments: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None


def schema_parameters(
    schemas: Optional[Iterable[Mapping[str, Any]]]
) -> Dict[str, Dict[str, Any]]:
    """Map tool name -> JSON parameter schema from OpenAI-style tool schemas."""
    parameters: Dict[str, Dict[str, Any]] = {}
    for schema in schemas or ():
        if not isinstance(schema, Mapping):
            continue
        function = schema.get("function")
        if not isinstance(function, Mapping):
            function = schema
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = function.get("parameters")
        parameters[name] = dict(params) if isinstance(params, Mapping) else {}
    return parameters


def tool_names_from_schemas(
    schemas: Optional[Iterable[Mapping[str, Any]]]
) -> frozenset:
    """Names of the tools that were offered to the model for this turn."""
    return frozenset(schema_parameters(schemas))


def suggest_tool_name(name: str, available: Iterable[str]) -> str:
    """Return the closest registered tool name, or an empty string."""
    matches = difflib.get_close_matches(str(name or ""), list(available), n=1, cutoff=0.6)
    return matches[0] if matches else ""


def _tool_list(available: Sequence[str]) -> str:
    ordered = sorted(name for name in available if name)
    if not ordered:
        return "none"
    shown = ordered[:MAX_LISTED_TOOLS]
    suffix = ", ..." if len(ordered) > len(shown) else ""
    return ", ".join(shown) + suffix


def guard_tool_call(
    name: Any,
    arguments: Any,
    available: Iterable[str],
    parameters: Optional[Mapping[str, Mapping[str, Any]]] = None,
    parse_error: Optional[str] = None,
) -> GuardedToolCall:
    """Validate one tool call against the executable tool names.

    * An unknown tool name is rejected with the real catalog and a close-match
      suggestion, never executed.
    * Unknown arguments are rejected, never silently removed: removing a scope
      parameter could turn an all-time request into a default 30-day query.
    * Required arguments, JSON types, enums and declared numeric bounds are
      checked before either executor can invoke the database tool.
    """
    names = [str(item) for item in available or () if item]
    if not isinstance(name, str) or not name.strip():
        return GuardedToolCall("", {}, (
            "The tool call had no name, so nothing was executed. Call one of the "
            f"registered tools: {_tool_list(names)}."))
    name = name.strip()
    if name not in names:
        suggestion = suggest_tool_name(name, names)
        hint = f" Did you mean '{suggestion}'?" if suggestion else ""
        return GuardedToolCall(name, {}, (
            f"Unknown tool '{name}'.{hint} This POS system has no such API, so the "
            f"call was not executed. Registered tools: {_tool_list(names)}. Call one "
            "of them with its documented arguments, or answer in plain text without "
            "a tool call."))

    if parse_error:
        return GuardedToolCall(name, {}, (
            f"Arguments for '{name}' were not valid JSON ({parse_error}), so the call "
            f"was not executed. Call '{name}' again with a JSON object that matches its "
            "documented parameters."))

    if arguments is None:
        arguments = {}
    if isinstance(arguments, (str, bytes, bytearray)):
        try:
            arguments = json.loads(arguments or "{}")
        except (TypeError, ValueError):
            return GuardedToolCall(name, {}, (
                f"Arguments for '{name}' were not valid JSON. Call '{name}' again "
                "with a JSON object that matches its parameters."))
    if not isinstance(arguments, Mapping):
        return GuardedToolCall(name, {}, (
            f"Arguments for '{name}' must be a JSON object, received "
            f"{type(arguments).__name__}."))

    cleaned = dict(arguments)
    dropped: Tuple[str, ...] = ()
    schema = (parameters or {}).get(name)
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            allowed = set(properties)
            dropped = tuple(sorted(str(key) for key in cleaned if key not in allowed))
            if dropped:
                return GuardedToolCall(name, {}, (
                    f"Undocumented arguments for '{name}': {', '.join(dropped)}. "
                    f"Allowed arguments: {', '.join(sorted(allowed)) or 'none'}. "
                    "Nothing was executed; retry with documented arguments."))
            missing = [key for key in schema.get('required', ()) if key not in cleaned]
            if missing:
                return GuardedToolCall(name, {}, (
                    f"Missing required arguments for '{name}': {', '.join(missing)}. "
                    "Nothing was executed."))
            for key, value in cleaned.items():
                spec = properties[key]
                kind = spec.get('type')
                types = kind if isinstance(kind, list) else [kind]
                matches = {
                    'integer': type(value) is int,
                    'number': type(value) in (int, float),
                    'boolean': type(value) is bool,
                    'string': isinstance(value, str),
                    'object': isinstance(value, Mapping),
                    'array': isinstance(value, list),
                    'null': value is None,
                }
                error = None
                if kind and not any(matches.get(t, True) for t in types):
                    error = f"must have JSON type {kind}"
                elif 'enum' in spec and value not in spec['enum']:
                    error = f"must be one of {spec['enum']}"
                elif type(value) in (int, float):
                    if 'minimum' in spec and value < spec['minimum']:
                        error = f"must be at least {spec['minimum']}"
                    elif 'maximum' in spec and value > spec['maximum']:
                        error = f"must be at most {spec['maximum']}"
                if error:
                    return GuardedToolCall(name, {}, (
                        f"Invalid argument '{key}' for '{name}': {error}. "
                        "Nothing was executed; retry with documented arguments."))
    return GuardedToolCall(name, cleaned, None, dropped)
