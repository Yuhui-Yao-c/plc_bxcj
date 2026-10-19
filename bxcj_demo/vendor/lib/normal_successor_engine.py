#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/normal_successor_engine.py — Generic Normal Successor Discovery Engine.

Part C (Trace Readiness Gate): Extracted from build_final_timing_rows.py.
The engine implements the *generic mechanism*:
  - candidate filtering
  - feature calculation
  - scoring
  - hypothesis generation
  - evidence propagation

The *project-specific patterns* (which keywords, which selection rules,
which fallback policies) live in ``timing_boundary_spec.json`` per project.

Architecture:
    Spec (JSON)  →  NormalSuccessorEngine  →  NormalSuccessorHypothesis
                                             + TimingBoundaryEvidence

The engine NEVER branches on project name. All project variation is
expressed through the spec file.

Spec schema (timing_boundary_spec.json):
    {
        "completion_keywords": [...],
        "error_keywords": [...],
        "selection_patterns": [
            {
                "pattern_id": 1,
                "name": "...",
                "condition": "normal_count == 1",
                "hypothesis_status": "structurally_supported",
                "confidence": 0.8,
                "is_fallback": false,
                "fallback_policy": null
            },
            ...
        ],
        "fallback_policy": {
            "min_nonzero_target": true,
            "non_terminal_fallback": true,
            "never_treat_as_resolved": true
        }
    }
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from lib.action_row_fact import (
    NormalSuccessorHypothesis,
    TimingBoundaryEvidence,
    EDGE_ROLE_NORMAL_PROGRESSION,
    EDGE_ROLE_TERMINAL,
    EDGE_ROLE_RESET,
    EDGE_ROLE_CONDITIONAL_BRANCH,
    EDGE_ROLE_ERROR_RECOVERY,
    EDGE_ROLE_SKIP,
    EDGE_ROLE_ALTERNATE_PROCESS,
    EDGE_ROLE_UNRESOLVED,
    TIMING_HYP_VALIDATED,
    TIMING_HYP_STRUCTURALLY_SUPPORTED,
    TIMING_HYP_PLAUSIBLE,
    TIMING_HYP_FALLBACK,
    TIMING_HYP_UNRESOLVED,
    TIMING_SOURCE_NORMAL_SUCCESSOR,
    TIMING_SOURCE_FALLBACK,
    TIMING_SOURCE_UNRESOLVED,
    TIMING_SOURCE_STEP_EXIT,
    TIMING_NEXT_STEP_ENTRY,
)


# ============================================================
# Default Spec (used when no project spec is found)
# ============================================================

DEFAULT_SPEC = {
    'version': '1.0',
    'description': 'Default timing boundary spec — observed patterns',
    'completion_keywords': [
        'posdone', 'pos_done', '.xr', '.x_f', 'complete', 'finish',
    ],
    'error_keywords': [
        'alarm', 'jam', 'ng', 'err', 'timeout', 'safety', 'stop',
    ],
    'selection_patterns': [
        {
            'pattern_id': 1,
            'name': 'single_normal_candidate',
            'condition': 'normal_count == 1',
            'hypothesis_status': TIMING_HYP_STRUCTURALLY_SUPPORTED,
            'confidence': 0.8,
            'is_fallback': False,
            'fallback_policy': None,
        },
        {
            'pattern_id': 2,
            'name': 'multiple_normals_completion_evidence',
            'condition': 'normal_count > 1 and completion_count == 1',
            'hypothesis_status': TIMING_HYP_VALIDATED,
            'confidence': 0.7,
            'is_fallback': False,
            'fallback_policy': None,
        },
        {
            'pattern_id': 3,
            'name': 'multiple_normals_min_target_fallback',
            'condition': 'normal_count > 1 and completion_count != 1',
            'hypothesis_status': TIMING_HYP_FALLBACK,
            'confidence': 0.4,
            'is_fallback': True,
            'fallback_policy': 'select_min_nonzero_target',
        },
        {
            'pattern_id': 4,
            'name': 'no_normal_non_terminal_fallback',
            'condition': 'normal_count == 0 and non_terminal_count > 0',
            'hypothesis_status': TIMING_HYP_FALLBACK,
            'confidence': 0.3,
            'is_fallback': True,
            'fallback_policy': 'select_first_non_terminal',
        },
        {
            'pattern_id': 5,
            'name': 'only_terminal_or_error',
            'condition': 'normal_count == 0 and non_terminal_count == 0',
            'hypothesis_status': TIMING_HYP_UNRESOLVED,
            'confidence': 0.0,
            'is_fallback': False,
            'fallback_policy': None,
        },
    ],
    'fallback_policy': {
        'min_nonzero_target': True,
        'non_terminal_fallback': True,
        'never_treat_as_resolved': True,
    },
}


# ============================================================
# Spec Loader
# ============================================================

def load_spec(spec_path: str = None) -> dict:
    """Load timing_boundary_spec.json, falling back to DEFAULT_SPEC.

    Args:
        spec_path: Path to the spec file. If None or not found,
                   returns DEFAULT_SPEC.

    Returns:
        Spec dict with keys: completion_keywords, error_keywords,
        selection_patterns, fallback_policy.
    """
    if spec_path:
        p = Path(spec_path)
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
    return DEFAULT_SPEC.copy()


def save_default_spec(spec_path: str):
    """Save the default spec to a file (for project customization)."""
    p = Path(spec_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_SPEC, f, ensure_ascii=False, indent=2)


# ============================================================
# Step Info Extraction
# ============================================================

def extract_step_info(label: str):
    """Extract (state_var, thread, step_value) from a label string.

    Handles formats like:
        Process_steps[2]=130
        Process_steps[2]=INT#130
        NOT(Process_steps[2]=130)
    """
    if not label:
        return None, None, None
    label = label.strip().replace(' ', '')
    label = label.replace('NOT(', '').replace(')', '')
    label = label.replace('INT#', '')

    match = re.match(r'([\w]+)\[(\d+)\]=(\d+)', label)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(3))
    return None, None, None


# ============================================================
# Edge Classification (spec-driven, no project branching)
# ============================================================

def classify_edge(transition: dict, source_thread: int,
                  source_step_value: int, spec: dict) -> str:
    """Classify a transition edge into a role using spec keywords.

    Classification order (first match wins):
        1. target_value == 0                    → terminal
        2. condition contains error keywords    → error_recovery
        3. target_thread != source_thread       → conditional_branch
        4. condition contains completion keywords → normal_progression
        5. has condition (no completion kw)     → conditional_branch
        6. target_value > source_step_value     → normal_progression
        7. otherwise                            → unresolved
    """
    target_thread = transition.get('target_thread', -1)
    target_value = transition.get('target_value', 0)
    condition = transition.get('condition', '')
    cond_lower = condition.lower() if condition else ''

    error_kws = spec.get('error_keywords', DEFAULT_SPEC['error_keywords'])
    completion_kws = spec.get('completion_keywords',
                              DEFAULT_SPEC['completion_keywords'])

    if target_value == 0:
        return EDGE_ROLE_TERMINAL

    if any(k in cond_lower for k in error_kws):
        return EDGE_ROLE_ERROR_RECOVERY

    if target_thread != source_thread:
        return EDGE_ROLE_CONDITIONAL_BRANCH

    if any(k in cond_lower for k in completion_kws):
        return EDGE_ROLE_NORMAL_PROGRESSION

    if condition:
        return EDGE_ROLE_CONDITIONAL_BRANCH

    if target_value > source_step_value:
        return EDGE_ROLE_NORMAL_PROGRESSION

    return EDGE_ROLE_UNRESOLVED


# ============================================================
# Feature Calculation
# ============================================================

@dataclass
class CandidateFeatures:
    """Features calculated for each transition candidate."""
    target_thread: int = -1
    target_value: int = 0
    condition: str = ''
    edge_role: str = EDGE_ROLE_UNRESOLVED
    source: str = ''
    is_completion: bool = False
    is_same_thread: bool = False
    target_gt_source: bool = False

    @property
    def target_label(self) -> str:
        return ''  # set by caller with state_variable


def calculate_features(transition: dict, source_thread: int,
                       source_step_value: int, spec: dict) -> CandidateFeatures:
    """Calculate features for a single transition candidate."""
    target_thread = transition.get('target_thread', -1)
    target_value = transition.get('target_value', 0)
    condition = transition.get('condition', '')
    cond_lower = condition.lower() if condition else ''

    completion_kws = spec.get('completion_keywords',
                              DEFAULT_SPEC['completion_keywords'])

    return CandidateFeatures(
        target_thread=target_thread,
        target_value=target_value,
        condition=condition,
        edge_role=classify_edge(transition, source_thread,
                                source_step_value, spec),
        source=transition.get('source', ''),
        is_completion=any(k in cond_lower for k in completion_kws),
        is_same_thread=(target_thread == source_thread),
        target_gt_source=(target_value > source_step_value
                          if target_value is not None else False),
    )


# ============================================================
# Pattern Matching (spec-driven)
# ============================================================

def match_pattern(normal_count: int, completion_count: int,
                  non_terminal_count: int, spec: dict) -> dict:
    """Match the current candidate distribution to a selection pattern.

    Returns the matched pattern dict from the spec, or a default
    unresolved pattern if no match.
    """
    patterns = spec.get('selection_patterns', DEFAULT_SPEC['selection_patterns'])

    for p in patterns:
        cond = p.get('condition', '')
        pid = p.get('pattern_id', 0)

        if pid == 1 and normal_count == 1:
            return p
        if pid == 2 and normal_count > 1 and completion_count == 1:
            return p
        if pid == 3 and normal_count > 1 and completion_count != 1:
            return p
        if pid == 4 and normal_count == 0 and non_terminal_count > 0:
            return p
        if pid == 5 and normal_count == 0 and non_terminal_count == 0:
            return p

    # No match — return unresolved
    return {
        'pattern_id': 0,
        'name': 'no_match',
        'hypothesis_status': TIMING_HYP_UNRESOLVED,
        'confidence': 0.0,
        'is_fallback': False,
        'fallback_policy': None,
    }


# ============================================================
# Fallback Selection (spec-driven)
# ============================================================

def select_fallback(candidates: list, policy: str,
                    spec: dict) -> tuple:
    """Select a candidate using the specified fallback policy.

    Args:
        candidates: List of CandidateFeatures
        policy: Fallback policy name from spec
        spec: Full spec dict

    Returns:
        (selected_candidate, selection_reason)
    """
    if policy == 'select_min_nonzero_target':
        nonzero = [c for c in candidates
                   if c.target_value > 0
                   and c.edge_role == EDGE_ROLE_NORMAL_PROGRESSION]
        if nonzero:
            sorted_c = sorted(nonzero, key=lambda c: c.target_value)
            return sorted_c[0], (
                'Fallback: selected smallest non-zero target '
                '({}) among {} normal candidates'.format(
                    sorted_c[0].target_value, len(nonzero)))

    if policy == 'select_first_non_terminal':
        non_terminal = [c for c in candidates
                        if c.edge_role not in (
                            EDGE_ROLE_TERMINAL, EDGE_ROLE_ERROR_RECOVERY)]
        if non_terminal:
            return non_terminal[0], (
                'Fallback: no normal candidate; selected first '
                'non-terminal candidate (target={})'.format(
                    non_terminal[0].target_value))

    return None, 'No fallback candidate available'


# ============================================================
# Hypothesis Generation
# ============================================================

def build_hypothesis(step: dict, steps_by_key: dict,
                     spec: dict) -> tuple:
    """Build NormalSuccessorHypothesis + TimingBoundaryEvidence for a step.

    This is the core engine function. It:
      1. Extracts all transition candidates
      2. Calculates features for each
      3. Classifies edge roles
      4. Matches to a selection pattern from spec
      5. Selects the proposed normal target
      6. Generates hypothesis + timing boundary evidence

    Args:
        step: Step dict from steps_raw.json
        steps_by_key: Lookup dict {(thread, step_value): step}
        spec: timing_boundary_spec dict

    Returns:
        (NormalSuccessorHypothesis, TimingBoundaryEvidence)
        or (None, None) if step has no transitions.
    """
    thread_idx = step.get('thread_idx', 0)
    step_value = step.get('step_value', 0)
    transitions = step.get('transitions', [])
    state_var = step.get('state_variable', '')

    if not transitions:
        return None, None

    # Calculate features for all candidates
    candidates = []
    for t in transitions:
        feat = calculate_features(t, thread_idx, step_value, spec)
        candidates.append(feat)

    # Count candidate categories
    normal_candidates = [c for c in candidates
                         if c.edge_role == EDGE_ROLE_NORMAL_PROGRESSION]
    completion_candidates = [c for c in normal_candidates if c.is_completion]
    non_terminal = [c for c in candidates
                    if c.edge_role not in (
                        EDGE_ROLE_TERMINAL, EDGE_ROLE_ERROR_RECOVERY)]

    normal_count = len(normal_candidates)
    completion_count = len(completion_candidates)
    non_terminal_count = len(non_terminal)

    pattern_name = 'unknown'

    # Check for validated overrides (Part G — linear backbone replacement)
    override_key = 'thread={}/step={}'.format(thread_idx, step_value)
    overrides = spec.get('validated_overrides', {})
    override = overrides.get(override_key)

    if override:
        override_target = override.get('target', '')
        override_status = override.get(
            'status', TIMING_HYP_STRUCTURALLY_SUPPORTED)
        override_evidence = override.get('evidence', [])
        override_replaces = override.get('replaces', 'min_target_fallback')

        _, override_thread, override_value = extract_step_info(
            override_target)

        selected = None
        for c in candidates:
            if (c.target_thread == override_thread
                    and c.target_value == override_value):
                selected = c
                break

        if selected is None:
            selected = CandidateFeatures(
                target_thread=override_thread or thread_idx,
                target_value=override_value or 0,
                condition='',
                edge_role=EDGE_ROLE_NORMAL_PROGRESSION,
                source='validated_override',
            )

        hyp_status = override_status
        confidence = (0.9 if override_status == TIMING_HYP_VALIDATED
                      else 0.8)
        is_fallback = False
        pid = 0
        fallback_policy = None
        selection_reason = (
            'Validated override (replaces {}): {}'.format(
                override_replaces, ', '.join(override_evidence)))
        source = TIMING_SOURCE_NORMAL_SUCCESSOR
        pattern_name = 'validated_override'
    else:
        # Match to selection pattern
        pattern = match_pattern(normal_count, completion_count,
                                non_terminal_count, spec)

        pid = pattern.get('pattern_id', 0)
        hyp_status = pattern.get('hypothesis_status', TIMING_HYP_UNRESOLVED)
        confidence = pattern.get('confidence', 0.0)
        is_fallback = pattern.get('is_fallback', False)
        fallback_policy = pattern.get('fallback_policy')
        pattern_name = pattern.get('name', 'unknown')

        # Select the proposed target
        selected = None
        selection_reason = ''
        source = TIMING_SOURCE_NORMAL_SUCCESSOR

        if pid == 1:
            # Single normal candidate
            selected = normal_candidates[0]
            selection_reason = 'Exactly one normal_progression candidate'

        elif pid == 2:
            # Multiple normals, one with completion evidence
            selected = completion_candidates[0]
            selection_reason = (
                'Selected from {} normal candidates by completion '
                'condition'.format(normal_count))

        elif pid == 3 or pid == 4:
            # Fallback patterns
            source = TIMING_SOURCE_FALLBACK
            selected, selection_reason = select_fallback(
                candidates, fallback_policy, spec)

        elif pid == 5:
            # Only terminal/error — no timing end
            source = TIMING_SOURCE_STEP_EXIT
            selection_reason = (
                'Only terminal/error transitions; using step_exit')

    # Build target label
    proposed_target = ''
    if selected:
        proposed_target = '{}[{}]={}'.format(
            state_var, selected.target_thread, selected.target_value)

    # Build edge_roles map
    edge_roles = {}
    alternatives = []
    for c in candidates:
        label = '{}[{}]={}'.format(state_var, c.target_thread, c.target_value)
        edge_roles[label] = c.edge_role
        if selected and (c.target_thread != selected.target_thread
                         or c.target_value != selected.target_value):
            alternatives.append({
                'value': label,
                'edge_role': c.edge_role,
                'condition': c.condition[:80],
                'is_completion': c.is_completion,
            })

    # Build hypothesis_status for NormalSuccessorHypothesis
    if confidence >= 0.7:
        hyp_status_str = 'validated'
    elif confidence >= 0.4:
        hyp_status_str = 'plausible'
    else:
        hyp_status_str = 'ambiguous'

    # Determine timing source for evidence
    if is_fallback:
        timing_source = TIMING_SOURCE_FALLBACK
    elif pid == 5:
        timing_source = TIMING_SOURCE_STEP_EXIT
    elif proposed_target:
        timing_source = TIMING_SOURCE_NORMAL_SUCCESSOR
    else:
        timing_source = TIMING_SOURCE_UNRESOLVED

    hypothesis = NormalSuccessorHypothesis(
        source_step='thread={}/step={}'.format(thread_idx, step_value),
        source_thread=str(thread_idx),
        candidate_targets=[
            {
                'target_thread': c.target_thread,
                'target_value': c.target_value,
                'condition': c.condition,
                'edge_role': c.edge_role,
                'source': c.source,
            }
            for c in candidates
        ],
        proposed_normal_target=proposed_target,
        alternative_targets=alternatives,
        supporting_features=[selection_reason] if selection_reason else [],
        proposed_edge_roles=edge_roles,
        confidence=confidence,
        hypothesis_status=hyp_status_str,
    )

    evidence = TimingBoundaryEvidence(
        value=proposed_target,
        source=timing_source,
        hypothesis_status=hyp_status,
        confidence=confidence,
        is_fallback=is_fallback,
        evidence_path=[
            'pattern: {}'.format(pattern_name),
            'normal_count={}'.format(normal_count),
            'completion_count={}'.format(completion_count),
            'non_terminal_count={}'.format(non_terminal_count),
        ],
        alternatives=alternatives,
        unresolved_reason=(
            '' if proposed_target
            else 'No normal successor candidate found'),
        pattern_id=pid,
        selection_reason=selection_reason,
    )

    return hypothesis, evidence


# ============================================================
# Discovery (pattern mining from gold data — for spec formation)
# ============================================================

def discover_patterns(gold_pairs: list, steps_by_key: dict,
                      spec: dict) -> list:
    """Discover normal successor patterns from gold-matched data.

    This function is used during the discovery/validation phase to
    mine patterns from gold comparisons. It does NOT participate in
    runtime generation.

    Args:
        gold_pairs: List of {human_start, human_end, ...} dicts
        steps_by_key: Step lookup
        spec: Current spec (for edge classification)

    Returns:
        List of feature dicts for each gold pair.
    """
    discovery_data = []

    for pair in gold_pairs:
        human_start = pair.get('human_start', '')
        human_end = pair.get('human_end', '')

        _, start_thread, start_step = extract_step_info(human_start)
        _, end_thread, end_step = extract_step_info(human_end)

        if start_thread is None or start_step is None:
            continue

        step_key = (start_thread, start_step)
        step = steps_by_key.get(step_key)
        if not step:
            continue

        transitions = step.get('transitions', [])
        selected_t = None
        for t in transitions:
            if (t.get('target_thread') == end_thread
                    and t.get('target_value') == end_step):
                selected_t = t
                break

        if not selected_t:
            continue

        features = {
            'source_thread': start_thread,
            'source_step': start_step,
            'target_thread': end_thread,
            'target_value': end_step,
            'has_condition': bool(selected_t.get('condition')),
            'condition': selected_t.get('condition', '')[:60],
            'edge_role': classify_edge(
                selected_t, start_thread, start_step, spec),
            'target_is_zero': end_step == 0,
            'same_thread': end_thread == start_thread,
            'target_gt_source': (end_step > start_step
                                 if end_step is not None else False),
            'station': step.get('station', ''),
            'comment': step.get('comment', '')[:40],
            'num_transitions': len(transitions),
        }

        all_roles = []
        for t in transitions:
            role = classify_edge(t, start_thread, start_step, spec)
            all_roles.append(role)
        features['all_edge_roles'] = all_roles
        features['num_normal_candidates'] = sum(
            1 for r in all_roles if r == EDGE_ROLE_NORMAL_PROGRESSION)
        features['num_terminal'] = sum(
            1 for r in all_roles if r == EDGE_ROLE_TERMINAL)
        features['num_conditional'] = sum(
            1 for r in all_roles if r == EDGE_ROLE_CONDITIONAL_BRANCH)

        discovery_data.append(features)

    return discovery_data


# ============================================================
# Batch Processing
# ============================================================

def build_all_hypotheses(all_steps: list, steps_by_key: dict,
                         spec: dict) -> dict:
    """Build normal successor hypotheses for all steps.

    Args:
        all_steps: List of all step dicts
        steps_by_key: Step lookup {(thread, step_value): step}
        spec: timing_boundary_spec dict

    Returns:
        Dict {(thread_idx, step_value): (hypothesis, evidence)}
    """
    result = {}
    for step in all_steps:
        thread_idx = step.get('thread_idx', 0)
        step_value = step.get('step_value', 0)
        hyp, evidence = build_hypothesis(step, steps_by_key, spec)
        if hyp is not None:
            result[(thread_idx, step_value)] = (hyp, evidence)
    return result
