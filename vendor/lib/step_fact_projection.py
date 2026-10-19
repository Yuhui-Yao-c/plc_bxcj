"""Lossless StepLogicSlice projection for the StepGraph boundary.

This module deliberately has no PLCopen/SMC2 dependency: source adapters retain
their own facts, while the legacy StepGraph consumer gets compatible flat fields.
No Step occurrence is silently declared the controlling/owning rung.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def step_key(value: Any) -> tuple:
    """Logical state identity; source *occurrences* remain separately addressed."""
    obj = value.get("step_identity", value) if isinstance(value, dict) else getattr(value, "step_identity", {})
    return (
        obj.get("state_variable", ""),
        obj.get("station", ""),
        obj.get("thread_idx", 0),
        obj.get("step_value", obj.get("value", 0)),
    )


def fact_dict(fact: Any) -> dict:
    """Carry parameters, provenance and source paths instead of flattening facts."""
    if isinstance(fact, dict):
        return dict(fact)
    if callable(getattr(fact, "to_dict", None)):
        return fact.to_dict()
    names = (
        "fact_type", "variable", "negated", "callee", "timer_type", "preset",
        "target_step", "boolean_expr", "element_id", "network_id", "unit_name",
        "source_location", "evidence_path", "provenance", "inputs", "outputs",
    )
    return {k: getattr(fact, k) for k in names if hasattr(fact, k)}


def boolean_to_string(node: dict) -> str:
    """Render a Boolean AST without losing AND/OR precedence.

    Unknown expressions intentionally return the empty string; they are *not*
    assumed TRUE and must not be marked as verified by consumers.
    """
    if not isinstance(node, dict) or not node:
        return ""
    kind = node.get("expr_type", "")
    if kind == "unknown":
        return ""
    if kind == "atom":
        comparison = node.get("comparison")
        if comparison:
            operands = comparison.get("operands", [])
            body = f'{comparison.get("operator", "")}({", ".join(map(str, operands))})'
        else:
            body = node.get("variable", "")
        if not body:
            return ""
        return f"NOT ({body})" if node.get("negated") else body
    if kind == "not":
        inner = boolean_to_string(node.get("operand", {}))
        return f"NOT ({inner})" if inner else ""
    if kind in ("and", "or"):
        children = node.get("children", [])
        rendered = []
        for child in children:
            fragment = boolean_to_string(child)
            if not fragment:
                # An unresolved branch is not a removable term.
                return ""
            if isinstance(child, dict) and child.get("expr_type") in ("and", "or") and child.get("expr_type") != kind:
                fragment = f"({fragment})"
            rendered.append(fragment)
        if not rendered:
            return ""
        return f" {kind.upper()} ".join(rendered)
    return ""


def occurrence_reference(slice_obj: Any) -> dict:
    return {
        "source_file": getattr(slice_obj, "source_file", ""),
        "unit_name": getattr(slice_obj, "anchor_unit_name", ""),
        "network_id": getattr(slice_obj, "anchor_network_id", ""),
        "element_id": getattr(slice_obj, "anchor_element_id", ""),
        "command_count": len(getattr(slice_obj, "actions", []) or []),
        "call_count": len(getattr(slice_obj, "calls", []) or []),
        "transition_count": len(getattr(slice_obj, "transitions", []) or []),
    }


def choose_primary_slice(occurrences: list) -> tuple:
    """Choose a deterministic legacy projection, WITHOUT asserting ownership.

    All other slices must remain present in the raw output. The choice is a
    display/projection convenience, not validation of cross-network control.
    """
    if not occurrences:
        return None, "missing"
    if len(occurrences) == 1:
        return occurrences[0], "single_anchor"

    def rank(s):
        ident = s.step_identity
        matched = 0
        for t in getattr(s, "transitions", []) or []:
            target = getattr(t, "target_step", None) or {}
            # Only a *fully identified* target may vote on state ownership.
            if (target.get("state_variable") == ident.get("state_variable")
                    and target.get("thread") == ident.get("thread_idx")):
                matched += 1
        ref = occurrence_reference(s)
        return (
            matched,
            ref["command_count"] + ref["call_count"],
            ref["transition_count"],
            ref["source_file"],
            ref["unit_name"],
            ref["network_id"],
            ref["element_id"],
        )

    return max(occurrences, key=rank), "needs_owner_verification"


def slice_to_step_raw(s) -> dict:
    """Project a slice for legacy consumers, while preserving full fact records."""
    guards = []
    for g in getattr(s, "guards", []):
        label = ("NOT " if getattr(g, "negated", False) else "") + (getattr(g, "variable", "") or "")
        if label and label not in guards:
            guards.append(label)
    enables = []
    for f in getattr(s, "common_enables", []):
        label = ("NOT " if getattr(f, "negated", False) else "") + (getattr(f, "variable", "") or "")
        if label and label not in enables:
            enables.append(label)
    actions = list(dict.fromkeys(
        getattr(a, "variable", "") for a in getattr(s, "actions", []) if getattr(a, "variable", "")
    ))
    transitions = []
    for t in getattr(s, "transitions", []):
        target = getattr(t, "target_step", None) or {}
        cond_ast = getattr(t, "boolean_expr", None)
        trans = {
            "target_thread": target.get("thread", -1),
            "target_value": target.get("value", 0),
            "target_state_variable": target.get("state_variable"),
            "source": "topology_slice",
            "boolean_expr": cond_ast,
            "provenance": getattr(t, "provenance", "topology_only"),
            "element_id": getattr(t, "element_id", ""),
            "network_id": getattr(t, "network_id", ""),
            "evidence_path": getattr(t, "evidence_path", []),
        }
        rendered = boolean_to_string(cond_ast)
        if rendered:
            trans["condition"] = rendered
        transitions.append(trans)
    calls = []
    for f in getattr(s, "calls", []):
        d = fact_dict(f)
        d["name"] = d.get("callee", "")
        calls.append(d)
    return {
        "state_variable": s.step_identity.get("state_variable", ""),
        "station": s.step_identity.get("station", ""),
        "thread_idx": s.step_identity.get("thread_idx", 0),
        "step_value": s.step_identity.get("step_value", 0),
        "comment": getattr(s, "comment", "") or "",
        "actions": actions,
        "guards": guards,
        "common_enables": enables,
        "transitions": transitions,
        "calls": calls,
        "timers": [
            {"name": getattr(t, "timer_type", ""), "pt": getattr(t, "preset", ""),
             "fact": fact_dict(t)} for t in getattr(s, "timers", [])
        ],
        "st_code": [],
        "source_file": getattr(s, "source_file", ""),
        "source_type": "topology_slice",
        "logic_resolution_status": "partially_resolved",
        "guard_ast": getattr(s, "boolean_expression", None),
        "action_facts": [fact_dict(f) for f in getattr(s, "actions", [])],
        "guard_facts": [fact_dict(f) for f in getattr(s, "guards", [])],
        "common_enable_facts": [fact_dict(f) for f in getattr(s, "common_enables", [])],
        "unresolved_upstream": [fact_dict(f) for f in getattr(s, "unresolved_upstream", [])],
        "unresolved_logic": list(getattr(s, "unresolved_logic", []) or []),
        "evidence": {
            "anchor_id": getattr(s, "anchor_element_id", ""),
            "network_id": getattr(s, "anchor_network_id", ""),
            "unit_name": getattr(s, "anchor_unit_name", ""),
        },
    }
