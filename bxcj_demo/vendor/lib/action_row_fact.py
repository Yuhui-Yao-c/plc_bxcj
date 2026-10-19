#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/action_row_fact.py — ActionRowFact & ActionBoundaryFact Schema

The final business deliverable is a five-column action table:

    | 动作描述 | 时长(ms) | 目标时长 | 开始标签 | 结束标签 |

ActionRowFact is the sole intermediate object before the final table
projection. It synthesizes:
  - Program facts (from StepGraph / Step Logic)  → start/end labels
  - Engineering semantics (from EvidenceGraph)   → 动作描述

This phase (Action Row Reconstruction) fills:
  - 动作描述  (from ActionSemanticFact + Description Components)
  - 开始标签  (from Program Graph + Boundary Reconstruction)
  - 结束标签  (from Program Graph + Boundary Reconstruction)

Duration and target_duration are left null — they are filled later
from Runtime Trace and target standard data sources respectively.

Design principles:
  - Per-field provenance: every description and boundary field carries
    its own evidence_level, evidence_path, supporting_sources, conflicts,
    and unresolved_reason.
  - Weakest-link principle: a row's overall evidence level is the minimum
    of its constituent fields.
  - Instance Bridge != Action Effect Relation: a validated instance bridge
    (e.g., stIOMotor[10] -> Y006.10) does NOT automatically validate the
    operation field. If FB body is unknown, operation stays plausible.
  - 1 Step != 1 Row: a step may produce multiple rows (split) or multiple
    raw actions may merge into one row (group).
  - No duration guessing from static code.
  - No reverse-fitting from human tables.

Usage:
    row = ActionRowFact(
        row_id='ar_0001',
        project='包mylar',
        step_identity='SC11_Main/step/110',
        ...
    )
    row.description.actor = ProvenancedField(
        value='S11AML抚平气缸',
        evidence_level='exact',
        evidence_path=[...],
    )
    row.boundary = ActionBoundaryFact(
        start_expr='m_nNextAutoStep[3]=INT#110',
        end_expr='NOT (m_nNextAutoStep[3]=INT#110)',
        boundary_type='step_active_interval',
        end_label_type='step_exit',
    )
    row.row_status = 'partial'
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path


# ============================================================
# Evidence Levels (aligned with evidence_model.py confidence levels)
# ============================================================

EV_EXACT = 'exact'              # Direct mapping (EC, HMI label, call-site binding)
EV_STRUCTURAL = 'structural'    # Structure-inferred (FB parameter, topology)
EV_CONTEXTUAL = 'contextual'    # Context-inferred (documentation, naming pattern)
EV_LLM_INFERRED = 'llm_inferred'  # LLM-inferred (last resort)
EV_NONE = 'none'                # No evidence

EVIDENCE_LEVEL_ORDER = {
    EV_EXACT: 4,
    EV_STRUCTURAL: 3,
    EV_CONTEXTUAL: 2,
    EV_LLM_INFERRED: 1,
    EV_NONE: 0,
}

VALID_EVIDENCE_LEVELS = {
    EV_EXACT, EV_STRUCTURAL, EV_CONTEXTUAL, EV_LLM_INFERRED, EV_NONE,
}


# ============================================================
# Row Status
# ============================================================

ROW_RESOLVED = 'resolved'                    # All key fields resolved with evidence
ROW_PARTIAL = 'partial'                      # Some fields resolved, others unresolved
ROW_UNRESOLVED = 'unresolved'                # Key fields cannot be resolved
ROW_EXCLUDED_INTERNAL = 'excluded_internal_logic'  # Internal logic, not a final row

VALID_ROW_STATUSES = {
    ROW_RESOLVED, ROW_PARTIAL, ROW_UNRESOLVED, ROW_EXCLUDED_INTERNAL,
}


# ============================================================
# Boundary Type
# ============================================================

BOUNDARY_STEP_ACTIVE_INTERVAL = 'step_active_interval'        # Action lasts entire step
BOUNDARY_NEXT_STEP_ENTRY = 'next_step_entry'                  # Next step marks completion
BOUNDARY_EXPLICIT_DONE_SIGNAL = 'explicit_done_signal'        # PosDone/sensor feedback
BOUNDARY_TRANSITION_CONDITION = 'transition_condition'        # Transition condition
BOUNDARY_EXTERNAL_FEEDBACK = 'external_feedback'              # External completion signal
BOUNDARY_PARALLEL_COMPLETION = 'parallel_completion'          # Parallel branch completion
BOUNDARY_UNRESOLVED = 'unresolved'                            # Cannot determine boundary

VALID_BOUNDARY_TYPES = {
    BOUNDARY_STEP_ACTIVE_INTERVAL, BOUNDARY_NEXT_STEP_ENTRY,
    BOUNDARY_EXPLICIT_DONE_SIGNAL, BOUNDARY_TRANSITION_CONDITION,
    BOUNDARY_EXTERNAL_FEEDBACK, BOUNDARY_PARALLEL_COMPLETION,
    BOUNDARY_UNRESOLVED,
}


# ============================================================
# End Label Type (Section 七 — three types of end labels)
# ============================================================

END_LABEL_STEP_EXIT = 'step_exit'               # Type A: NOT (current step)
END_LABEL_NEXT_STEP_ENTRY = 'next_step_entry'   # Type B: next step value
END_LABEL_EXTERNAL_DONE = 'external_done_signal'  # Type C: PosDone/sensor/etc.

VALID_END_LABEL_TYPES = {
    END_LABEL_STEP_EXIT, END_LABEL_NEXT_STEP_ENTRY, END_LABEL_EXTERNAL_DONE,
}


# ============================================================
# Action Role (Section 二 — role classification)
# ============================================================

ROLE_PHYSICAL_ACTUATOR_COMMAND = 'physical_actuator_command'
ROLE_PROCESS_COMMAND = 'process_command'
ROLE_MOTION_COMMAND = 'motion_command'
ROLE_DETECTION = 'detection'
ROLE_WAIT = 'wait'
ROLE_PARAMETER_WRITE = 'parameter_write'
ROLE_HMI_CONTROL = 'hmi_control'
ROLE_INTERNAL_STATE = 'internal_state'
ROLE_STATUS_UPDATE = 'status_update'
ROLE_ALARM_CONTROL = 'alarm_control'
ROLE_UNKNOWN = 'unknown'

VALID_ROLES = {
    ROLE_PHYSICAL_ACTUATOR_COMMAND, ROLE_PROCESS_COMMAND,
    ROLE_MOTION_COMMAND, ROLE_DETECTION, ROLE_WAIT,
    ROLE_PARAMETER_WRITE, ROLE_HMI_CONTROL, ROLE_INTERNAL_STATE,
    ROLE_STATUS_UPDATE, ROLE_ALARM_CONTROL, ROLE_UNKNOWN,
}


# ============================================================
# Boundary Semantic Role (Section 六 — distinguish completion signal
# from transition guard)
# ============================================================

SEMANTIC_ROLE_PHYSICAL_COMPLETION = 'physical_completion'
SEMANTIC_ROLE_STEP_EXIT = 'step_exit'
SEMANTIC_ROLE_NEXT_STEP_ENTRY = 'next_step_entry'
SEMANTIC_ROLE_TRANSITION_GUARD = 'transition_guard'
SEMANTIC_ROLE_EXTERNAL_EVENT = 'external_event'
SEMANTIC_ROLE_COMPOSITE_TRANSITION = 'composite_transition_condition'
SEMANTIC_ROLE_UNRESOLVED = 'unresolved'

VALID_SEMANTIC_ROLES = {
    SEMANTIC_ROLE_PHYSICAL_COMPLETION,
    SEMANTIC_ROLE_STEP_EXIT,
    SEMANTIC_ROLE_NEXT_STEP_ENTRY,
    SEMANTIC_ROLE_TRANSITION_GUARD,
    SEMANTIC_ROLE_EXTERNAL_EVENT,
    SEMANTIC_ROLE_COMPOSITE_TRANSITION,
    SEMANTIC_ROLE_UNRESOLVED,
}


# ============================================================
# Row Candidate Selection Rules (Section 三)
# ============================================================
#
# Roles that become final row candidates (appear in final table):
#   - physical_actuator_command : cylinder, vacuum, valve actions
#   - motion_command            : axis/robot motion
#   - process_command           : process control, sequence, home
#   - detection                 : CCD, sensor, inspect
#   - wait                      : explicit wait/delay/dwell
#
# Roles that do NOT independently become final rows (excluded as
# internal logic, but still tracked as Guard/Context/Boundary Evidence):
#   - parameter_write           : parameter value write
#   - hmi_control               : HMI window/button control
#   - internal_state            : internal state/data
#   - status_update             : status/communication signal
#   - alarm_control             : alarm/warning/error/safety
#   - unknown                   : no pattern match
#
# These rules are format-agnostic and project-agnostic.
# A project may override via Spec if certain roles need different
# treatment, but the default mapping applies universally.

CANDIDATE_ROLES = {
    ROLE_PHYSICAL_ACTUATOR_COMMAND,
    ROLE_MOTION_COMMAND,
    ROLE_PROCESS_COMMAND,
    ROLE_DETECTION,
    ROLE_WAIT,
}

NON_CANDIDATE_ROLES = {
    ROLE_PARAMETER_WRITE,
    ROLE_HMI_CONTROL,
    ROLE_INTERNAL_STATE,
    ROLE_STATUS_UPDATE,
    ROLE_ALARM_CONTROL,
    ROLE_UNKNOWN,
}


def is_row_candidate(role: str) -> bool:
    """Determine if a role should produce a final row candidate.

    Per Section 三 selection rules:
      - physical_actuator_command, motion_command, process_command,
        detection, wait → True (appear in final table)
      - parameter_write, hmi_control, internal_state, status_update,
        alarm_control, unknown → False (excluded as internal logic)

    Non-candidate facts are still tracked (not deleted) and can serve
    as Guard, Context, Boundary Evidence, or Semantic Evidence.
    """
    return role in CANDIDATE_ROLES


# ============================================================
# Role Status (Section 二 — role classification confidence)
# ============================================================

ROLE_STATUS_VALIDATED = 'validated'    # Confirmed by multiple evidence sources
ROLE_STATUS_PLAUSIBLE = 'plausible'    # Some evidence, not fully confirmed
ROLE_STATUS_UNRESOLVED = 'unresolved'  # Only naming-pattern evidence or no match

VALID_ROLE_STATUSES = {
    ROLE_STATUS_VALIDATED, ROLE_STATUS_PLAUSIBLE, ROLE_STATUS_UNRESOLVED,
}


# ============================================================
# Provenanced Field — value with evidence provenance
# ============================================================

@dataclass
class ProvenancedField:
    """A field value with its own evidence provenance.

    Every description component and boundary field in ActionRowFact
    carries its own provenance, following the weakest-link principle.

    Fields:
        value               — The resolved value (None if unresolved)
        evidence_level      — exact/structural/contextual/llm_inferred/none
        evidence_path       — List of evidence hops (chain of reasoning)
        supporting_sources  — List of source references (file, line, entity_id)
        conflicts           — List of conflicting evidence
        unresolved_reason   — Why this field cannot be resolved (if empty)
    """
    value: Any = None
    evidence_level: str = EV_NONE
    evidence_path: list = field(default_factory=list)
    supporting_sources: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    unresolved_reason: str = ''

    def __post_init__(self):
        if self.evidence_level not in VALID_EVIDENCE_LEVELS:
            raise ValueError('Invalid evidence_level: {}. Must be one of {}'.format(
                self.evidence_level, VALID_EVIDENCE_LEVELS))

    def is_resolved(self) -> bool:
        """Check if this field has a value and non-none evidence."""
        return self.value is not None and self.evidence_level != EV_NONE

    def to_dict(self) -> dict:
        return {
            'value': self.value,
            'evidence_level': self.evidence_level,
            'evidence_path': self.evidence_path,
            'supporting_sources': self.supporting_sources,
            'conflicts': self.conflicts,
            'unresolved_reason': self.unresolved_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ProvenancedField':
        if d is None:
            return cls()
        return cls(
            value=d.get('value'),
            evidence_level=d.get('evidence_level', EV_NONE),
            evidence_path=d.get('evidence_path', []),
            supporting_sources=d.get('supporting_sources', []),
            conflicts=d.get('conflicts', []),
            unresolved_reason=d.get('unresolved_reason', ''),
        )


# ============================================================
# Description Components — decomposed action description elements
# ============================================================

@dataclass
class DescriptionComponents:
    """Description components — decomposed elements of the action description.

    Before composing a Chinese sentence, each component is resolved
    independently with its own evidence. This enables:
      - Per-field evidence tracking (weakest-link principle)
      - Partial resolution (some fields resolved, others not)
      - Instance Bridge vs Action Effect Relation distinction

    The composed description is formed by combining resolved components.
    Unresolved components are omitted or marked as placeholder.

    Fields are split into two tiers:

    ### Program Identity (from source code — can be exact)
        instance_index       — Array index from source (e.g., 10 in stIOMotor[10])
        array_indices        — All array indices in the action reference
        program_actor_candidate — Variable/entity name from source (e.g., 'Axis090Program', 'EM06V.Cy_Clamp1')

    ### Engineering Semantic (requires engineering evidence — HMI/EC/
        documentation/validated relation/validated project mapping.
        Program variable names ALONE are NOT sufficient.)
        module              — 模块 (e.g., 上料搬运模块) — engineering module name
        mechanism           — 机构 (e.g., 合芯前机械手) — engineering mechanism name
        engineering_actor   — 执行器 (e.g., Z轴, 抚平气缸) — engineering actor name
        actor_type          — 执行器类型 (e.g., axis, cylinder, vacuum, robot)
        operation           — 动作 (e.g., 下降, 缩回, 伸出)
        command_role        — 命令角色 (e.g., command, feedback, status)
        direction_state     — 方向/状态 (e.g., up, down, extend, retract)
        target              — 目标 (e.g., 取料位, 焊接位) — MUST come from engineering evidence
        position_name       — 位置名称 (e.g., 放顶盖位) — MUST come from engineering evidence
        distance            — 距离 (e.g., 150mm)
        parameters          — 参数 (e.g., velocity, pressure)

    ### Special
        synchronized_actions — 同步动作 (list of action descriptions)

    CRITICAL RULE (Section 一, Part A.1):
        instance_index and array_indices can be filled from source code.
        target and position_name MUST NOT be filled from array indices alone.
        E.g., AxisValP[42] does NOT mean target=42.
        target must come from HMI/EC/documentation/validated mapping.
    """
    # --- Program Identity (can be exact from source) ---
    instance_index: ProvenancedField = field(default_factory=ProvenancedField)
    array_indices: ProvenancedField = field(default_factory=ProvenancedField)
    program_actor_candidate: ProvenancedField = field(default_factory=ProvenancedField)

    # --- Engineering Semantic (requires engineering evidence) ---
    module: ProvenancedField = field(default_factory=ProvenancedField)
    mechanism: ProvenancedField = field(default_factory=ProvenancedField)
    engineering_actor: ProvenancedField = field(default_factory=ProvenancedField)
    actor_type: ProvenancedField = field(default_factory=ProvenancedField)
    operation: ProvenancedField = field(default_factory=ProvenancedField)
    command_role: ProvenancedField = field(default_factory=ProvenancedField)
    direction_state: ProvenancedField = field(default_factory=ProvenancedField)
    target: ProvenancedField = field(default_factory=ProvenancedField)
    position_name: ProvenancedField = field(default_factory=ProvenancedField)
    distance: ProvenancedField = field(default_factory=ProvenancedField)
    parameters: ProvenancedField = field(default_factory=ProvenancedField)

    # --- Special ---
    synchronized_actions: ProvenancedField = field(default_factory=ProvenancedField)

    # Field names grouped by tier for metrics
    PROGRAM_IDENTITY_FIELDS = ('instance_index', 'array_indices', 'program_actor_candidate')
    ENGINEERING_SEMANTIC_FIELDS = ('module', 'mechanism', 'engineering_actor',
                                    'actor_type', 'operation', 'command_role',
                                    'direction_state', 'target', 'position_name',
                                    'distance', 'parameters')
    ALL_FIELDS = PROGRAM_IDENTITY_FIELDS + ENGINEERING_SEMANTIC_FIELDS + ('synchronized_actions',)

    # For backward compat — KEY_FIELDS maps to ALL_FIELDS
    KEY_FIELDS = ALL_FIELDS

    def get_field(self, name: str) -> ProvenancedField:
        """Get a ProvenancedField by name."""
        return getattr(self, name, ProvenancedField())

    def set_field(self, name: str, value: ProvenancedField):
        """Set a ProvenancedField by name."""
        if hasattr(self, name):
            setattr(self, name, value)

    def resolved_count(self) -> int:
        """Count of resolved fields (all fields)."""
        return sum(1 for name in self.ALL_FIELDS
                   if self.get_field(name).is_resolved())

    def engineering_resolved_count(self) -> int:
        """Count of resolved engineering semantic fields."""
        return sum(1 for name in self.ENGINEERING_SEMANTIC_FIELDS
                   if self.get_field(name).is_resolved())

    def program_identity_resolved_count(self) -> int:
        """Count of resolved program identity fields."""
        return sum(1 for name in self.PROGRAM_IDENTITY_FIELDS
                   if self.get_field(name).is_resolved())

    def total_count(self) -> int:
        """Total field count."""
        return len(self.ALL_FIELDS)

    def weakest_link(self) -> str:
        """Return the lowest evidence level across all fields."""
        min_level = EV_EXACT
        for name in self.ALL_FIELDS:
            f = self.get_field(name)
            if f.value is not None:
                level = f.evidence_level
                if EVIDENCE_LEVEL_ORDER.get(level, 0) < EVIDENCE_LEVEL_ORDER.get(min_level, 0):
                    min_level = level
        return min_level if min_level != EV_EXACT or self.resolved_count() == 0 else EV_EXACT

    def to_dict(self) -> dict:
        return {name: self.get_field(name).to_dict() for name in self.ALL_FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> 'DescriptionComponents':
        obj = cls()
        if d is None:
            return obj
        for name in cls.ALL_FIELDS:
            if name in d:
                obj.set_field(name, ProvenancedField.from_dict(d[name]))
            elif name == 'engineering_actor' and 'actor' in d:
                obj.set_field(name, ProvenancedField.from_dict(d['actor']))
        return obj


# ============================================================
# Engineering Row Relevance (Section 十九)
# ============================================================

RELEVANCE_PRIMARY_ACTION = 'primary_action'
RELEVANCE_SUPPORTING_ACTION = 'supporting_action'
RELEVANCE_COMPLETION_EVIDENCE = 'completion_evidence'
RELEVANCE_CONTEXT_ONLY = 'context_only'
RELEVANCE_INTERNAL_ONLY = 'internal_only'

VALID_RELEVANCE = {
    RELEVANCE_PRIMARY_ACTION,
    RELEVANCE_SUPPORTING_ACTION,
    RELEVANCE_COMPLETION_EVIDENCE,
    RELEVANCE_CONTEXT_ONLY,
    RELEVANCE_INTERNAL_ONLY,
}


# ============================================================
# Timing Boundary Type (for final table end label — NOT physical completion)
# ============================================================

TIMING_NEXT_STEP_ENTRY = 'timing_next_step_entry'
TIMING_STEP_EXIT = 'timing_step_exit'
TIMING_NOT_CURRENT_STEP = 'timing_not_current_step'
TIMING_EXPLICIT_NEXT_STATE = 'timing_explicit_next_state'
TIMING_EXTERNAL_SIGNAL = 'timing_external_signal'
TIMING_UNRESOLVED = 'timing_unresolved'

VALID_TIMING_TYPES = {
    TIMING_NEXT_STEP_ENTRY,
    TIMING_STEP_EXIT,
    TIMING_NOT_CURRENT_STEP,
    TIMING_EXPLICIT_NEXT_STATE,
    TIMING_EXTERNAL_SIGNAL,
    TIMING_UNRESOLVED,
}


# ============================================================
# Completion Type (physical completion — separate from timing)
# ============================================================

COMPLETION_PHYSICAL = 'physical_completion'      # PosDone, sensor, XR
COMPLETION_NONE = 'no_completion_signal'          # No completion signal found
COMPLETION_UNRESOLVED = 'completion_unresolved'

VALID_COMPLETION_TYPES = {
    COMPLETION_PHYSICAL,
    COMPLETION_NONE,
    COMPLETION_UNRESOLVED,
}


# ============================================================
# Timing Boundary Hypothesis Status (Part A — Trace Readiness Gate)
# ============================================================
#
# Each timing boundary (start/end) carries a hypothesis_status that
# reflects HOW it was determined, NOT just whether it has a value.
#
# Frozen mapping from Normal Successor Pattern to status:
#   Pattern 1 (single normal candidate)         → structurally_supported
#   Pattern 2 (multiple, completion evidence)    → validated or plausible (by evidence)
#   Pattern 3 (multiple, min nonzero target)     → fallback (NEVER resolved)
#   Pattern 4 (no normal, non-terminal)           → fallback (NEVER resolved)
#   No candidates at all                          → unresolved

TIMING_HYP_VALIDATED = 'validated'
TIMING_HYP_STRUCTURALLY_SUPPORTED = 'structurally_supported'
TIMING_HYP_PLAUSIBLE = 'plausible'
TIMING_HYP_FALLBACK = 'fallback'
TIMING_HYP_UNRESOLVED = 'unresolved'

VALID_TIMING_HYPOTHESIS_STATUSES = {
    TIMING_HYP_VALIDATED,
    TIMING_HYP_STRUCTURALLY_SUPPORTED,
    TIMING_HYP_PLAUSIBLE,
    TIMING_HYP_FALLBACK,
    TIMING_HYP_UNRESOLVED,
}

# Ordering for comparison (higher = more reliable)
TIMING_HYP_ORDER = {
    TIMING_HYP_VALIDATED: 4,
    TIMING_HYP_STRUCTURALLY_SUPPORTED: 3,
    TIMING_HYP_PLAUSIBLE: 2,
    TIMING_HYP_FALLBACK: 1,
    TIMING_HYP_UNRESOLVED: 0,
}


# ============================================================
# Timing Boundary Source (where the label value came from)
# ============================================================

TIMING_SOURCE_NORMAL_SUCCESSOR = 'normal_successor'
TIMING_SOURCE_STEP_ENTRY = 'step_entry'
TIMING_SOURCE_EXPLICIT_NEXT_STATE = 'explicit_next_state'
TIMING_SOURCE_EXTERNAL_SIGNAL = 'external_signal'
TIMING_SOURCE_STEP_EXIT = 'step_exit'
TIMING_SOURCE_FALLBACK = 'fallback'
TIMING_SOURCE_UNRESOLVED = 'unresolved'

VALID_TIMING_SOURCES = {
    TIMING_SOURCE_NORMAL_SUCCESSOR,
    TIMING_SOURCE_STEP_ENTRY,
    TIMING_SOURCE_EXPLICIT_NEXT_STATE,
    TIMING_SOURCE_EXTERNAL_SIGNAL,
    TIMING_SOURCE_STEP_EXIT,
    TIMING_SOURCE_FALLBACK,
    TIMING_SOURCE_UNRESOLVED,
}


# ============================================================
# Composite Merge Status (Part H/I — composite audit)
# ============================================================

MERGE_VALID_COMPOSITE = 'valid_composite'
MERGE_OVER_MERGED = 'over_merged'
MERGE_AMBIGUOUS = 'ambiguous'
MERGE_SINGLE = 'single'

VALID_MERGE_STATUSES = {
    MERGE_VALID_COMPOSITE,
    MERGE_OVER_MERGED,
    MERGE_AMBIGUOUS,
    MERGE_SINGLE,
}


# ============================================================
# Branch / Dependency / Compatibility relations (Part F/G/I)
# ============================================================

BRANCH_SAME = 'same_branch'
BRANCH_MUTUAL_EXCLUSIVE = 'mutual_exclusive'
BRANCH_DIFFERENT = 'different_branch'
BRANCH_UNKNOWN = 'unknown_branch'

VALID_BRANCH_RELATIONS = {
    BRANCH_SAME, BRANCH_MUTUAL_EXCLUSIVE,
    BRANCH_DIFFERENT, BRANCH_UNKNOWN,
}

DEP_NONE = 'no_dependency'
DEP_SEQUENTIAL = 'sequential'
DEP_PARALLEL = 'parallel'
DEP_UNKNOWN = 'unknown_dependency'

VALID_DEPENDENCY_RELATIONS = {
    DEP_NONE, DEP_SEQUENTIAL, DEP_PARALLEL, DEP_UNKNOWN,
}

COMPAT_SAME = 'same'
COMPAT_COMPATIBLE = 'compatible'
COMPAT_DIFFERENT = 'different'
COMPAT_UNKNOWN = 'unknown'

VALID_COMPATIBILITY = {
    COMPAT_SAME, COMPAT_COMPATIBLE, COMPAT_DIFFERENT, COMPAT_UNKNOWN,
}


# ============================================================
# Trace Readiness (Part T — Trace Readiness Score)
# ============================================================

TRACE_READY = 'ready'
TRACE_CONDITIONAL = 'conditional'
TRACE_NOT_READY = 'not_ready'

VALID_TRACE_READINESS = {
    TRACE_READY, TRACE_CONDITIONAL, TRACE_NOT_READY,
}


# ============================================================
# Trace Observability (Part U — can this label be observed in trace?)
# ============================================================

OBS_DIRECTLY_OBSERVABLE = 'directly_observable'
OBS_DERIVABLE = 'derivable_from_trace'
OBS_NOT_OBSERVABLE = 'not_observable'
OBS_UNKNOWN = 'unknown'

VALID_OBSERVABILITY = {
    OBS_DIRECTLY_OBSERVABLE,
    OBS_DERIVABLE,
    OBS_NOT_OBSERVABLE,
    OBS_UNKNOWN,
}


# ============================================================
# Timing Boundary Evidence — evidence-graded timing boundary (Part A)
# ============================================================

@dataclass
class TimingBoundaryEvidence:
    """Evidence-graded timing boundary (start or end).

    The final five-column table shows only ``value`` as the label string,
    but internally each boundary carries full provenance so that the
    Trace Readiness Gate can distinguish:

      - a *validated* boundary (backed by completion evidence)
      - a *structurally_supported* boundary (single normal successor)
      - a *plausible* boundary (selected from multiple candidates with
        some evidence)
      - a *fallback* boundary (min-target or non-terminal heuristic —
        NEVER to be treated as resolved)
      - an *unresolved* boundary (no candidate found)

    Fields:
        value               — The label string (e.g., 'Process_steps[2]=130')
        source              — Where the value came from (normal_successor /
                              step_entry / fallback / ...)
        hypothesis_status   — validated / structurally_supported / plausible
                              / fallback / unresolved
        confidence          — 0.0 to 1.0
        is_fallback         — True if this was a fallback selection
        evidence_path       — Chain of evidence hops
        alternatives        — Other candidate labels considered (not selected)
        unresolved_reason   — Why this boundary cannot be resolved (if status
                              is unresolved)
        pattern_id          — Which normal successor pattern was used (1-4),
                              empty for start boundaries
        selection_reason    — Human-readable reason for selecting this value
    """
    value: str = ''
    source: str = TIMING_SOURCE_UNRESOLVED
    hypothesis_status: str = TIMING_HYP_UNRESOLVED
    confidence: float = 0.0
    is_fallback: bool = False
    evidence_path: list = field(default_factory=list)
    alternatives: list = field(default_factory=list)
    unresolved_reason: str = ''
    pattern_id: int = 0
    selection_reason: str = ''

    def __post_init__(self):
        if self.hypothesis_status not in VALID_TIMING_HYPOTHESIS_STATUSES:
            raise ValueError(
                'Invalid hypothesis_status: {}. Must be one of {}'.format(
                    self.hypothesis_status, VALID_TIMING_HYPOTHESIS_STATUSES))
        if self.source not in VALID_TIMING_SOURCES:
            raise ValueError(
                'Invalid source: {}. Must be one of {}'.format(
                    self.source, VALID_TIMING_SOURCES))
        # is_fallback must be consistent with hypothesis_status
        if self.hypothesis_status == TIMING_HYP_FALLBACK:
            self.is_fallback = True

    def is_resolved(self) -> bool:
        """True if value is non-empty and status is NOT fallback/unresolved."""
        return bool(self.value) and self.hypothesis_status not in (
            TIMING_HYP_FALLBACK, TIMING_HYP_UNRESOLVED)

    def is_fallback_or_unresolved(self) -> bool:
        """True if this boundary is a fallback or unresolved."""
        return self.hypothesis_status in (
            TIMING_HYP_FALLBACK, TIMING_HYP_UNRESOLVED)

    def to_dict(self) -> dict:
        return {
            'value': self.value,
            'source': self.source,
            'hypothesis_status': self.hypothesis_status,
            'confidence': self.confidence,
            'is_fallback': self.is_fallback,
            'evidence_path': self.evidence_path,
            'alternatives': self.alternatives,
            'unresolved_reason': self.unresolved_reason,
            'pattern_id': self.pattern_id,
            'selection_reason': self.selection_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'TimingBoundaryEvidence':
        if d is None:
            return cls()
        return cls(
            value=d.get('value', ''),
            source=d.get('source', TIMING_SOURCE_UNRESOLVED),
            hypothesis_status=d.get('hypothesis_status', TIMING_HYP_UNRESOLVED),
            confidence=d.get('confidence', 0.0),
            is_fallback=d.get('is_fallback', False),
            evidence_path=d.get('evidence_path', []),
            alternatives=d.get('alternatives', []),
            unresolved_reason=d.get('unresolved_reason', ''),
            pattern_id=d.get('pattern_id', 0),
            selection_reason=d.get('selection_reason', ''),
        )


# ============================================================
# Action Boundary Fact — start/end label analysis
# ============================================================

@dataclass
class ActionBoundaryFact:
    """Boundary analysis for an ActionRowFact.

    Records the start/end label expressions, boundary type, evidence,
    and alternatives.

    End Label Types (Section 七):
      - Type A (step_exit):           NOT (m_nNextAutoStep[3]=INT#110)
        Action lasts entire step.
      - Type B (next_step_entry):     m_nNextAutoStep[3]=INT#1120
        Next step marks action completion.
      - Type C (external_done_signal): Axis PosDone / Robot PosDone / Sensor
        External completion condition.

    Boundary Types (Section 八):
      - step_active_interval:   Action active during entire step
      - next_step_entry:        Next step entry marks completion
      - explicit_done_signal:   Explicit done signal (PosDone, sensor)
      - transition_condition:   Transition condition determines end
      - external_feedback:      External feedback signal
      - parallel_completion:    Parallel branch completion
      - unresolved:             Cannot determine boundary

    Semantic Role (Section 六, Part B.6 — distinguish completion signal
    from transition guard):
      - physical_completion:       Axis.PosDone, Robot.PosDone, Cylinder.Out.XR
                                   — confirms physical action completion
      - step_exit:                 NOT (step_predicate) — step ended
      - next_step_entry:           Next step value — marks transition
      - transition_guard:          Quality=0 AND Safety AND Permission
                                   — guard allowing step exit, NOT physical completion
      - external_event:            External trigger (MES, operator, etc.)
      - composite_transition_condition: Multiple conditions ANDed/ORed
      - unresolved:                Cannot determine

    CRITICAL (Part B.6): A transition condition is NOT automatically a
    physical completion signal. E.g., "Quality=0 AND Safety" is a guard
    that permits step exit, not proof that the physical action completed.
    Only PosDone/sensor/XR-type signals are physical_completion.

    Selected End Label (Part B.7): Each row has at most ONE selected
    end label. Alternatives go into alternatives[] with selection_reason
    explaining why the selected one was chosen.

    Fields:
        action_row_id       — Reference to parent ActionRowFact
        start_expr          — Start label expression (engineering-readable)
        end_expr            — Selected end label expression (engineering-readable)
        boundary_type       — Type of boundary (see VALID_BOUNDARY_TYPES)
        end_label_type      — End label type (step_exit/next_step_entry/external_done_signal)
        semantic_role       — Semantic role of end boundary (see VALID_SEMANTIC_ROLES)
        start_label_type    — Start label type (usually step_entry)
        source_elements     — Program element IDs contributing to boundary
        evidence_path       — Chain of evidence for boundary determination
        evidence_level      — Confidence level of boundary
        confidence          — Textual confidence description
        alternatives        — Alternative boundary interpretations (NOT selected)
        selection_reason    — Why this end label was selected over alternatives
        unresolved_reason   — Why boundary cannot be resolved (if applicable)
    """
    action_row_id: str = ''
    start_expr: str = ''
    end_expr: str = ''
    boundary_type: str = BOUNDARY_UNRESOLVED
    end_label_type: str = ''
    semantic_role: str = SEMANTIC_ROLE_UNRESOLVED
    start_label_type: str = ''
    source_elements: list = field(default_factory=list)
    evidence_path: list = field(default_factory=list)
    evidence_level: str = EV_NONE
    confidence: str = ''
    alternatives: list = field(default_factory=list)
    selection_reason: str = ''
    unresolved_reason: str = ''

    # --- SEPARATED: Timing Boundary (for final table) vs Physical Completion ---
    # Per Section 一/二: These two concepts MUST be kept separate.
    # timing_end_label = when the timer stops for this row in the final table
    # completion_expr = when the physical action is actually done (evidence)
    timing_start_label: str = ''
    timing_end_label: str = ''
    timing_type: str = TIMING_UNRESOLVED
    timing_selection_reason: str = ''
    timing_alternatives: list = field(default_factory=list)

    completion_expr: str = ''
    completion_type: str = COMPLETION_NONE
    completion_evidence: list = field(default_factory=list)

    def __post_init__(self):
        if self.boundary_type not in VALID_BOUNDARY_TYPES:
            raise ValueError('Invalid boundary_type: {}. Must be one of {}'.format(
                self.boundary_type, VALID_BOUNDARY_TYPES))
        if self.evidence_level not in VALID_EVIDENCE_LEVELS:
            raise ValueError('Invalid evidence_level: {}. Must be one of {}'.format(
                self.evidence_level, VALID_EVIDENCE_LEVELS))
        if self.semantic_role not in VALID_SEMANTIC_ROLES:
            raise ValueError('Invalid semantic_role: {}. Must be one of {}'.format(
                self.semantic_role, VALID_SEMANTIC_ROLES))

    def is_resolved(self) -> bool:
        """Check if both start and end labels are resolved."""
        return bool(self.start_expr) and bool(self.end_expr) \
            and self.boundary_type != BOUNDARY_UNRESOLVED

    def to_dict(self) -> dict:
        return {
            'action_row_id': self.action_row_id,
            'start_expr': self.start_expr,
            'end_expr': self.end_expr,
            'boundary_type': self.boundary_type,
            'end_label_type': self.end_label_type,
            'semantic_role': self.semantic_role,
            'start_label_type': self.start_label_type,
            'source_elements': self.source_elements,
            'evidence_path': self.evidence_path,
            'evidence_level': self.evidence_level,
            'confidence': self.confidence,
            'alternatives': self.alternatives,
            'selection_reason': self.selection_reason,
            'unresolved_reason': self.unresolved_reason,
            'timing_start_label': self.timing_start_label,
            'timing_end_label': self.timing_end_label,
            'timing_type': self.timing_type,
            'timing_selection_reason': self.timing_selection_reason,
            'timing_alternatives': self.timing_alternatives,
            'completion_expr': self.completion_expr,
            'completion_type': self.completion_type,
            'completion_evidence': self.completion_evidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ActionBoundaryFact':
        if d is None:
            return cls()
        return cls(
            action_row_id=d.get('action_row_id', ''),
            start_expr=d.get('start_expr', ''),
            end_expr=d.get('end_expr', ''),
            boundary_type=d.get('boundary_type', BOUNDARY_UNRESOLVED),
            end_label_type=d.get('end_label_type', ''),
            semantic_role=d.get('semantic_role', SEMANTIC_ROLE_UNRESOLVED),
            start_label_type=d.get('start_label_type', ''),
            source_elements=d.get('source_elements', []),
            evidence_path=d.get('evidence_path', []),
            evidence_level=d.get('evidence_level', EV_NONE),
            confidence=d.get('confidence', ''),
            alternatives=d.get('alternatives', []),
            selection_reason=d.get('selection_reason', ''),
            unresolved_reason=d.get('unresolved_reason', ''),
            timing_start_label=d.get('timing_start_label', ''),
            timing_end_label=d.get('timing_end_label', ''),
            timing_type=d.get('timing_type', TIMING_UNRESOLVED),
            timing_selection_reason=d.get('timing_selection_reason', ''),
            timing_alternatives=d.get('timing_alternatives', []),
            completion_expr=d.get('completion_expr', ''),
            completion_type=d.get('completion_type', COMPLETION_NONE),
            completion_evidence=d.get('completion_evidence', []),
        )


# ============================================================
# Action Group Hypothesis (Section 九, Part C.9)
# ============================================================

GROUP_STATUS_VALIDATED = 'validated'
GROUP_STATUS_PLAUSIBLE = 'plausible'
GROUP_STATUS_AMBIGUOUS = 'ambiguous'
GROUP_STATUS_REJECTED = 'rejected'

VALID_GROUP_STATUSES = {
    GROUP_STATUS_VALIDATED, GROUP_STATUS_PLAUSIBLE,
    GROUP_STATUS_AMBIGUOUS, GROUP_STATUS_REJECTED,
}


@dataclass
class ActionGroupHypothesis:
    """Hypothesis for grouping multiple raw actions into one engineering action.

    Section 九 / Part C.8-10: Grouping must consider more than step+actor.
    A grouping hypothesis proposes that a set of raw actions constitutes
    one engineering action (grouped) vs. independent synchronized actions.

    Grouped actions:     Together express one engineering action
                         (e.g., set target + enable + start motion = "move to A")
    Synchronized actions: Independent actions happening simultaneously
                         (e.g., Z-axis down + cylinder clamp — two rows)

    Fields:
        group_id             — Unique group identifier
        raw_action_ids       — List of raw action element IDs in this group
        proposed_group       — 'grouped' (merge into 1 row) or 'synchronized' (keep separate)
        actor                — Shared program actor (if any)
        engineering_actor    — Shared engineering actor (if resolved)
        semantic_effect      — Proposed combined semantic effect
        shared_boundary      — Whether all actions share the same boundary
        concurrency_relation — 'sequential' | 'parallel' | 'unknown'
        supporting_evidence  — Evidence supporting this grouping
        conflicting_evidence — Evidence against this grouping
        status               — validated/plausible/ambiguous/rejected
        confidence           — 0.0 to 1.0
        grouping_reason      — Why these actions should be grouped
        rejection_reason     — Why grouping was rejected (if applicable)
    """
    group_id: str = ''
    raw_action_ids: list = field(default_factory=list)
    proposed_group: str = 'grouped'
    actor: str = ''
    engineering_actor: str = ''
    semantic_effect: str = ''
    shared_boundary: bool = False
    concurrency_relation: str = 'unknown'
    supporting_evidence: list = field(default_factory=list)
    conflicting_evidence: list = field(default_factory=list)
    status: str = GROUP_STATUS_PLAUSIBLE
    confidence: float = 0.0
    grouping_reason: str = ''
    rejection_reason: str = ''

    def __post_init__(self):
        if self.proposed_group not in ('grouped', 'synchronized'):
            raise ValueError('Invalid proposed_group: {}'.format(self.proposed_group))
        if self.status not in VALID_GROUP_STATUSES:
            raise ValueError('Invalid status: {}. Must be one of {}'.format(
                self.status, VALID_GROUP_STATUSES))

    def to_dict(self) -> dict:
        return {
            'group_id': self.group_id,
            'raw_action_ids': self.raw_action_ids,
            'proposed_group': self.proposed_group,
            'actor': self.actor,
            'engineering_actor': self.engineering_actor,
            'semantic_effect': self.semantic_effect,
            'shared_boundary': self.shared_boundary,
            'concurrency_relation': self.concurrency_relation,
            'supporting_evidence': self.supporting_evidence,
            'conflicting_evidence': self.conflicting_evidence,
            'status': self.status,
            'confidence': self.confidence,
            'grouping_reason': self.grouping_reason,
            'rejection_reason': self.rejection_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ActionGroupHypothesis':
        if d is None:
            return cls()
        return cls(
            group_id=d.get('group_id', ''),
            raw_action_ids=d.get('raw_action_ids', []),
            proposed_group=d.get('proposed_group', 'grouped'),
            actor=d.get('actor', ''),
            engineering_actor=d.get('engineering_actor', ''),
            semantic_effect=d.get('semantic_effect', ''),
            shared_boundary=d.get('shared_boundary', False),
            concurrency_relation=d.get('concurrency_relation', 'unknown'),
            supporting_evidence=d.get('supporting_evidence', []),
            conflicting_evidence=d.get('conflicting_evidence', []),
            status=d.get('status', GROUP_STATUS_PLAUSIBLE),
            confidence=d.get('confidence', 0.0),
            grouping_reason=d.get('grouping_reason', ''),
            rejection_reason=d.get('rejection_reason', ''),
        )


# ============================================================
# Engineering Action Group (Section 八/九 — final action unit)
# ============================================================

@dataclass
class EngineeringActionGroup:
    """An engineering action unit — the final row in the five-column table.

    Section 七/八: 1830 program actions must converge to engineering action
    units. Multiple program facts (primary action + support configuration +
    completion evidence + internal bookkeeping) together form ONE engineering
    action.

    Program Facts Classification (Section 九):
      - primary_action:     move, extend, retract, clamp, vacuum, weld, detect, wait
      - supporting_action:  target position assignment, speed, mode, enable
      - completion_evidence: PosDone, sensor, finish flag
      - internal_only:      median, state flag, statistic

    Only primary_action independently forms a group. supporting_action and
    completion_evidence are absorbed into the group they support.

    Fields:
        group_id             — Unique group ID
        step_identity        — Step this group belongs to
        thread               — Thread index
        raw_action_ids       — All raw action IDs in this group
        primary_action_ids   — IDs of primary actions (the core)
        support_action_ids   — IDs of support configuration actions
        completion_ids       — IDs of completion evidence actions
        internal_ids         — IDs of internal bookkeeping (excluded)
        actor_candidates     — Program actor candidates
        engineering_actor    — Engineering actor (if resolved)
        operation            — Operation (e.g., 定位, 下降)
        target               — Target/position (if resolved)
        parameters           — Parameters (speed, distance, etc.)
        composed_description — Final Chinese description
        start_boundary       — Timing start label
        end_boundary         — Timing end label (for final table)
        completion_signal    — Physical completion signal (separate from timing)
        synchronization_relations — Relations to other groups in same step
        evidence             — Supporting evidence
        group_status         — resolved/partial/unresolved
    """
    group_id: str = ''
    step_identity: str = ''
    thread: str = ''
    raw_action_ids: list = field(default_factory=list)
    primary_action_ids: list = field(default_factory=list)
    support_action_ids: list = field(default_factory=list)
    completion_ids: list = field(default_factory=list)
    internal_ids: list = field(default_factory=list)
    actor_candidates: list = field(default_factory=list)
    engineering_actor: str = ''
    operation: str = ''
    target: str = ''
    parameters: dict = field(default_factory=dict)
    composed_description: str = ''
    start_boundary: str = ''
    end_boundary: str = ''
    timing_type: str = TIMING_UNRESOLVED
    completion_signal: str = ''
    completion_type: str = COMPLETION_NONE
    synchronization_relations: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    group_status: str = ROW_UNRESOLVED
    project: str = ''

    def to_final_table_row(self) -> dict:
        """Project to the final five-column table row."""
        return {
            '动作描述': self.composed_description,
            '时长(ms)': None,
            '目标时长': None,
            '开始标签': self.start_boundary,
            '结束标签': self.end_boundary,
        }

    def to_dict(self) -> dict:
        return {
            'group_id': self.group_id,
            'step_identity': self.step_identity,
            'thread': self.thread,
            'raw_action_ids': self.raw_action_ids,
            'primary_action_ids': self.primary_action_ids,
            'support_action_ids': self.support_action_ids,
            'completion_ids': self.completion_ids,
            'internal_ids': self.internal_ids,
            'actor_candidates': self.actor_candidates,
            'engineering_actor': self.engineering_actor,
            'operation': self.operation,
            'target': self.target,
            'parameters': self.parameters,
            'composed_description': self.composed_description,
            'start_boundary': self.start_boundary,
            'end_boundary': self.end_boundary,
            'timing_type': self.timing_type,
            'completion_signal': self.completion_signal,
            'completion_type': self.completion_type,
            'synchronization_relations': self.synchronization_relations,
            'evidence': self.evidence,
            'group_status': self.group_status,
            'project': self.project,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'EngineeringActionGroup':
        if d is None:
            return cls()
        return cls(
            group_id=d.get('group_id', ''),
            step_identity=d.get('step_identity', ''),
            thread=d.get('thread', ''),
            raw_action_ids=d.get('raw_action_ids', []),
            primary_action_ids=d.get('primary_action_ids', []),
            support_action_ids=d.get('support_action_ids', []),
            completion_ids=d.get('completion_ids', []),
            internal_ids=d.get('internal_ids', []),
            actor_candidates=d.get('actor_candidates', []),
            engineering_actor=d.get('engineering_actor', ''),
            operation=d.get('operation', ''),
            target=d.get('target', ''),
            parameters=d.get('parameters', {}),
            composed_description=d.get('composed_description', ''),
            start_boundary=d.get('start_boundary', ''),
            end_boundary=d.get('end_boundary', ''),
            timing_type=d.get('timing_type', TIMING_UNRESOLVED),
            completion_signal=d.get('completion_signal', ''),
            completion_type=d.get('completion_type', COMPLETION_NONE),
            synchronization_relations=d.get('synchronization_relations', []),
            evidence=d.get('evidence', []),
            group_status=d.get('group_status', ROW_UNRESOLVED),
            project=d.get('project', ''),
        )


# ============================================================
# Normal Successor Hypothesis (Section I/K — normal timing successor)
# ============================================================

EDGE_ROLE_NORMAL_PROGRESSION = 'normal_progression'
EDGE_ROLE_TERMINAL = 'terminal'
EDGE_ROLE_RESET = 'reset'
EDGE_ROLE_CONDITIONAL_BRANCH = 'conditional_branch'
EDGE_ROLE_ERROR_RECOVERY = 'error_recovery'
EDGE_ROLE_SKIP = 'skip'
EDGE_ROLE_ALTERNATE_PROCESS = 'alternate_process'
EDGE_ROLE_UNRESOLVED = 'unresolved'

VALID_EDGE_ROLES = {
    EDGE_ROLE_NORMAL_PROGRESSION, EDGE_ROLE_TERMINAL, EDGE_ROLE_RESET,
    EDGE_ROLE_CONDITIONAL_BRANCH, EDGE_ROLE_ERROR_RECOVERY,
    EDGE_ROLE_SKIP, EDGE_ROLE_ALTERNATE_PROCESS, EDGE_ROLE_UNRESOLVED,
}


@dataclass
class NormalSuccessorHypothesis:
    """Hypothesis for which transition is the normal timing successor.

    Section G/I: For steps with multiple transitions, determine which
    target represents normal timing progression vs reset/skip/error.

    Fields:
        source_step          — Source step identity
        source_thread        — Source thread
        candidate_targets    — All candidate transitions with edge_role
        proposed_normal_target — The proposed normal successor
        alternative_targets  — Other targets with their roles
        supporting_features  — Features supporting the proposal
        conflicting_features — Features against the proposal
        proposed_edge_roles  — Map of target → edge_role
        confidence           — 0.0 to 1.0
        hypothesis_status    — validated/plausible/ambiguous/rejected
        validation_samples   — Samples from discovery/validation
        holdout_result       — Holdout validation result
    """
    source_step: str = ''
    source_thread: str = ''
    candidate_targets: list = field(default_factory=list)
    proposed_normal_target: str = ''
    alternative_targets: list = field(default_factory=list)
    supporting_features: list = field(default_factory=list)
    conflicting_features: list = field(default_factory=list)
    proposed_edge_roles: dict = field(default_factory=dict)
    confidence: float = 0.0
    hypothesis_status: str = 'plausible'
    validation_samples: list = field(default_factory=list)
    holdout_result: str = ''

    def to_dict(self) -> dict:
        return {
            'source_step': self.source_step,
            'source_thread': self.source_thread,
            'candidate_targets': self.candidate_targets,
            'proposed_normal_target': self.proposed_normal_target,
            'alternative_targets': self.alternative_targets,
            'supporting_features': self.supporting_features,
            'conflicting_features': self.conflicting_features,
            'proposed_edge_roles': self.proposed_edge_roles,
            'confidence': self.confidence,
            'hypothesis_status': self.hypothesis_status,
            'validation_samples': self.validation_samples,
            'holdout_result': self.holdout_result,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'NormalSuccessorHypothesis':
        if d is None:
            return cls()
        return cls(
            source_step=d.get('source_step', ''),
            source_thread=d.get('source_thread', ''),
            candidate_targets=d.get('candidate_targets', []),
            proposed_normal_target=d.get('proposed_normal_target', ''),
            alternative_targets=d.get('alternative_targets', []),
            supporting_features=d.get('supporting_features', []),
            conflicting_features=d.get('conflicting_features', []),
            proposed_edge_roles=d.get('proposed_edge_roles', {}),
            confidence=d.get('confidence', 0.0),
            hypothesis_status=d.get('hypothesis_status', 'plausible'),
            validation_samples=d.get('validation_samples', []),
            holdout_result=d.get('holdout_result', ''),
        )


# ============================================================
# Timing Row Grouping Hypothesis (Section D — composite timing row)
# ============================================================

ROW_TYPE_SINGLE = 'single'
ROW_TYPE_COMPOSITE_SYNCHRONIZED = 'composite_synchronized'
ROW_TYPE_PARALLEL_CHANNEL = 'parallel_channel'
ROW_TYPE_UNRESOLVED = 'unresolved'

VALID_ROW_TYPES = {
    ROW_TYPE_SINGLE, ROW_TYPE_COMPOSITE_SYNCHRONIZED,
    ROW_TYPE_PARALLEL_CHANNEL, ROW_TYPE_UNRESOLVED,
}


@dataclass
class TimingRowGroupingHypothesis:
    """Hypothesis for grouping EngineeringActionGroups into one FinalTimingRow.

    Section D/E: Multiple groups with same timing interval → composite row.
    Synchronized groups (same step, same start/end) → one timing row.

    Part F/G/I (Trace Readiness Gate): ``same boundary`` alone is NO LONGER
    sufficient to validate a composite. The following compatibility checks
    must be performed and recorded:

      - state_domain_compatibility : same state variable? (Process_steps vs
                                      Init_Steps → different, cannot merge)
      - thread_compatibility        : same thread/channel? (Thread1 Step100
                                      vs Thread5 Step100 → different)
      - branch_relation             : are groups in the same IF/ELSE branch?
                                      (mutual_exclusive → CANNOT merge)
      - dependency_relation         : sequential vs parallel? (A finishes
                                      then B starts → sequential, CANNOT
                                      merge even if same step)
      - concurrency_evidence        : evidence that groups actually execute
                                      concurrently (same step active interval)
      - merge_status                : valid_composite / over_merged / ambiguous
      - merge_confidence            — 0.0 to 1.0

    Forbidden composites (Part G):
      1. Mutual-exclusive branch (IF/ELSE sides)
      2. Sequential actions (A completes → B starts, same outer step)
      3. Different threads sharing same step value
      4. Different state domains (unless sync/combine evidence exists)

    Fields:
        engineering_group_ids       — Groups in this timing row
        shared_start_boundary       — Common start label
        shared_end_boundary         — Common end label
        concurrency_relation        — 'parallel' | 'sequential' | 'unknown'
        same_step                   — Whether all groups are in same step
        same_thread                 — Whether all groups are in same thread
        semantic_compatibility      — Whether groups can be semantically combined
        proposed_row_type           — single/composite_synchronized/parallel_channel
        supporting_evidence         — Evidence for grouping
        conflicts                   — Evidence against grouping
        status                      — validated/plausible/ambiguous/rejected
        confidence                  — 0.0 to 1.0
        --- Part I additions ---
        concurrency_evidence        — Evidence that groups execute concurrently
        branch_relation             — same_branch / mutual_exclusive / different_branch / unknown
        dependency_relation         — no_dependency / sequential / parallel / unknown
        state_domain_compatibility  — same / compatible / different / unknown
        thread_compatibility        — same / compatible / different / unknown
        merge_status                — valid_composite / over_merged / ambiguous / single
        merge_confidence            — 0.0 to 1.0 (confidence in the merge decision)
    """
    engineering_group_ids: list = field(default_factory=list)
    shared_start_boundary: str = ''
    shared_end_boundary: str = ''
    concurrency_relation: str = 'parallel'
    same_step: bool = False
    same_thread: bool = False
    semantic_compatibility: bool = True
    proposed_row_type: str = ROW_TYPE_SINGLE
    supporting_evidence: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    status: str = 'plausible'
    confidence: float = 0.0

    # --- Part I additions (Trace Readiness Gate) ---
    concurrency_evidence: list = field(default_factory=list)
    branch_relation: str = BRANCH_UNKNOWN
    dependency_relation: str = DEP_UNKNOWN
    state_domain_compatibility: str = COMPAT_UNKNOWN
    thread_compatibility: str = COMPAT_UNKNOWN
    merge_status: str = MERGE_SINGLE
    merge_confidence: float = 0.0

    def __post_init__(self):
        if self.branch_relation not in VALID_BRANCH_RELATIONS:
            raise ValueError(
                'Invalid branch_relation: {}. Must be one of {}'.format(
                    self.branch_relation, VALID_BRANCH_RELATIONS))
        if self.dependency_relation not in VALID_DEPENDENCY_RELATIONS:
            raise ValueError(
                'Invalid dependency_relation: {}. Must be one of {}'.format(
                    self.dependency_relation, VALID_DEPENDENCY_RELATIONS))
        if self.state_domain_compatibility not in VALID_COMPATIBILITY:
            raise ValueError(
                'Invalid state_domain_compatibility: {}. Must be one of {}'.format(
                    self.state_domain_compatibility, VALID_COMPATIBILITY))
        if self.thread_compatibility not in VALID_COMPATIBILITY:
            raise ValueError(
                'Invalid thread_compatibility: {}. Must be one of {}'.format(
                    self.thread_compatibility, VALID_COMPATIBILITY))
        if self.merge_status not in VALID_MERGE_STATUSES:
            raise ValueError(
                'Invalid merge_status: {}. Must be one of {}'.format(
                    self.merge_status, VALID_MERGE_STATUSES))

    def to_dict(self) -> dict:
        return {
            'engineering_group_ids': self.engineering_group_ids,
            'shared_start_boundary': self.shared_start_boundary,
            'shared_end_boundary': self.shared_end_boundary,
            'concurrency_relation': self.concurrency_relation,
            'same_step': self.same_step,
            'same_thread': self.same_thread,
            'semantic_compatibility': self.semantic_compatibility,
            'proposed_row_type': self.proposed_row_type,
            'supporting_evidence': self.supporting_evidence,
            'conflicts': self.conflicts,
            'status': self.status,
            'confidence': self.confidence,
            'concurrency_evidence': self.concurrency_evidence,
            'branch_relation': self.branch_relation,
            'dependency_relation': self.dependency_relation,
            'state_domain_compatibility': self.state_domain_compatibility,
            'thread_compatibility': self.thread_compatibility,
            'merge_status': self.merge_status,
            'merge_confidence': self.merge_confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'TimingRowGroupingHypothesis':
        if d is None:
            return cls()
        return cls(
            engineering_group_ids=d.get('engineering_group_ids', []),
            shared_start_boundary=d.get('shared_start_boundary', ''),
            shared_end_boundary=d.get('shared_end_boundary', ''),
            concurrency_relation=d.get('concurrency_relation', 'parallel'),
            same_step=d.get('same_step', False),
            same_thread=d.get('same_thread', False),
            semantic_compatibility=d.get('semantic_compatibility', True),
            proposed_row_type=d.get('proposed_row_type', ROW_TYPE_SINGLE),
            supporting_evidence=d.get('supporting_evidence', []),
            conflicts=d.get('conflicts', []),
            status=d.get('status', 'plausible'),
            confidence=d.get('confidence', 0.0),
            concurrency_evidence=d.get('concurrency_evidence', []),
            branch_relation=d.get('branch_relation', BRANCH_UNKNOWN),
            dependency_relation=d.get('dependency_relation', DEP_UNKNOWN),
            state_domain_compatibility=d.get('state_domain_compatibility', COMPAT_UNKNOWN),
            thread_compatibility=d.get('thread_compatibility', COMPAT_UNKNOWN),
            merge_status=d.get('merge_status', MERGE_SINGLE),
            merge_confidence=d.get('merge_confidence', 0.0),
        )


# ============================================================
# Final Timing Row Fact (Section B — final table row)
# ============================================================

TIMING_ROW_STANDALONE = 'standalone_timing_action'
TIMING_ROW_COMPOSITE_MEMBER = 'composite_member'
TIMING_ROW_SUPPORTING_ONLY = 'supporting_only'
TIMING_ROW_COMPLETION_ONLY = 'completion_only'
TIMING_ROW_INTERNAL_ONLY = 'internal_only'
TIMING_ROW_UNRESOLVED = 'unresolved'

VALID_TIMING_ROW_RELEVANCE = {
    TIMING_ROW_STANDALONE, TIMING_ROW_COMPOSITE_MEMBER,
    TIMING_ROW_SUPPORTING_ONLY, TIMING_ROW_COMPLETION_ONLY,
    TIMING_ROW_INTERNAL_ONLY, TIMING_ROW_UNRESOLVED,
}


@dataclass
class FinalTimingRowFact:
    """A row in the final five-column timing table.

    Section A/B: FinalTimingRow represents a timing interval in the final
    table. Multiple EngineeringActionGroups with the same timing interval
    → one FinalTimingRow (composite).

    Layer hierarchy:
        Program Facts → EngineeringActionGroup → FinalTimingRow → Final Table

    EngineeringActionGroup answers: "what engineering actions exist?"
    FinalTimingRow answers: "which actions share the same timing interval?"

    Fields:
        row_id                   — Unique row ID
        state_domain             — State variable (e.g., Process_steps)
        thread                   — Thread/channel
        source_steps             — Steps contributing to this row
        source_engineering_group_ids — EngineeringActionGroup IDs in this row
        primary_actions          — Primary action descriptions
        synchronized_actions     — Synchronized action descriptions
        description_components   — Composed description parts
        composed_description     — Final Chinese description (ALL actions listed,
                                    never "等N个动作" as final text — Part S)
        description_quality      — strong / usable / weak / unresolved (Part R)
        timing_start             — Structured timing start evidence (Part A)
        timing_end               — Structured timing end evidence (Part A)
        timing_start_label       — Flat start label (derived from timing_start.value)
        timing_end_label         — Flat end label (derived from timing_end.value)
        timing_type              — Type of timing boundary
        boundary_evidence        — Evidence for boundary selection (legacy flat)
        boundary_status          — resolved/fallback/unresolved (legacy flat)
        boundary_alternatives    — Alternative end labels (legacy flat)
        completion_signals       — Physical completion signals (separate)
        duration_ms              — null (filled by Trace later)
        target_duration_ms       — null (filled by target data source)
        row_status               — resolved/partial/unresolved
        needs_review             — Whether this row needs manual review
        unresolved_reason        — Why row is unresolved (if applicable)
        grouping_hypothesis      — How groups were combined (Part I upgraded)
        timing_row_relevance     — standalone/composite_member/etc.
        trace_readiness          — ready / conditional / not_ready (Part T)
        start_observability      — directly_observable / derivable / not_observable / unknown (Part U)
        end_observability        — directly_observable / derivable / not_observable / unknown (Part U)
        provenance               — Provenance info
    """
    row_id: str = ''
    state_domain: str = ''
    thread: str = ''
    source_steps: list = field(default_factory=list)
    source_engineering_group_ids: list = field(default_factory=list)

    primary_actions: list = field(default_factory=list)
    synchronized_actions: list = field(default_factory=list)
    description_components: list = field(default_factory=list)
    composed_description: str = ''
    description_quality: str = 'unresolved'

    # --- Part A: Structured timing boundary evidence ---
    timing_start: TimingBoundaryEvidence = None
    timing_end: TimingBoundaryEvidence = None

    # --- Legacy flat fields (derived from timing_start/timing_end) ---
    timing_start_label: str = ''
    timing_end_label: str = ''
    timing_type: str = TIMING_UNRESOLVED
    boundary_evidence: list = field(default_factory=list)
    boundary_status: str = 'unresolved'
    boundary_alternatives: list = field(default_factory=list)

    completion_signals: list = field(default_factory=list)

    duration_ms: float = None
    target_duration_ms: float = None

    row_status: str = ROW_UNRESOLVED
    needs_review: bool = False
    unresolved_reason: str = ''

    grouping_hypothesis: TimingRowGroupingHypothesis = None
    timing_row_relevance: str = TIMING_ROW_UNRESOLVED

    # --- Part T/U: Trace Readiness + Observability ---
    trace_readiness: str = TRACE_NOT_READY
    start_observability: str = OBS_UNKNOWN
    end_observability: str = OBS_UNKNOWN

    provenance: str = ''

    def __post_init__(self):
        if self.trace_readiness not in VALID_TRACE_READINESS:
            raise ValueError(
                'Invalid trace_readiness: {}. Must be one of {}'.format(
                    self.trace_readiness, VALID_TRACE_READINESS))
        if self.start_observability not in VALID_OBSERVABILITY:
            raise ValueError(
                'Invalid start_observability: {}. Must be one of {}'.format(
                    self.start_observability, VALID_OBSERVABILITY))
        if self.end_observability not in VALID_OBSERVABILITY:
            raise ValueError(
                'Invalid end_observability: {}. Must be one of {}'.format(
                    self.end_observability, VALID_OBSERVABILITY))
        # Initialize timing_start/timing_end if not provided
        if self.timing_start is None:
            self.timing_start = TimingBoundaryEvidence(
                value=self.timing_start_label,
                source=TIMING_SOURCE_STEP_ENTRY,
            )
        if self.timing_end is None:
            self.timing_end = TimingBoundaryEvidence(
                value=self.timing_end_label,
                source=TIMING_SOURCE_UNRESOLVED,
            )
        # Sync flat fields from structured ones
        self._sync_flat_fields()

    def _sync_flat_fields(self):
        """Sync legacy flat fields from structured timing_start/timing_end."""
        if self.timing_start:
            self.timing_start_label = self.timing_start.value
        if self.timing_end:
            self.timing_end_label = self.timing_end.value
            self.boundary_status = self.timing_end.hypothesis_status
            if self.timing_end.is_fallback:
                self.boundary_status = 'fallback'
            elif self.timing_end.hypothesis_status == TIMING_HYP_UNRESOLVED:
                self.boundary_status = 'unresolved'
            elif self.timing_end.hypothesis_status in (
                    TIMING_HYP_VALIDATED, TIMING_HYP_STRUCTURALLY_SUPPORTED,
                    TIMING_HYP_PLAUSIBLE):
                self.boundary_status = 'resolved'
            self.boundary_alternatives = self.timing_end.alternatives

    def to_final_table_row(self) -> dict:
        """Project to the final five-column table row."""
        return {
            '动作描述': self.composed_description,
            '时长(ms)': self.duration_ms,
            '目标时长': self.target_duration_ms,
            '开始标签': self.timing_start_label,
            '结束标签': self.timing_end_label,
        }

    def to_dict(self) -> dict:
        return {
            'row_id': self.row_id,
            'state_domain': self.state_domain,
            'thread': self.thread,
            'source_steps': self.source_steps,
            'source_engineering_group_ids': self.source_engineering_group_ids,
            'primary_actions': self.primary_actions,
            'synchronized_actions': self.synchronized_actions,
            'description_components': self.description_components,
            'composed_description': self.composed_description,
            'description_quality': self.description_quality,
            'timing_start': self.timing_start.to_dict() if self.timing_start else None,
            'timing_end': self.timing_end.to_dict() if self.timing_end else None,
            'timing_start_label': self.timing_start_label,
            'timing_end_label': self.timing_end_label,
            'timing_type': self.timing_type,
            'boundary_evidence': self.boundary_evidence,
            'boundary_status': self.boundary_status,
            'boundary_alternatives': self.boundary_alternatives,
            'completion_signals': self.completion_signals,
            'duration_ms': self.duration_ms,
            'target_duration_ms': self.target_duration_ms,
            'row_status': self.row_status,
            'needs_review': self.needs_review,
            'unresolved_reason': self.unresolved_reason,
            'grouping_hypothesis': self.grouping_hypothesis.to_dict() if self.grouping_hypothesis else None,
            'timing_row_relevance': self.timing_row_relevance,
            'trace_readiness': self.trace_readiness,
            'start_observability': self.start_observability,
            'end_observability': self.end_observability,
            'provenance': self.provenance,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'FinalTimingRowFact':
        if d is None:
            return cls()
        gh = None
        if d.get('grouping_hypothesis'):
            gh = TimingRowGroupingHypothesis.from_dict(d['grouping_hypothesis'])
        ts = None
        if d.get('timing_start'):
            ts = TimingBoundaryEvidence.from_dict(d['timing_start'])
        te = None
        if d.get('timing_end'):
            te = TimingBoundaryEvidence.from_dict(d['timing_end'])
        return cls(
            row_id=d.get('row_id', ''),
            state_domain=d.get('state_domain', ''),
            thread=d.get('thread', ''),
            source_steps=d.get('source_steps', []),
            source_engineering_group_ids=d.get('source_engineering_group_ids', []),
            primary_actions=d.get('primary_actions', []),
            synchronized_actions=d.get('synchronized_actions', []),
            description_components=d.get('description_components', []),
            composed_description=d.get('composed_description', ''),
            description_quality=d.get('description_quality', 'unresolved'),
            timing_start=ts,
            timing_end=te,
            timing_start_label=d.get('timing_start_label', ''),
            timing_end_label=d.get('timing_end_label', ''),
            timing_type=d.get('timing_type', TIMING_UNRESOLVED),
            boundary_evidence=d.get('boundary_evidence', []),
            boundary_status=d.get('boundary_status', 'unresolved'),
            boundary_alternatives=d.get('boundary_alternatives', []),
            completion_signals=d.get('completion_signals', []),
            duration_ms=d.get('duration_ms'),
            target_duration_ms=d.get('target_duration_ms'),
            row_status=d.get('row_status', ROW_UNRESOLVED),
            needs_review=d.get('needs_review', False),
            unresolved_reason=d.get('unresolved_reason', ''),
            grouping_hypothesis=gh,
            timing_row_relevance=d.get('timing_row_relevance', TIMING_ROW_UNRESOLVED),
            trace_readiness=d.get('trace_readiness', TRACE_NOT_READY),
            start_observability=d.get('start_observability', OBS_UNKNOWN),
            end_observability=d.get('end_observability', OBS_UNKNOWN),
            provenance=d.get('provenance', ''),
        )


# ============================================================
# Final Timing Row Store
# ============================================================

@dataclass
class FinalTimingRowStore:
    """Collection of FinalTimingRowFacts with query and stats."""
    rows: list = field(default_factory=list)

    def add(self, row: FinalTimingRowFact):
        if not row.row_id:
            row.row_id = 'ftr_{:04d}'.format(len(self.rows) + 1)
        self.rows.append(row)

    def get_resolved(self) -> list:
        return [r for r in self.rows if r.row_status == ROW_RESOLVED]

    def get_composite(self) -> list:
        return [r for r in self.rows
                if r.grouping_hypothesis and
                r.grouping_hypothesis.proposed_row_type == ROW_TYPE_COMPOSITE_SYNCHRONIZED]

    def get_single(self) -> list:
        return [r for r in self.rows
                if not r.grouping_hypothesis or
                r.grouping_hypothesis.proposed_row_type == ROW_TYPE_SINGLE]

    def stats(self) -> dict:
        total = len(self.rows)
        if total == 0:
            return {'total_rows': 0}

        from collections import Counter
        status_counts = Counter(r.row_status for r in self.rows)
        timing_type_counts = Counter(r.timing_type for r in self.rows)
        relevance_counts = Counter(r.timing_row_relevance for r in self.rows)
        boundary_status_counts = Counter(r.boundary_status for r in self.rows)

        # Part A: timing hypothesis status distribution
        start_hyp_counts = Counter()
        end_hyp_counts = Counter()
        for r in self.rows:
            if r.timing_start:
                start_hyp_counts[r.timing_start.hypothesis_status] += 1
            if r.timing_end:
                end_hyp_counts[r.timing_end.hypothesis_status] += 1

        # Part T: trace readiness distribution
        readiness_counts = Counter(r.trace_readiness for r in self.rows)

        # Part U: observability distribution
        start_obs_counts = Counter(r.start_observability for r in self.rows)
        end_obs_counts = Counter(r.end_observability for r in self.rows)

        # Part R: description quality distribution
        desc_quality_counts = Counter(r.description_quality for r in self.rows)

        # Part I: merge status distribution
        merge_counts = Counter()
        for r in self.rows:
            if r.grouping_hypothesis:
                merge_counts[r.grouping_hypothesis.merge_status] += 1

        # Fallback count
        fallback_count = sum(1 for r in self.rows
                             if r.timing_end and r.timing_end.is_fallback)

        composite_count = len(self.get_composite())
        single_count = len(self.get_single())

        return {
            'total_rows': total,
            'composite_rows': composite_count,
            'single_rows': single_count,
            'row_status_distribution': dict(status_counts),
            'timing_type_distribution': dict(timing_type_counts),
            'relevance_distribution': dict(relevance_counts),
            'boundary_status_distribution': dict(boundary_status_counts),
            'timing_start_hypothesis_distribution': dict(start_hyp_counts),
            'timing_end_hypothesis_distribution': dict(end_hyp_counts),
            'trace_readiness_distribution': dict(readiness_counts),
            'start_observability_distribution': dict(start_obs_counts),
            'end_observability_distribution': dict(end_obs_counts),
            'description_quality_distribution': dict(desc_quality_counts),
            'merge_status_distribution': dict(merge_counts),
            'fallback_count': fallback_count,
        }

    def to_dict(self) -> dict:
        return {
            'rows': [r.to_dict() for r in self.rows],
            'stats': self.stats(),
        }

    def save(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> 'FinalTimingRowStore':
        store = cls()
        for r_dict in d.get('rows', []):
            store.add(FinalTimingRowFact.from_dict(r_dict))
        return store

    @classmethod
    def load(cls, path: str) -> 'FinalTimingRowStore':
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)


# ============================================================
# Action Row Fact — the sole intermediate object before final table
# ============================================================

@dataclass
class ActionRowFact:
    """A single row in the final action table.

    The final five-column table is a projection:
        动作描述  = composed_description
        时长(ms)  = duration_ms       (null in this phase)
        目标时长  = target_duration_ms (null in this phase)
        开始标签  = boundary.start_expr
        结束标签  = boundary.end_expr

    Section 一 field groups:
      1. Row Identity    — who/where/when
      2. Description     — decomposed components + composed text
      3. Boundary        — start/end labels + conditions
      4. Time fields     — null in this phase (Runtime Trace fills later)
      5. Provenance      — per-field (embedded in ProvenancedField)
      6. Review          — row_status / needs_review

    Additional:
      - row_candidate:   true = appears in final table; false = internal logic
      - role / role_status: from Action Role Classifier
      - group_id:        grouping hypothesis ID (for split/merge)
      - synchronized_action_ids: references to synchronized ActionRowFacts
      - source_program_facts: raw action/call element IDs that produced this row

    Instance Bridge vs Action Effect Relation (Section 五):
      A validated Instance Bridge (e.g., stIOMotor[10] -> Y006.10 -> "缩回")
      does NOT automatically validate operation. If the step action is
      stIOMotor[10].bAutoCtrl_P and FB body is unknown, operation stays
      plausible/unresolved. This is tracked in the operation field's
      evidence_level and unresolved_reason.
    """
    # --- 1. Row Identity ---
    row_id: str = ''
    project: str = ''
    state_domain: str = ''
    step_identity: str = ''
    thread: str = ''
    source_unit: str = ''
    source_file: str = ''

    # --- 2. Description ---
    description: DescriptionComponents = field(default_factory=DescriptionComponents)
    composed_description: str = ''

    # --- 3. Boundary ---
    boundary: ActionBoundaryFact = None

    # --- 4. Time fields (null in this phase — Runtime Trace fills later) ---
    duration_ms: float = None
    duration_source: str = ''
    start_timestamp: float = None
    end_timestamp: float = None
    target_duration_ms: float = None
    target_duration_source: str = ''

    # --- 5. Row selection & grouping ---
    row_candidate: bool = False
    role: str = ROLE_UNKNOWN
    role_status: str = ROLE_STATUS_UNRESOLVED
    engineering_relevance: str = RELEVANCE_INTERNAL_ONLY
    group_id: str = ''
    group_type: str = ''  # 'grouped' | 'synchronized' | 'single' | ''
    grouping_hypothesis: ActionGroupHypothesis = None
    synchronized_action_ids: list = field(default_factory=list)

    # --- 6. Review ---
    row_status: str = ROW_UNRESOLVED
    needs_review: bool = False
    review_reason: str = ''

    # --- Metadata ---
    source_program_facts: list = field(default_factory=list)
    created_by: str = ''
    notes: str = ''

    def __post_init__(self):
        if self.row_status not in VALID_ROW_STATUSES:
            raise ValueError('Invalid row_status: {}. Must be one of {}'.format(
                self.row_status, VALID_ROW_STATUSES))
        if self.role not in VALID_ROLES:
            raise ValueError('Invalid role: {}. Must be one of {}'.format(
                self.role, VALID_ROLES))
        if self.role_status not in VALID_ROLE_STATUSES:
            raise ValueError('Invalid role_status: {}. Must be one of {}'.format(
                self.role_status, VALID_ROLE_STATUSES))

    def weakest_link(self) -> str:
        """Return the lowest evidence level across description + boundary."""
        desc_link = self.description.weakest_link()
        boundary_link = self.boundary.evidence_level if self.boundary else EV_NONE
        if EVIDENCE_LEVEL_ORDER.get(boundary_link, 0) < EVIDENCE_LEVEL_ORDER.get(desc_link, 0):
            return boundary_link
        return desc_link

    def is_full_description_resolved(self) -> bool:
        """Check if all key description fields are resolved."""
        return self.description.resolved_count() == self.description.total_count()

    def is_partial_description(self) -> bool:
        """Check if some (but not all) description fields are resolved."""
        count = self.description.resolved_count()
        return 0 < count < self.description.total_count()

    def is_boundary_resolved(self) -> bool:
        """Check if boundary (start + end) is resolved."""
        return self.boundary is not None and self.boundary.is_resolved()

    def to_final_table_row(self) -> dict:
        """Project to the final five-column table row.

        Returns:
            dict with keys: 动作描述, 时长(ms), 目标时长, 开始标签, 结束标签
        """
        start_label = self.boundary.start_expr if self.boundary else ''
        end_label = self.boundary.end_expr if self.boundary else ''
        return {
            '动作描述': self.composed_description,
            '时长(ms)': self.duration_ms,
            '目标时长': self.target_duration_ms,
            '开始标签': start_label,
            '结束标签': end_label,
        }

    def to_dict(self) -> dict:
        return {
            'row_id': self.row_id,
            'project': self.project,
            'state_domain': self.state_domain,
            'step_identity': self.step_identity,
            'thread': self.thread,
            'source_unit': self.source_unit,
            'source_file': self.source_file,
            'description': self.description.to_dict(),
            'composed_description': self.composed_description,
            'boundary': self.boundary.to_dict() if self.boundary else None,
            'duration_ms': self.duration_ms,
            'duration_source': self.duration_source,
            'start_timestamp': self.start_timestamp,
            'end_timestamp': self.end_timestamp,
            'target_duration_ms': self.target_duration_ms,
            'target_duration_source': self.target_duration_source,
            'row_candidate': self.row_candidate,
            'role': self.role,
            'role_status': self.role_status,
            'engineering_relevance': self.engineering_relevance,
            'group_id': self.group_id,
            'group_type': self.group_type,
            'grouping_hypothesis': self.grouping_hypothesis.to_dict() if self.grouping_hypothesis else None,
            'synchronized_action_ids': self.synchronized_action_ids,
            'row_status': self.row_status,
            'needs_review': self.needs_review,
            'review_reason': self.review_reason,
            'source_program_facts': self.source_program_facts,
            'created_by': self.created_by,
            'notes': self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ActionRowFact':
        boundary = None
        if d.get('boundary'):
            boundary = ActionBoundaryFact.from_dict(d['boundary'])
        grouping_hyp = None
        if d.get('grouping_hypothesis'):
            grouping_hyp = ActionGroupHypothesis.from_dict(d['grouping_hypothesis'])
        return cls(
            row_id=d.get('row_id', ''),
            project=d.get('project', ''),
            state_domain=d.get('state_domain', ''),
            step_identity=d.get('step_identity', ''),
            thread=d.get('thread', ''),
            source_unit=d.get('source_unit', ''),
            source_file=d.get('source_file', ''),
            description=DescriptionComponents.from_dict(d.get('description')),
            composed_description=d.get('composed_description', ''),
            boundary=boundary,
            duration_ms=d.get('duration_ms'),
            duration_source=d.get('duration_source', ''),
            start_timestamp=d.get('start_timestamp'),
            end_timestamp=d.get('end_timestamp'),
            target_duration_ms=d.get('target_duration_ms'),
            target_duration_source=d.get('target_duration_source', ''),
            row_candidate=d.get('row_candidate', False),
            role=d.get('role', ROLE_UNKNOWN),
            role_status=d.get('role_status', ROLE_STATUS_UNRESOLVED),
            engineering_relevance=d.get('engineering_relevance', RELEVANCE_INTERNAL_ONLY),
            group_id=d.get('group_id', ''),
            group_type=d.get('group_type', ''),
            grouping_hypothesis=grouping_hyp,
            synchronized_action_ids=d.get('synchronized_action_ids', []),
            row_status=d.get('row_status', ROW_UNRESOLVED),
            needs_review=d.get('needs_review', False),
            review_reason=d.get('review_reason', ''),
            source_program_facts=d.get('source_program_facts', []),
            created_by=d.get('created_by', ''),
            notes=d.get('notes', ''),
        )


# ============================================================
# Action Row Fact Store — collection with query + quality metrics
# ============================================================

@dataclass
class ActionRowFactStore:
    """Collection of ActionRowFacts with query capabilities and quality metrics.

    Provides:
      - Query by project, step, role, status, candidate flag
      - Row Quality Metrics (Section 十四):
          * Description: actor/operation/target/parameter resolved %
          * Boundary: start/end label resolved %, boundary exact/plausible %
          * Row Selection: candidates, excluded, grouped, split, review counts
    """
    rows: list = field(default_factory=list)

    def add(self, row: ActionRowFact):
        """Add a row. Auto-assign row_id if empty."""
        if not row.row_id:
            row.row_id = 'ar_{:04d}'.format(len(self.rows) + 1)
        self.rows.append(row)

    def find_by_project(self, project: str) -> list:
        return [r for r in self.rows if r.project == project]

    def find_by_step(self, step_identity: str) -> list:
        return [r for r in self.rows if r.step_identity == step_identity]

    def find_by_role(self, role: str) -> list:
        return [r for r in self.rows if r.role == role]

    def find_by_role_status(self, role_status: str) -> list:
        return [r for r in self.rows if r.role_status == role_status]

    def find_by_row_status(self, row_status: str) -> list:
        return [r for r in self.rows if r.row_status == row_status]

    def find_by_group(self, group_id: str) -> list:
        return [r for r in self.rows if r.group_id == group_id]

    def get_candidates(self) -> list:
        """Get all row_candidate=True rows (for final table)."""
        return [r for r in self.rows if r.row_candidate]

    def get_excluded(self) -> list:
        """Get all row_candidate=False rows (internal logic)."""
        return [r for r in self.rows if not r.row_candidate]

    def get_review_rows(self) -> list:
        """Get all rows needing review."""
        return [r for r in self.rows if r.needs_review]

    def get_split_rows(self) -> list:
        """Get rows that were split from a single step (group_id with multiple rows per step)."""
        step_groups = {}
        for r in self.rows:
            key = (r.step_identity, r.group_id)
            step_groups.setdefault(key, []).append(r)
        split_ids = set()
        for key, rows in step_groups.items():
            if len(rows) > 1:
                for r in rows:
                    split_ids.add(r.row_id)
        return [r for r in self.rows if r.row_id in split_ids]

    def get_grouped_rows(self) -> list:
        """Get rows that were merged from multiple raw actions."""
        return [r for r in self.rows if len(r.source_program_facts) > 1]

    def stats(self) -> dict:
        """Row Quality Metrics (Section 十四)."""
        total = len(self.rows)
        if total == 0:
            return {'total_rows': 0}

        candidates = self.get_candidates()
        excluded = self.get_excluded()
        review_rows = self.get_review_rows()
        split_rows = self.get_split_rows()
        grouped_rows = self.get_grouped_rows()

        # --- Description metrics ---
        desc_fields = DescriptionComponents.KEY_FIELDS
        field_resolved = {f: 0 for f in desc_fields}
        for r in self.rows:
            for f in desc_fields:
                if r.description.get_field(f).is_resolved():
                    field_resolved[f] += 1

        full_desc = sum(1 for r in self.rows if r.is_full_description_resolved())
        partial_desc = sum(1 for r in self.rows if r.is_partial_description())

        # --- Boundary metrics ---
        start_resolved = sum(1 for r in self.rows
                             if r.boundary and r.boundary.start_expr)
        end_resolved = sum(1 for r in self.rows
                           if r.boundary and r.boundary.end_expr)
        boundary_exact = sum(1 for r in self.rows
                             if r.boundary and r.boundary.evidence_level == EV_EXACT)
        boundary_plausible = sum(1 for r in self.rows
                                 if r.boundary and r.boundary.evidence_level in (EV_STRUCTURAL, EV_CONTEXTUAL))
        boundary_unresolved = sum(1 for r in self.rows
                                  if r.boundary and r.boundary.boundary_type == BOUNDARY_UNRESOLVED)

        # End label type distribution
        end_type_counts = {}
        for r in self.rows:
            if r.boundary and r.boundary.end_label_type:
                t = r.boundary.end_label_type
                end_type_counts[t] = end_type_counts.get(t, 0) + 1

        # Semantic role distribution (Part B.6)
        semantic_role_counts = {}
        for r in self.rows:
            if r.boundary and r.boundary.semantic_role:
                sr = r.boundary.semantic_role
                semantic_role_counts[sr] = semantic_role_counts.get(sr, 0) + 1

        # Boundary count transparency (Part B.5)
        rows_with_single_boundary = sum(1 for r in self.rows
                                        if r.boundary and not r.boundary.alternatives)
        rows_with_multiple_alternatives = sum(1 for r in self.rows
                                              if r.boundary and len(r.boundary.alternatives) > 0)
        raw_boundary_candidate_count = sum(len(r.boundary.alternatives) + 1
                                           for r in self.rows if r.boundary)
        selected_boundary_count = sum(1 for r in self.rows
                                      if r.boundary and r.boundary.end_expr)
        rows_with_resolved_boundary = sum(1 for r in self.rows if r.is_boundary_resolved())

        # --- Description metrics: separate program identity vs engineering semantic ---
        prog_fields = DescriptionComponents.PROGRAM_IDENTITY_FIELDS
        eng_fields = DescriptionComponents.ENGINEERING_SEMANTIC_FIELDS
        prog_resolved = {f: 0 for f in prog_fields}
        eng_resolved = {f: 0 for f in eng_fields}
        for r in self.rows:
            for f in prog_fields:
                if r.description.get_field(f).is_resolved():
                    prog_resolved[f] += 1
            for f in eng_fields:
                if r.description.get_field(f).is_resolved():
                    eng_resolved[f] += 1

        # --- Row status distribution ---
        status_counts = {}
        for r in self.rows:
            status_counts[r.row_status] = status_counts.get(r.row_status, 0) + 1

        # --- Role distribution ---
        role_counts = {}
        for r in self.rows:
            role_counts[r.role] = role_counts.get(r.role, 0) + 1

        def pct(n):
            return round(100.0 * n / total, 1) if total > 0 else 0.0

        return {
            'total_rows': total,
            'description': {
                'program_identity_resolved_pct': {f: pct(prog_resolved[f]) for f in prog_fields},
                'engineering_semantic_resolved_pct': {f: pct(eng_resolved[f]) for f in eng_fields},
                'full_description_resolved_pct': pct(full_desc),
                'partial_description_pct': pct(partial_desc),
                'unresolved_description_pct': pct(total - full_desc - partial_desc),
            },
            'boundary': {
                'start_label_resolved_pct': pct(start_resolved),
                'end_label_resolved_pct': pct(end_resolved),
                'boundary_exact_pct': pct(boundary_exact),
                'boundary_plausible_pct': pct(boundary_plausible),
                'boundary_unresolved_pct': pct(boundary_unresolved),
                'end_label_type_distribution': end_type_counts,
                'semantic_role_distribution': semantic_role_counts,
                'rows_with_single_boundary': rows_with_single_boundary,
                'rows_with_multiple_alternatives': rows_with_multiple_alternatives,
                'raw_boundary_candidate_count': raw_boundary_candidate_count,
                'selected_boundary_count': selected_boundary_count,
                'rows_with_resolved_boundary': rows_with_resolved_boundary,
                'boundary_count_equals_resolved': selected_boundary_count == rows_with_resolved_boundary,
            },
            'row_selection': {
                'total_program_action_candidates': total,
                'final_row_candidates': len(candidates),
                'excluded_internal_logic': len(excluded),
                'grouped_rows': len(grouped_rows),
                'split_rows': len(split_rows),
                'review_rows': len(review_rows),
            },
            'row_status_distribution': status_counts,
            'role_distribution': role_counts,
        }

    def to_dict(self) -> dict:
        return {
            'rows': [r.to_dict() for r in self.rows],
            'stats': self.stats(),
        }

    def save(self, path: str):
        """Save to JSON file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> 'ActionRowFactStore':
        store = cls()
        for r_dict in d.get('rows', []):
            store.add(ActionRowFact.from_dict(r_dict))
        return store

    @classmethod
    def load(cls, path: str) -> 'ActionRowFactStore':
        """Load from JSON file."""
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)


# ============================================================
# Helper: Create a ProvenancedField with exact evidence
# ============================================================

def exact_field(value: Any, evidence_path: list = None,
                supporting_sources: list = None) -> ProvenancedField:
    """Create a ProvenancedField with exact evidence level."""
    return ProvenancedField(
        value=value,
        evidence_level=EV_EXACT,
        evidence_path=evidence_path or [],
        supporting_sources=supporting_sources or [],
    )


def unresolved_field(reason: str = '') -> ProvenancedField:
    """Create an unresolved ProvenancedField."""
    return ProvenancedField(
        value=None,
        evidence_level=EV_NONE,
        unresolved_reason=reason,
    )


def plausible_field(value: Any, evidence_path: list = None,
                    supporting_sources: list = None,
                    unresolved_reason: str = '') -> ProvenancedField:
    """Create a ProvenancedField with structural/contextual evidence."""
    return ProvenancedField(
        value=value,
        evidence_level=EV_STRUCTURAL,
        evidence_path=evidence_path or [],
        supporting_sources=supporting_sources or [],
        unresolved_reason=unresolved_reason,
    )
