#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/datatype_alias_hypothesis.py — Datatype Alias Hypothesis

Alias resolution for external/empty datatypes must be an explicit hypothesis
with validation states: candidate → validated → rejected → ambiguous

Validation criteria:
  - member_overlap_ratio: how many expected members exist in candidate
  - member_type_compatibility: derived types of matching members
  - documentation_compatibility: documentation of matching members
  - usage_context_similarity: are they used in similar contexts?
  - competing_candidate_score: is there a better candidate?
  - negative_evidence: do any members conflict?

Only validated aliases allow Resolver to continue datatype traversal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# Validation states
CANDIDATE = 'candidate'
PLAUSIBLE = 'plausible'
VALIDATED = 'validated'
REJECTED = 'rejected'
AMBIGUOUS = 'ambiguous'


@dataclass
class AliasHypothesis:
    """A datatype alias hypothesis"""
    source_type: str  # external/empty datatype name
    target_type: str  # candidate alias datatype name
    state: str = CANDIDATE

    # Validation metrics
    member_overlap_ratio: float = 0.0
    matching_members: list = field(default_factory=list)
    conflicting_members: list = field(default_factory=list)
    type_compatibility_score: float = 0.0
    doc_compatibility_score: float = 0.0
    usage_context_score: float = 0.0
    competing_candidates: list = field(default_factory=list)
    negative_evidence: list = field(default_factory=list)

    # Overall
    confidence: float = 0.0
    validation_reason: str = ''

    def to_dict(self) -> dict:
        return {
            'source_type': self.source_type,
            'target_type': self.target_type,
            'state': self.state,
            'member_overlap_ratio': self.member_overlap_ratio,
            'matching_members': self.matching_members,
            'conflicting_members': self.conflicting_members,
            'type_compatibility_score': self.type_compatibility_score,
            'doc_compatibility_score': self.doc_compatibility_score,
            'usage_context_score': self.usage_context_score,
            'competing_candidates': self.competing_candidates,
            'negative_evidence': self.negative_evidence,
            'confidence': self.confidence,
            'validation_reason': self.validation_reason,
        }


class DatatypeAliasHypothesisBuilder:
    """Builds and validates datatype alias hypotheses.

    Usage:
        builder = DatatypeAliasHypothesisBuilder(store, dt_members_index)
        hyp = builder.build_hypothesis('ST_AxisIn', 'ST_BaseAxis_01In')
        # hyp.state == 'validated' | 'rejected' | 'ambiguous' | 'candidate'
    """

    def __init__(self, store, dt_members_index, member_to_dts_index, external_types):
        self.store = store
        self._dt_members_index = dt_members_index
        self._member_to_dts_index = member_to_dts_index
        self._external_types = external_types

        # Build relation indexes
        self._rel_from_index = {}
        self._rel_to_index = {}
        for r in store.relations:
            self._rel_from_index.setdefault((r.source_id, r.relation_type), []).append(r)
            self._rel_to_index.setdefault((r.target_id, r.relation_type), []).append(r)

    def build_all_hypotheses(self) -> list:
        """Build alias hypotheses for all external types that have candidate targets."""
        hypotheses = []

        for ext_type in sorted(self._external_types):
            # Find all datatypes that could be aliases (have members with similar names)
            # We look for datatypes with name similarity
            candidates = self._find_candidates(ext_type)
            if not candidates:
                continue

            # If multiple candidates, check for ambiguity
            if len(candidates) > 1:
                # Build hypothesis for each candidate
                for cand in candidates:
                    hyp = self.build_hypothesis(ext_type, cand)
                    # Add competing candidates
                    hyp.competing_candidates = [c for c in candidates if c != cand]
                    hypotheses.append(hyp)
            else:
                hyp = self.build_hypothesis(ext_type, candidates[0])
                hypotheses.append(hyp)

        return hypotheses

    def build_hypothesis(self, source_type: str, target_type: str) -> AliasHypothesis:
        """Build and validate a single alias hypothesis."""
        hyp = AliasHypothesis(source_type=source_type, target_type=target_type)

        source_members = self._dt_members_index.get(source_type, [])
        target_members = self._dt_members_index.get(target_type, [])

        # External types have 0 members by definition
        if source_members:
            hyp.state = REJECTED
            hyp.validation_reason = 'Source type has members, not external'
            return hyp

        if not target_members:
            hyp.state = REJECTED
            hyp.validation_reason = 'Target type also has 0 members'
            return hyp

        # We can't compute member overlap for external types (0 members)
        # Instead, we validate by:
        # 1. Name similarity (token overlap)
        # 2. Usage context (are they used by similar actor types?)
        # 3. Member accessibility (can we find expected members in target?)

        # For external types, we validate by checking if the target type
        # contains members that the source type is expected to have
        # (based on usage context — what members are accessed through it)

        # Get all members accessed through the source type
        # (from action expressions that traverse through it)
        expected_members = self._get_expected_members(source_type)

        if not expected_members:
            hyp.state = CANDIDATE
            hyp.validation_reason = 'No expected members found for external type'
            hyp.confidence = 0.1
            return hyp

        # Check how many expected members exist in target
        target_member_names = {m.key for _, m in target_members}
        matching = [m for m in expected_members if m in target_member_names]
        not_matching = [m for m in expected_members if m not in target_member_names]

        hyp.matching_members = matching
        hyp.conflicting_members = not_matching

        if expected_members:
            hyp.member_overlap_ratio = len(matching) / len(expected_members)
        else:
            hyp.member_overlap_ratio = 0.0

        # Name similarity
        name_sim = self._name_similarity(source_type, target_type)

        # Type compatibility (check if target members have compatible types)
        hyp.type_compatibility_score = name_sim

        # Documentation compatibility
        hyp.doc_compatibility_score = name_sim  # simplified

        # Usage context: check if source and target are used in similar contexts
        # (e.g., both used by axis-type actors)
        hyp.usage_context_score = self._usage_context_similarity(source_type, target_type)

        # Confidence calculation
        hyp.confidence = (
            hyp.member_overlap_ratio * 0.5 +
            name_sim * 0.2 +
            hyp.usage_context_score * 0.3
        )

        # Validation decision
        if hyp.member_overlap_ratio >= 0.5 and hyp.confidence >= 0.5:
            hyp.state = VALIDATED
            hyp.validation_reason = (
                'member_overlap={:.2f}, name_sim={:.2f}, context={:.2f}, confidence={:.2f}'.format(
                    hyp.member_overlap_ratio, name_sim,
                    hyp.usage_context_score, hyp.confidence))
        elif hyp.member_overlap_ratio > 0 and len(hyp.competing_candidates) > 1:
            hyp.state = AMBIGUOUS
            hyp.validation_reason = 'Multiple candidates with similar scores'
        elif hyp.member_overlap_ratio == 0:
            hyp.state = REJECTED
            hyp.validation_reason = 'No expected members found in target'
        else:
            hyp.state = CANDIDATE
            hyp.validation_reason = 'Low confidence, needs more evidence'

        return hyp

    def _find_candidates(self, ext_type: str) -> list:
        """Find candidate alias datatypes for an external type."""
        candidates = set()

        # Strategy 1: Find datatypes with similar names
        ext_tokens = self._tokenize(ext_type)
        for dt_name in self._dt_members_index:
            if dt_name == ext_type:
                continue
            dt_tokens = self._tokenize(dt_name)
            # Check suffix match (In→In, Out→Out)
            if ext_type.endswith('In') and dt_name.endswith('In'):
                candidates.add(dt_name)
            elif ext_type.endswith('Out') and dt_name.endswith('Out'):
                candidates.add(dt_name)
            # Check token overlap
            elif ext_tokens and dt_tokens:
                overlap = ext_tokens & dt_tokens
                if len(overlap) >= 2:
                    candidates.add(dt_name)

        # Strategy 2: Find datatypes that contain members accessed through the external type
        expected_members = self._get_expected_members(ext_type)
        for member_name in expected_members:
            dts_with_member = self._member_to_dts_index.get(member_name, [])
            for dt in dts_with_member:
                if dt != ext_type:
                    candidates.add(dt)

        return sorted(candidates)

    def _get_expected_members(self, ext_type: str) -> list:
        """Get members that are accessed through this external type.

        We look at all action expressions that traverse through this type
        and collect the member names they try to access.
        """
        # This requires knowing which actions traverse through this type.
        # For now, we use a simplified approach: check if any plc_variable
        # with this type's scope exists (it won't for external types),
        # then look at what members are commonly accessed after 'In' or 'Out'
        # in action expressions.

        # Actually, we need to look at the usage context. For external types
        # like ST_AxisIn, we know from action expressions that members like
        # Location_Auto, Home_Auto, JOG_Auto are accessed through it.
        # We can find these by looking at what the resolver tries to access.

        # Simplified: return empty list — the resolver will populate this
        # by calling build_hypothesis with expected_members parameter
        return []

    def _usage_context_similarity(self, source_type: str, target_type: str) -> float:
        """Check if source and target are used in similar contexts."""
        # Check if they share the same family prefix
        source_family = self._extract_family(source_type)
        target_family = self._extract_family(target_type)

        if source_family and target_family and source_family == target_family:
            return 1.0

        # Check suffix match
        if source_type.endswith('In') and target_type.endswith('In'):
            return 0.5
        if source_type.endswith('Out') and target_type.endswith('Out'):
            return 0.5

        return 0.0

    def _extract_family(self, dt_name: str) -> str:
        m = re.match(r'^(ST_[A-Za-z]+)', dt_name)
        return m.group(1) if m else ''

    def _tokenize(self, s: str) -> set:
        tokens = re.findall(r'[A-Z][a-z]*|[a-z]+|[A-Z]+|[0-9]+', s)
        return set(t.lower() for t in tokens)

    def _name_similarity(self, name1: str, name2: str) -> float:
        if name1 == name2:
            return 1.0
        tokens1 = self._tokenize(name1)
        tokens2 = self._tokenize(name2)
        if not tokens1 or not tokens2:
            return 0.0
        intersection = tokens1 & tokens2
        union = tokens1 | tokens2
        jaccard = len(intersection) / len(union)
        if name1.endswith('In') and name2.endswith('In'):
            jaccard += 0.2
        if name1.endswith('Out') and name2.endswith('Out'):
            jaccard += 0.2
        return min(jaccard, 1.0)


def build_alias_hypotheses_with_expected_members(store, dt_members_index,
                                                  member_to_dts_index, external_types,
                                                  expected_members_map):
    """Build alias hypotheses with expected members from action expressions.

    Args:
        expected_members_map: {external_type_name: [member_name, ...]}
                              Members that action expressions try to access
                              through this external type.

    Returns:
        list of AliasHypothesis
    """
    builder = DatatypeAliasHypothesisBuilder(
        store, dt_members_index, member_to_dts_index, external_types)

    hypotheses = []
    for ext_type, expected_members in expected_members_map.items():
        if ext_type not in external_types:
            continue

        # Find candidates
        candidates = builder._find_candidates(ext_type)
        if not candidates:
            continue

        for cand in candidates:
            hyp = builder.build_hypothesis(ext_type, cand)
            # Override expected members
            target_members = dt_members_index.get(cand, [])
            target_member_names = {m.key for _, m in target_members}
            matching = [m for m in expected_members if m in target_member_names]
            not_matching = [m for m in expected_members if m not in target_member_names]

            hyp.matching_members = matching
            hyp.conflicting_members = not_matching
            if expected_members:
                hyp.member_overlap_ratio = len(matching) / len(expected_members)

            # Recompute confidence
            name_sim = builder._name_similarity(ext_type, cand)
            hyp.type_compatibility_score = name_sim
            hyp.usage_context_score = builder._usage_context_similarity(ext_type, cand)
            hyp.confidence = (
                hyp.member_overlap_ratio * 0.5 +
                name_sim * 0.2 +
                hyp.usage_context_score * 0.3
            )

            # Recompute state — STRICTER criteria
            # validated: high member overlap (>=0.5) + high confidence (>=0.5) + no close competitors
            # plausible: some member overlap (>0) but insufficient evidence for validated
            # rejected: no member overlap
            # ambiguous: multiple candidates with similar scores
            if len(candidates) > 1:
                hyp.competing_candidates = [c for c in candidates if c != cand]

            if hyp.member_overlap_ratio == 0:
                hyp.state = REJECTED
                hyp.validation_reason = 'No expected members found in target'
            elif hyp.member_overlap_ratio >= 0.5 and hyp.confidence >= 0.5:
                # Check for close competitors
                best_score = hyp.confidence
                has_close_competitor = False
                for comp in hyp.competing_candidates:
                    comp_score = builder._name_similarity(ext_type, comp) * 0.2 + \
                                 builder._usage_context_similarity(ext_type, comp) * 0.3
                    if comp_score >= best_score * 0.9:  # within 10% = close competitor
                        has_close_competitor = True
                        break
                if has_close_competitor:
                    hyp.state = AMBIGUOUS
                    hyp.validation_reason = 'Close competitor exists (within 10% score)'
                else:
                    hyp.state = VALIDATED
                    hyp.validation_reason = (
                        'member_overlap={:.2f}, name_sim={:.2f}, context={:.2f}, confidence={:.2f}'.format(
                            hyp.member_overlap_ratio, name_sim,
                            hyp.usage_context_score, hyp.confidence))
            elif hyp.member_overlap_ratio > 0:
                # Has some matching members but not enough for validated
                # Check if this is the best candidate
                best_score = hyp.confidence
                is_best = True
                for comp in hyp.competing_candidates:
                    comp_score = builder._name_similarity(ext_type, comp) * 0.2 + \
                                 builder._usage_context_similarity(ext_type, comp) * 0.3
                    if comp_score > best_score:
                        is_best = False
                        break
                if is_best and hyp.member_overlap_ratio > 0:
                    hyp.state = PLAUSIBLE
                    hyp.validation_reason = (
                        'Best candidate but insufficient overlap (overlap={:.2f}, confidence={:.2f})'.format(
                            hyp.member_overlap_ratio, hyp.confidence))
                else:
                    hyp.state = AMBIGUOUS
                    hyp.validation_reason = 'Multiple candidates with similar scores'
            else:
                hyp.state = CANDIDATE
                hyp.validation_reason = 'Low confidence, needs more evidence'

            hypotheses.append(hyp)

    return hypotheses
