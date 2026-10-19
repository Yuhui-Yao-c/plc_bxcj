#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/workflow_failure_model.py — Exception-Driven Human Intervention Data Models

Implements the minimal data structures for the closed loop:

    Workflow Failure → Diagnosis → Self-Recovery Attempt
        → (if blocked) ChoiceQuestion → EngineerChoice
        → InterpretationHypothesis → Validation → Spec Update → Resume

Design principles:
    - These are data containers + serialization, NOT a framework.
    - opencode (the harness) orchestrates the loop; Python provides
      deterministic collection, clustering, and state tracking.
    - All state is persisted as JSON files in output/ for opencode to read.
    - No database, no message queue, no multi-agent service.

Trigger taxonomy (from spec):
    A — Source parsing or step discovery blocked
    B — Engineering semantic evidence chain cannot be established
    C — Program control flow cannot be reliably recovered
    D — FinalTimingRow generation has systemic problems
    E — Critical engineering resources missing
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ============================================================
# Trigger Types
# ============================================================

TRIGGER_A_STEP_DISCOVERY = 'A_step_discovery'
TRIGGER_B_SEMANTIC_EVIDENCE = 'B_semantic_evidence'
TRIGGER_C_CONTROL_FLOW = 'C_control_flow'
TRIGGER_D_TIMING_ROW = 'D_timing_row'
TRIGGER_E_RESOURCE_MISSING = 'E_resource_missing'

VALID_TRIGGERS = {
    TRIGGER_A_STEP_DISCOVERY,
    TRIGGER_B_SEMANTIC_EVIDENCE,
    TRIGGER_C_CONTROL_FLOW,
    TRIGGER_D_TIMING_ROW,
    TRIGGER_E_RESOURCE_MISSING,
}

TRIGGER_LABELS = {
    TRIGGER_A_STEP_DISCOVERY: '源码解析或步序发现受阻',
    TRIGGER_B_SEMANTIC_EVIDENCE: '工程语义证据链无法建立',
    TRIGGER_C_CONTROL_FLOW: '程序控制流无法可靠恢复',
    TRIGGER_D_TIMING_ROW: 'FinalTimingRow生成出现系统性问题',
    TRIGGER_E_RESOURCE_MISSING: '关键工程资源缺失',
}


# ============================================================
# Status Constants
# ============================================================

FAILURE_OPEN = 'open'
FAILURE_AUTO_RECOVERED = 'auto_recovered'
FAILURE_HUMAN_RESOLVED = 'human_resolved'
FAILURE_UNRESOLVED = 'unresolved'
FAILURE_SKIPPED = 'skipped'

QUESTION_NOT_ASKED = 'not_asked'
QUESTION_PENDING = 'pending'
QUESTION_ASKED = 'asked'
QUESTION_ANSWERED = 'answered'
QUESTION_SKIPPED = 'skipped'
QUESTION_SIMULATED = 'simulated_answer'
QUESTION_WITHDRAWN = 'withdrawn'
QUESTION_SUPERSEDED = 'superseded'
QUESTION_TIMED_OUT = 'timed_out'

HYP_PENDING = 'pending_verification'
HYP_VALIDATED = 'validated'
HYP_REFUTED = 'refuted'
HYP_PARTIAL = 'partially_validated'
HYP_INSUFFICIENT_EVIDENCE = 'insufficient_evidence'
HYP_NOT_APPLICABLE = 'not_applicable'
HYP_WITHDRAWN = 'withdrawn'

AUTO_REPAIR_SUCCESS = 'success'
AUTO_REPAIR_FAILED = 'failed'
AUTO_REPAIR_NOT_ATTEMPTED = 'not_attempted'


# ============================================================
# Individual Failure Instance
# ============================================================

@dataclass
class FailureInstance:
    """A single concrete failure detected from an audit output."""

    failure_id: str = ''
    trigger_type: str = ''
    category: str = ''
    source_file: str = ''
    description: str = ''
    affected_count: int = 0
    affected_items: list = field(default_factory=list)
    evidence_snapshot: dict = field(default_factory=dict)
    auto_repair_attempted: str = AUTO_REPAIR_NOT_ATTEMPTED
    auto_repair_detail: str = ''

    def to_dict(self) -> dict:
        return {
            'failure_id': self.failure_id,
            'trigger_type': self.trigger_type,
            'category': self.category,
            'source_file': self.source_file,
            'description': self.description,
            'affected_count': self.affected_count,
            'affected_items': self.affected_items,
            'evidence_snapshot': self.evidence_snapshot,
            'auto_repair_attempted': self.auto_repair_attempted,
            'auto_repair_detail': self.auto_repair_detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'FailureInstance':
        return cls(
            failure_id=d.get('failure_id', ''),
            trigger_type=d.get('trigger_type', ''),
            category=d.get('category', ''),
            source_file=d.get('source_file', ''),
            description=d.get('description', ''),
            affected_count=d.get('affected_count', 0),
            affected_items=d.get('affected_items', []),
            evidence_snapshot=d.get('evidence_snapshot', {}),
            auto_repair_attempted=d.get('auto_repair_attempted', AUTO_REPAIR_NOT_ATTEMPTED),
            auto_repair_detail=d.get('auto_repair_detail', ''),
        )


# ============================================================
# Failure Cluster (aggregated root cause)
# ============================================================

@dataclass
class FailureCluster:
    """A cluster of failures sharing a common root cause."""

    cluster_id: str = ''
    trigger_type: str = ''
    root_cause_summary: str = ''
    failure_ids: list = field(default_factory=list)
    affected_scope: dict = field(default_factory=dict)
    shared_evidence: dict = field(default_factory=dict)
    auto_repair_attempts: list = field(default_factory=list)
    auto_repair_succeeded: bool = False
    status: str = FAILURE_OPEN

    @property
    def affected_count(self) -> int:
        return self.affected_scope.get('total_instances', 0)

    def to_dict(self) -> dict:
        return {
            'cluster_id': self.cluster_id,
            'trigger_type': self.trigger_type,
            'root_cause_summary': self.root_cause_summary,
            'failure_ids': self.failure_ids,
            'affected_scope': self.affected_scope,
            'shared_evidence': self.shared_evidence,
            'auto_repair_attempts': self.auto_repair_attempts,
            'auto_repair_succeeded': self.auto_repair_succeeded,
            'status': self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'FailureCluster':
        return cls(
            cluster_id=d.get('cluster_id', ''),
            trigger_type=d.get('trigger_type', ''),
            root_cause_summary=d.get('root_cause_summary', ''),
            failure_ids=d.get('failure_ids', []),
            affected_scope=d.get('affected_scope', {}),
            shared_evidence=d.get('shared_evidence', {}),
            auto_repair_attempts=d.get('auto_repair_attempts', []),
            auto_repair_succeeded=d.get('auto_repair_succeeded', False),
            status=d.get('status', FAILURE_OPEN),
        )


# ============================================================
# Workflow Failure Report (top-level container)
# ============================================================

@dataclass
class WorkflowFailureReport:
    """Top-level report aggregating all failures in a workflow run."""

    project: str = ''
    timestamp: str = ''
    workflow_stage: str = ''
    failures: list = field(default_factory=list)
    clusters: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'project': self.project,
            'timestamp': self.timestamp,
            'workflow_stage': self.workflow_stage,
            'failures': [f.to_dict() if isinstance(f, FailureInstance) else f for f in self.failures],
            'clusters': [c.to_dict() if isinstance(c, FailureCluster) else c for c in self.clusters],
            'summary': self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'WorkflowFailureReport':
        return cls(
            project=d.get('project', ''),
            timestamp=d.get('timestamp', ''),
            workflow_stage=d.get('workflow_stage', ''),
            failures=[FailureInstance.from_dict(f) for f in d.get('failures', [])],
            clusters=[FailureCluster.from_dict(c) for c in d.get('clusters', [])],
            summary=d.get('summary', {}),
        )

    def save(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> 'WorkflowFailureReport':
        with open(path, 'r', encoding='utf-8') as f:
            return cls.from_dict(json.load(f))


# ============================================================
# Choice Question
# ============================================================

@dataclass
class QuestionOption:
    """A single option in a choice question."""

    option_id: str = ''
    text: str = ''
    interpretation_hint: str = ''
    expected_scope: str = ''

    def to_dict(self) -> dict:
        return {
            'option_id': self.option_id,
            'text': self.text,
            'interpretation_hint': self.interpretation_hint,
            'expected_scope': self.expected_scope,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'QuestionOption':
        return cls(
            option_id=d.get('option_id', ''),
            text=d.get('text', ''),
            interpretation_hint=d.get('interpretation_hint', ''),
            expected_scope=d.get('expected_scope', ''),
        )


@dataclass
class ChoiceQuestion:
    """A structured question presented to the engineer.

    Generated dynamically from runtime evidence, not from a fixed template.
    Must include 'uncertain' and 'none_of_above' options.
    """

    question_id: str = ''
    triggering_cluster_id: str = ''
    trigger_type: str = ''
    root_cause_cluster: str = ''
    question_text: str = ''
    problem_summary: str = ''
    auto_investigation_summary: str = ''
    options: list = field(default_factory=list)
    supporting_evidence: list = field(default_factory=list)
    recommended_option: str = ''
    recommended_reason: str = ''
    affected_scope: dict = field(default_factory=dict)
    expected_impact: str = ''
    can_skip: bool = True
    status: str = QUESTION_PENDING
    created_at: str = ''
    answered_at: str = ''

    def to_dict(self) -> dict:
        return {
            'question_id': self.question_id,
            'triggering_cluster_id': self.triggering_cluster_id,
            'trigger_type': self.trigger_type,
            'root_cause_cluster': self.root_cause_cluster,
            'question_text': self.question_text,
            'problem_summary': self.problem_summary,
            'auto_investigation_summary': self.auto_investigation_summary,
            'options': [o.to_dict() if isinstance(o, QuestionOption) else o for o in self.options],
            'supporting_evidence': self.supporting_evidence,
            'recommended_option': self.recommended_option,
            'recommended_reason': self.recommended_reason,
            'affected_scope': self.affected_scope,
            'expected_impact': self.expected_impact,
            'can_skip': self.can_skip,
            'status': self.status,
            'created_at': self.created_at,
            'answered_at': self.answered_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ChoiceQuestion':
        return cls(
            question_id=d.get('question_id', ''),
            triggering_cluster_id=d.get('triggering_cluster_id', ''),
            trigger_type=d.get('trigger_type', ''),
            root_cause_cluster=d.get('root_cause_cluster', ''),
            question_text=d.get('question_text', ''),
            problem_summary=d.get('problem_summary', ''),
            auto_investigation_summary=d.get('auto_investigation_summary', ''),
            options=[QuestionOption.from_dict(o) for o in d.get('options', [])],
            supporting_evidence=d.get('supporting_evidence', []),
            recommended_option=d.get('recommended_option', ''),
            recommended_reason=d.get('recommended_reason', ''),
            affected_scope=d.get('affected_scope', {}),
            expected_impact=d.get('expected_impact', ''),
            can_skip=d.get('can_skip', True),
            status=d.get('status', QUESTION_PENDING),
            created_at=d.get('created_at', ''),
            answered_at=d.get('answered_at', ''),
        )

    def save(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> 'ChoiceQuestion':
        with open(path, 'r', encoding='utf-8') as f:
            return cls.from_dict(json.load(f))


# ============================================================
# Engineer Choice (answer to a ChoiceQuestion)
# ============================================================

@dataclass
class EngineerChoice:
    """The engineer's answer to a ChoiceQuestion."""

    question_id: str = ''
    selected_option: str = ''
    optional_short_note: str = ''
    decision_scope: str = ''
    timestamp: str = ''
    hypothesis_id: str = ''

    def to_dict(self) -> dict:
        return {
            'question_id': self.question_id,
            'selected_option': self.selected_option,
            'optional_short_note': self.optional_short_note,
            'decision_scope': self.decision_scope,
            'timestamp': self.timestamp,
            'hypothesis_id': self.hypothesis_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'EngineerChoice':
        return cls(
            question_id=d.get('question_id', ''),
            selected_option=d.get('selected_option', ''),
            optional_short_note=d.get('optional_short_note', ''),
            decision_scope=d.get('decision_scope', ''),
            timestamp=d.get('timestamp', ''),
            hypothesis_id=d.get('hypothesis_id', ''),
        )

    def save(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


# ============================================================
# Interpretation Hypothesis
# ============================================================

@dataclass
class InterpretationHypothesis:
    """An engineering interpretation derived from an engineer's choice.

    The engineer's choice is guidance, NOT a direct fact.
    opencode must verify this hypothesis against source evidence
    before applying it to specs or workflow.
    """

    hypothesis_id: str = ''
    source_question_id: str = ''
    source_choice: str = ''
    hypothesis_text: str = ''
    scope: dict = field(default_factory=dict)
    status: str = HYP_PENDING
    supporting_evidence: list = field(default_factory=list)
    conflicting_evidence: list = field(default_factory=list)
    validation_result: dict = field(default_factory=dict)
    spec_update: dict = field(default_factory=dict)
    created_at: str = ''
    validated_at: str = ''

    def to_dict(self) -> dict:
        return {
            'hypothesis_id': self.hypothesis_id,
            'source_question_id': self.source_question_id,
            'source_choice': self.source_choice,
            'hypothesis_text': self.hypothesis_text,
            'scope': self.scope,
            'status': self.status,
            'supporting_evidence': self.supporting_evidence,
            'conflicting_evidence': self.conflicting_evidence,
            'validation_result': self.validation_result,
            'spec_update': self.spec_update,
            'created_at': self.created_at,
            'validated_at': self.validated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'InterpretationHypothesis':
        return cls(
            hypothesis_id=d.get('hypothesis_id', ''),
            source_question_id=d.get('source_question_id', ''),
            source_choice=d.get('source_choice', ''),
            hypothesis_text=d.get('hypothesis_text', ''),
            scope=d.get('scope', {}),
            status=d.get('status', HYP_PENDING),
            supporting_evidence=d.get('supporting_evidence', []),
            conflicting_evidence=d.get('conflicting_evidence', []),
            validation_result=d.get('validation_result', {}),
            spec_update=d.get('spec_update', {}),
            created_at=d.get('created_at', ''),
            validated_at=d.get('validated_at', ''),
        )


# ============================================================
# Utility Functions
# ============================================================

def generate_id(prefix: str = '') -> str:
    """Generate a unique ID with optional prefix."""
    short_uuid = uuid.uuid4().hex[:8]
    return '{}_{}'.format(prefix, short_uuid) if prefix else short_uuid


def now_iso() -> str:
    """Current timestamp in ISO format."""
    return datetime.now().isoformat()


def save_json(data: Any, path: str):
    """Save any serializable object as JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        if hasattr(data, 'to_dict'):
            json.dump(data.to_dict(), f, ensure_ascii=False, indent=2)
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path: str) -> Any:
    """Load JSON from file."""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)
