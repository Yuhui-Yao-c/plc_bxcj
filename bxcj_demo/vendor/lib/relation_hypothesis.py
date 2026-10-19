#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/relation_hypothesis.py — RelationHypothesis Schema

Format-agnostic schema for proposing, validating, and storing evidence
relation hypotheses. Not bound to any specific project or PLC format.

A RelationHypothesis represents a proposed connection between two entities
in the evidence graph that has NOT yet been validated. Only validated
hypotheses (status='validated') are written to EvidenceGraph as Relations.

Lifecycle:
    candidate → plausible → validated
                      ↘ ambiguous
                      ↘ rejected

Design principles:
    - opencode proposes hypotheses based on evidence observation
    - Python provides validation infrastructure (graph query, pattern matching)
    - Only validated relations enter EvidenceGraph
    - Co-occurrence is NOT a relation — only a retrieval hint

Usage:
    hyp = RelationHypothesis(
        source_entity='plc_variable:Vacuum[19].HMIStrOut',
        target_entity='hmi_label:Y006.10',
        proposed_relation='maps_to',
        relation_pattern='fb_parameter_binding',
        source_scope='action',
        ...
    )
    hyp.status = 'validated'  # after validation
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path


# ============================================================
# Relation Hypothesis Status
# ============================================================

STATUS_CANDIDATE = 'candidate'      # Initial proposal, no validation
STATUS_PLAUSIBLE = 'plausible'      # Some supporting evidence, not fully validated
STATUS_VALIDATED = 'validated'      # Fully validated, can enter EvidenceGraph
STATUS_AMBIGUOUS = 'ambiguous'      # Conflicting evidence, cannot resolve
STATUS_REJECTED = 'rejected'        # Evidence contradicts the hypothesis

VALID_STATUSES = {
    STATUS_CANDIDATE, STATUS_PLAUSIBLE, STATUS_VALIDATED,
    STATUS_AMBIGUOUS, STATUS_REJECTED,
}

# ============================================================
# Resolution Status (instance-level target resolution)
# ============================================================

RES_UNIQUE = 'unique'                # One-to-one: source maps to exactly one target
RES_ONE_TO_MANY = 'one_to_many'      # Source maps to multiple targets (all valid)
RES_MANY_TO_ONE = 'many_to_one'      # Multiple sources map to same target
RES_AMBIGUOUS_TARGET = 'ambiguous_target'  # Cannot determine which specific target
RES_UNRESOLVED = 'unresolved'        # No target identified

VALID_RESOLUTION_STATUSES = {
    RES_UNIQUE, RES_ONE_TO_MANY, RES_MANY_TO_ONE,
    RES_AMBIGUOUS_TARGET, RES_UNRESOLVED,
}


# ============================================================
# Relation Hypothesis
# ============================================================

@dataclass
class RelationHypothesis:
    """A proposed evidence relation between two entities.

    Fields:
        source_entity       — Entity ID of the source (e.g., 'plc_variable:Vacuum[19].HMIStrOut')
        target_entity       — Entity ID of the target (e.g., 'hmi_label:Y006.10')
        proposed_relation   — Relation type (e.g., 'maps_to', 'labelled_as', 'documented_as')
        relation_pattern    — How this relation was discovered:
                              - 'direct_ec_mapping'      — EC entry directly maps variable to address
                              - 'fb_parameter_binding'   — FB call parameter connects variables
                              - 'variable_flow'          — VariableFlowEdge connects definitions/uses
                              - 'assignment_chain'       — Assignment chain connects variables
                              - 'hmi_label_lookup'       — Physical address has HMI label
                              - 'co_occurrence_hint'     — Co-occurrence suggested the relation (NOT validated)
                              - 'documentation_text'     — Documentation text provides semantic
                              - 'manual_proposal'        — opencode manually proposed
        source_scope        — Context where the relation was discovered:
                              - 'action'                 — From a StepGraph action
                              - 'guard'                  — From a StepGraph guard
                              - 'fb_call'                — From FB call parameter analysis
                              - 'variable_declaration'   — From variable declaration
                              - 'ec_entry'               — From EC entry
                              - 'hmi_component'          — From HMI component
        supporting_evidence — List of evidence supporting the hypothesis
        conflicting_evidence— List of evidence contradicting the hypothesis
        candidate_paths     — List of candidate paths (for multi-hop relations)
        validation_strategy — How to validate this hypothesis
        positive_samples    — Samples that confirm the relation
        negative_samples    — Samples that contradict the relation
        holdout_samples     — Samples reserved for holdout validation
        confidence          — Confidence score (0.0 to 1.0)
        hypothesis_status   — Pattern-level status (candidate/plausible/validated/ambiguous/rejected)
        resolution_status   — Instance-level target resolution:
                              - 'unique'             — One-to-one mapping confirmed
                              - 'one_to_many'        — Source maps to multiple targets (all valid)
                              - 'many_to_one'        — Multiple sources map to same target
                              - 'ambiguous_target'   — Cannot determine which specific target
                              - 'unresolved'         — No target identified
    """
    source_entity: str = ''
    target_entity: str = ''
    proposed_relation: str = ''
    relation_pattern: str = ''
    source_scope: str = ''

    supporting_evidence: list = field(default_factory=list)
    conflicting_evidence: list = field(default_factory=list)
    candidate_paths: list = field(default_factory=list)
    validation_strategy: str = ''

    positive_samples: list = field(default_factory=list)
    negative_samples: list = field(default_factory=list)
    holdout_samples: list = field(default_factory=list)

    confidence: float = 0.0
    hypothesis_status: str = STATUS_CANDIDATE
    resolution_status: str = RES_UNRESOLVED

    # Optional metadata
    hypothesis_id: str = ''
    created_by: str = ''  # 'opencode' or 'python_tool'
    notes: str = ''

    def __post_init__(self):
        if self.hypothesis_status not in VALID_STATUSES:
            raise ValueError('Invalid hypothesis_status: {}. Must be one of {}'.format(
                self.hypothesis_status, VALID_STATUSES))
        if self.resolution_status not in VALID_RESOLUTION_STATUSES:
            raise ValueError('Invalid resolution_status: {}. Must be one of {}'.format(
                self.resolution_status, VALID_RESOLUTION_STATUSES))

    def to_dict(self) -> dict:
        return {
            'hypothesis_id': self.hypothesis_id,
            'source_entity': self.source_entity,
            'target_entity': self.target_entity,
            'proposed_relation': self.proposed_relation,
            'relation_pattern': self.relation_pattern,
            'source_scope': self.source_scope,
            'supporting_evidence': self.supporting_evidence,
            'conflicting_evidence': self.conflicting_evidence,
            'candidate_paths': self.candidate_paths,
            'validation_strategy': self.validation_strategy,
            'positive_samples': self.positive_samples,
            'negative_samples': self.negative_samples,
            'holdout_samples': self.holdout_samples,
            'confidence': self.confidence,
            'hypothesis_status': self.hypothesis_status,
            'resolution_status': self.resolution_status,
            'created_by': self.created_by,
            'notes': self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'RelationHypothesis':
        return cls(
            hypothesis_id=d.get('hypothesis_id', ''),
            source_entity=d.get('source_entity', ''),
            target_entity=d.get('target_entity', ''),
            proposed_relation=d.get('proposed_relation', ''),
            relation_pattern=d.get('relation_pattern', ''),
            source_scope=d.get('source_scope', ''),
            supporting_evidence=d.get('supporting_evidence', []),
            conflicting_evidence=d.get('conflicting_evidence', []),
            candidate_paths=d.get('candidate_paths', []),
            validation_strategy=d.get('validation_strategy', ''),
            positive_samples=d.get('positive_samples', []),
            negative_samples=d.get('negative_samples', []),
            holdout_samples=d.get('holdout_samples', []),
            confidence=d.get('confidence', 0.0),
            hypothesis_status=d.get('hypothesis_status', d.get('status', STATUS_CANDIDATE)),
            resolution_status=d.get('resolution_status', RES_UNRESOLVED),
            created_by=d.get('created_by', ''),
            notes=d.get('notes', ''),
        )

    def add_supporting_evidence(self, evidence_type: str, detail: str,
                                 source: str = '', data: dict = None):
        """Add supporting evidence."""
        self.supporting_evidence.append({
            'type': evidence_type,
            'detail': detail,
            'source': source,
            'data': data or {},
        })

    def add_conflicting_evidence(self, evidence_type: str, detail: str,
                                  source: str = '', data: dict = None):
        """Add conflicting evidence."""
        self.conflicting_evidence.append({
            'type': evidence_type,
            'detail': detail,
            'source': source,
            'data': data or {},
        })

    def add_candidate_path(self, path: list):
        """Add a candidate path (list of entity/relation hops).

        Example:
            [
                {'entity': 'plc_variable:Vacuum[19].HMIStrOut'},
                {'relation': 'fb_output_param', 'detail': 'FB output parameter'},
                {'entity': 'fb_instance:twoCyControl_123'},
                {'relation': 'fb_internal_assignment', 'detail': 'FB internal assignment'},
                {'entity': 'plc_variable:m_bOutput[106]'},
                {'relation': 'maps_to', 'detail': 'EC mapping'},
                {'entity': 'physical_address:Y006.10'},
                {'relation': 'labelled_as', 'detail': 'HMI Y label'},
                {'entity': 'hmi_label:S11AML上平移取料上升'},
            ]
        """
        self.candidate_paths.append(path)

    def can_enter_evidence_graph(self) -> bool:
        """Check if this hypothesis is validated and can enter EvidenceGraph."""
        return (self.hypothesis_status == STATUS_VALIDATED
                and bool(self.source_entity) and bool(self.target_entity))

    def to_relation(self):
        """Convert a validated hypothesis to an EvidenceGraph Relation.

        Only call this after status == 'validated'.
        """
        if not self.can_enter_evidence_graph():
            raise ValueError('Hypothesis must be validated before converting to Relation')

        from .evidence_model import Relation
        return Relation(
            source_id=self.source_entity,
            target_id=self.target_entity,
            relation_type=self.proposed_relation,
            confidence=self._confidence_to_evidence_level(),
            evidence_source=self.relation_pattern,
            evidence_detail='; '.join(
                e.get('detail', '') for e in self.supporting_evidence[:3]
            ),
        )

    def _confidence_to_evidence_level(self) -> str:
        """Map confidence score to evidence level."""
        if self.confidence >= 0.9:
            return 'exact'
        elif self.confidence >= 0.7:
            return 'structural'
        elif self.confidence >= 0.5:
            return 'contextual'
        else:
            return 'llm_inferred'


# ============================================================
# Relation Hypothesis Store
# ============================================================

@dataclass
class RelationHypothesisStore:
    """Collection of RelationHypotheses with query capabilities."""
    hypotheses: list = field(default_factory=list)

    def add(self, hyp: RelationHypothesis):
        """Add a hypothesis."""
        if not hyp.hypothesis_id:
            hyp.hypothesis_id = 'rh_{:04d}'.format(len(self.hypotheses) + 1)
        self.hypotheses.append(hyp)

    def find_by_source(self, source_entity: str,
                       proposed_relation: str = None) -> list:
        """Find hypotheses by source entity."""
        results = []
        for h in self.hypotheses:
            if h.source_entity != source_entity:
                continue
            if proposed_relation and h.proposed_relation != proposed_relation:
                continue
            results.append(h)
        return results

    def find_by_target(self, target_entity: str,
                       proposed_relation: str = None) -> list:
        """Find hypotheses by target entity."""
        results = []
        for h in self.hypotheses:
            if h.target_entity != target_entity:
                continue
            if proposed_relation and h.proposed_relation != proposed_relation:
                continue
            results.append(h)
        return results

    def find_by_pattern(self, relation_pattern: str) -> list:
        """Find hypotheses by relation pattern."""
        return [h for h in self.hypotheses if h.relation_pattern == relation_pattern]

    def find_by_status(self, hypothesis_status: str) -> list:
        """Find hypotheses by hypothesis_status."""
        return [h for h in self.hypotheses if h.hypothesis_status == hypothesis_status]

    def find_by_resolution(self, resolution_status: str) -> list:
        """Find hypotheses by resolution_status."""
        return [h for h in self.hypotheses if h.resolution_status == resolution_status]

    def find_by_scope(self, source_scope: str) -> list:
        """Find hypotheses by source scope."""
        return [h for h in self.hypotheses if h.source_scope == source_scope]

    def find_path(self, source_entity: str, target_entity: str) -> list:
        """Find all hypotheses connecting source to target (any relation type)."""
        return [h for h in self.hypotheses
                if h.source_entity == source_entity
                and h.target_entity == target_entity]

    def get_validated(self) -> list:
        """Get all validated hypotheses ready for EvidenceGraph."""
        return [h for h in self.hypotheses if h.can_enter_evidence_graph()]

    def stats(self) -> dict:
        """Statistics."""
        status_counts = {}
        resolution_counts = {}
        pattern_counts = {}
        relation_counts = {}
        for h in self.hypotheses:
            status_counts[h.hypothesis_status] = status_counts.get(h.hypothesis_status, 0) + 1
            resolution_counts[h.resolution_status] = resolution_counts.get(h.resolution_status, 0) + 1
            pattern_counts[h.relation_pattern] = pattern_counts.get(h.relation_pattern, 0) + 1
            relation_counts[h.proposed_relation] = relation_counts.get(h.proposed_relation, 0) + 1
        return {
            'total_hypotheses': len(self.hypotheses),
            'by_hypothesis_status': status_counts,
            'by_resolution_status': resolution_counts,
            'by_pattern': pattern_counts,
            'by_relation': relation_counts,
            'validated_count': len(self.get_validated()),
        }

    def to_dict(self) -> dict:
        return {
            'hypotheses': [h.to_dict() for h in self.hypotheses],
            'stats': self.stats(),
        }

    def save(self, path: str):
        """Save to JSON file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> 'RelationHypothesisStore':
        store = cls()
        for h_dict in d.get('hypotheses', []):
            store.add(RelationHypothesis.from_dict(h_dict))
        return store

    @classmethod
    def load(cls, path: str) -> 'RelationHypothesisStore':
        """Load from JSON file."""
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)


# ============================================================
# Helper: Create standard entity IDs
# ============================================================

def make_entity_id(entity_type: str, key: str, scope: str = '') -> str:
    """Create a standard entity ID for hypothesis source/target.

    This is a convenience wrapper around evidence_model.make_entity_id
    to avoid circular imports in some cases.
    """
    if scope:
        safe_scope = scope.replace(':', '/')
        return '{}:{}/{}'.format(entity_type, safe_scope, key)
    return '{}:{}'.format(entity_type, key)
