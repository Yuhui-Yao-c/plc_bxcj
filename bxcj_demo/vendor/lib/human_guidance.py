#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/human_guidance.py -- Human Guidance Decision models + clustering.

Implements the data structures and logic for:

    DecisionRisk (from assessment)
        -> UncertaintyCluster (root cause grouping)
        -> HumanGuidanceDecision (ask or continue?)
        -> ChoiceQuestion (from workflow_failure_model)
        -> EngineerChoice (from workflow_failure_model)
        -> InterpretationHypothesis (from workflow_failure_model)
        -> Validation -> Spec Update -> Resume

Design principles (per spec):
    - Default autonomous; human only when high-leverage.
    - Not failure-only; decision risk and exploration efficiency
      also trigger.
    - Questions are choice-based, dynamically generated from evidence.
    - Engineer's choice is guidance, not verified fact -- must validate.
    - Human cost is bounded and configurable.
    - Project-specific questions/answers are NOT hardcoded in Python.

This module reuses the existing ChoiceQuestion / EngineerChoice /
InterpretationHypothesis from workflow_failure_model.py and adds:

    - UncertaintyCluster
    - HumanGuidanceDecision
    - ProjectGuidanceKnowledge
    - HumanGuidanceConfig
    - cluster_uncertainties()
    - estimate_human_value()
    - plan_question()
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from collections import defaultdict

from lib.workflow_failure_model import (
    ChoiceQuestion,
    QuestionOption,
    EngineerChoice,
    InterpretationHypothesis,
    generate_id,
    now_iso,
    save_json,
    load_json,
)
from lib.decision_quality import (
    DecisionRisk,
    DecisionQualityAssessment,
    SEV_HUMAN_CANDIDATE,
    SEV_NEEDS_AUTO,
    SEV_BLOCKED,
    TRIGGER_DECISION_RISK,
    TRIGGER_EXPLORATION_EFFICIENCY,
    TRIGGER_EXECUTION_RECOVERY,
    AREA_A_STEP_DISCOVERY,
    AREA_B_PROGRAM_LOGIC,
    AREA_C_SEMANTIC_GROUNDING,
    AREA_D_FINAL_TIMING_ROW,
)


# ============================================================
# Human Guidance Config (configurable limits)
# ============================================================

DEFAULT_CONFIG = {
    'max_questions_per_session': 3,
    'max_questions_per_root_cause': 1,
    'min_evidence_for_options': 2,
    'min_affected_scope_for_question': 10,
    'dedup_key': 'root_cause_hint',
    'allow_skip': True,
    'allow_uncertain': True,
    'allow_none_of_above': True,
    'require_recommendation': True,
    'require_evidence_in_options': True,
}


def load_config(config_path: str = None) -> dict:
    """Load human guidance config, falling back to defaults."""
    if config_path:
        p = Path(config_path)
        if p.exists():
            with open(p, 'r', encoding='utf-8-sig') as f:
                cfg = DEFAULT_CONFIG.copy()
                cfg.update(json.load(f))
                return cfg
    return DEFAULT_CONFIG.copy()


# ============================================================
# UncertaintyCluster -- grouped risks sharing root cause
# ============================================================

@dataclass
class UncertaintyCluster:
    """A cluster of DecisionRisks sharing a common root cause.

    Clustering key: root_cause_hint (or area+category if no hint).
    """

    cluster_id: str = ''
    root_cause_hint: str = ''
    area: str = ''
    category: str = ''
    risk_ids: list = field(default_factory=list)
    affected_scope: dict = field(default_factory=dict)
    total_affected_steps: int = 0
    total_affected_rows: int = 0
    competing_hypotheses: list = field(default_factory=list)
    evidence_conflicts: list = field(default_factory=list)
    trigger_type: str = ''
    estimated_human_value: str = ''    # low / medium / high
    interaction_cost: str = ''         # low / medium / high
    sample_evidence: list = field(default_factory=list)
    has_been_asked: bool = False
    asked_question_id: str = ''

    def to_dict(self) -> dict:
        return {
            'cluster_id': self.cluster_id,
            'root_cause_hint': self.root_cause_hint,
            'area': self.area,
            'category': self.category,
            'risk_ids': self.risk_ids,
            'affected_scope': self.affected_scope,
            'total_affected_steps': self.total_affected_steps,
            'total_affected_rows': self.total_affected_rows,
            'competing_hypotheses': self.competing_hypotheses,
            'evidence_conflicts': self.evidence_conflicts,
            'trigger_type': self.trigger_type,
            'estimated_human_value': self.estimated_human_value,
            'interaction_cost': self.interaction_cost,
            'sample_evidence': self.sample_evidence,
            'has_been_asked': self.has_been_asked,
            'asked_question_id': self.asked_question_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'UncertaintyCluster':
        return cls(
            cluster_id=d.get('cluster_id', ''),
            root_cause_hint=d.get('root_cause_hint', ''),
            area=d.get('area', ''),
            category=d.get('category', ''),
            risk_ids=d.get('risk_ids', []),
            affected_scope=d.get('affected_scope', {}),
            total_affected_steps=d.get('total_affected_steps', 0),
            total_affected_rows=d.get('total_affected_rows', 0),
            competing_hypotheses=d.get('competing_hypotheses', []),
            evidence_conflicts=d.get('evidence_conflicts', []),
            trigger_type=d.get('trigger_type', ''),
            estimated_human_value=d.get('estimated_human_value', ''),
            interaction_cost=d.get('interaction_cost', ''),
            sample_evidence=d.get('sample_evidence', []),
            has_been_asked=d.get('has_been_asked', False),
            asked_question_id=d.get('asked_question_id', ''),
        )


# ============================================================
# HumanGuidanceDecision -- should we ask or continue?
# ============================================================

@dataclass
class HumanGuidanceDecision:
    """OpenCode's autonomous decision on whether to ask the engineer.

    All fields are auto-generated by opencode (via cluster_uncertainties +
    estimate_human_value).  Only when ask_or_continue='ask' does the
    interaction happen.
    """

    decision_id: str = ''
    project: str = ''
    workflow_stage: str = ''
    cluster_id: str = ''
    problem_category: str = ''
    triggering_evidence: list = field(default_factory=list)
    affected_scope: dict = field(default_factory=dict)
    estimated_affected_steps: int = 0
    estimated_affected_rows: int = 0
    competing_hypotheses: list = field(default_factory=list)
    evidence_conflicts: list = field(default_factory=list)
    auto_investigation_attempts: list = field(default_factory=list)
    estimated_human_value: str = ''
    interaction_cost: str = ''
    ask_or_continue: str = 'continue'   # 'ask' or 'continue'
    decision_reason: str = ''
    created_at: str = ''

    def to_dict(self) -> dict:
        return {
            'decision_id': self.decision_id,
            'project': self.project,
            'workflow_stage': self.workflow_stage,
            'cluster_id': self.cluster_id,
            'problem_category': self.problem_category,
            'triggering_evidence': self.triggering_evidence,
            'affected_scope': self.affected_scope,
            'estimated_affected_steps': self.estimated_affected_steps,
            'estimated_affected_rows': self.estimated_affected_rows,
            'competing_hypotheses': self.competing_hypotheses,
            'evidence_conflicts': self.evidence_conflicts,
            'auto_investigation_attempts': self.auto_investigation_attempts,
            'estimated_human_value': self.estimated_human_value,
            'interaction_cost': self.interaction_cost,
            'ask_or_continue': self.ask_or_continue,
            'decision_reason': self.decision_reason,
            'created_at': self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'HumanGuidanceDecision':
        return cls(
            decision_id=d.get('decision_id', ''),
            project=d.get('project', ''),
            workflow_stage=d.get('workflow_stage', ''),
            cluster_id=d.get('cluster_id', ''),
            problem_category=d.get('problem_category', ''),
            triggering_evidence=d.get('triggering_evidence', []),
            affected_scope=d.get('affected_scope', {}),
            estimated_affected_steps=d.get('estimated_affected_steps', 0),
            estimated_affected_rows=d.get('estimated_affected_rows', 0),
            competing_hypotheses=d.get('competing_hypotheses', []),
            evidence_conflicts=d.get('evidence_conflicts', []),
            auto_investigation_attempts=d.get('auto_investigation_attempts', []),
            estimated_human_value=d.get('estimated_human_value', ''),
            interaction_cost=d.get('interaction_cost', ''),
            ask_or_continue=d.get('ask_or_continue', 'continue'),
            decision_reason=d.get('decision_reason', ''),
            created_at=d.get('created_at', ''),
        )


# ============================================================
# ProjectGuidanceKnowledge -- accumulated guidance knowledge
# ============================================================

@dataclass
class GuidanceKnowledgeEntry:
    """A single validated guidance knowledge entry.

    Saved when an InterpretationHypothesis is validated.  Can be reused
    for similar structures in the same project, and as a candidate
    hypothesis for new projects (must re-validate).
    """

    entry_id: str = ''
    project: str = ''
    trigger_type: str = ''
    root_cause: str = ''
    engineer_choice: str = ''
    interpretation_rule: str = ''
    applicable_structure: str = ''
    scope: dict = field(default_factory=dict)
    validation_status: str = ''    # validated / partially_validated / refuted
    supporting_evidence: list = field(default_factory=list)
    counterexamples: list = field(default_factory=list)
    spec_update: dict = field(default_factory=dict)
    related_question_id: str = ''
    related_hypothesis_id: str = ''
    created_at: str = ''
    validated_at: str = ''

    def to_dict(self) -> dict:
        return {
            'entry_id': self.entry_id,
            'project': self.project,
            'trigger_type': self.trigger_type,
            'root_cause': self.root_cause,
            'engineer_choice': self.engineer_choice,
            'interpretation_rule': self.interpretation_rule,
            'applicable_structure': self.applicable_structure,
            'scope': self.scope,
            'validation_status': self.validation_status,
            'supporting_evidence': self.supporting_evidence,
            'counterexamples': self.counterexamples,
            'spec_update': self.spec_update,
            'related_question_id': self.related_question_id,
            'related_hypothesis_id': self.related_hypothesis_id,
            'created_at': self.created_at,
            'validated_at': self.validated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'GuidanceKnowledgeEntry':
        return cls(
            entry_id=d.get('entry_id', ''),
            project=d.get('project', ''),
            trigger_type=d.get('trigger_type', ''),
            root_cause=d.get('root_cause', ''),
            engineer_choice=d.get('engineer_choice', ''),
            interpretation_rule=d.get('interpretation_rule', ''),
            applicable_structure=d.get('applicable_structure', ''),
            scope=d.get('scope', {}),
            validation_status=d.get('validation_status', ''),
            supporting_evidence=d.get('supporting_evidence', []),
            counterexamples=d.get('counterexamples', []),
            spec_update=d.get('spec_update', {}),
            related_question_id=d.get('related_question_id', ''),
            related_hypothesis_id=d.get('related_hypothesis_id', ''),
            created_at=d.get('created_at', ''),
            validated_at=d.get('validated_at', ''),
        )


@dataclass
class ProjectGuidanceKnowledge:
    """Accumulated guidance knowledge for a project."""

    project: str = ''
    entries: list = field(default_factory=list)  # list[GuidanceKnowledgeEntry]
    asked_questions: list = field(default_factory=list)  # question_ids
    resolved_clusters: list = field(default_factory=list)
    unresolved_clusters: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'project': self.project,
            'entries': [e.to_dict() if isinstance(e, GuidanceKnowledgeEntry)
                        else e for e in self.entries],
            'asked_questions': self.asked_questions,
            'resolved_clusters': self.resolved_clusters,
            'unresolved_clusters': self.unresolved_clusters,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ProjectGuidanceKnowledge':
        return cls(
            project=d.get('project', ''),
            entries=[GuidanceKnowledgeEntry.from_dict(e)
                     for e in d.get('entries', [])],
            asked_questions=d.get('asked_questions', []),
            resolved_clusters=d.get('resolved_clusters', []),
            unresolved_clusters=d.get('unresolved_clusters', []),
        )

    def save(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> 'ProjectGuidanceKnowledge':
        with open(path, 'r', encoding='utf-8') as f:
            return cls.from_dict(json.load(f))


# ============================================================
# Clustering Function
# ============================================================

def cluster_uncertainties(assessment: DecisionQualityAssessment,
                          config: dict = None) -> list:
    """Group DecisionRisks into UncertaintyClusters by root cause.

    Clustering strategy:
        1. Primary key: root_cause_hint
        2. Secondary key: area + category (if no root_cause_hint)
        3. Merge competing_hypotheses and evidence_conflicts
        4. Sum affected_steps and affected_rows
        5. Estimate human value (higher for larger scope + more conflicts)

    Args:
        assessment: DecisionQualityAssessment from assess_all()
        config: Human guidance config dict

    Returns:
        List of UncertaintyCluster, sorted by total_affected_rows desc.
    """
    if config is None:
        config = DEFAULT_CONFIG.copy()

    dedup_key = config.get('dedup_key', 'root_cause_hint')

    # Collect all risks (exclude trusted)
    all_risks = []
    for area in assessment.areas:
        for risk in area.risks:
            if risk.severity != 'trusted':
                all_risks.append(risk)

    # Group by clustering key
    groups = defaultdict(list)
    for risk in all_risks:
        key = getattr(risk, dedup_key, '') or '{}_{}'.format(
            risk.area, risk.category)
        groups[key].append(risk)

    # Build clusters
    clusters = []
    for key, risks in groups.items():
        # Merge competing hypotheses (dedup preserving order)
        all_hypotheses = []
        for r in risks:
            for h in r.competing_hypotheses:
                if h not in all_hypotheses:
                    all_hypotheses.append(h)

        # Merge evidence conflicts
        all_conflicts = []
        for r in risks:
            for c in r.evidence_conflicts:
                if c not in all_conflicts:
                    all_conflicts.append(c)

        # Sum affected scope
        total_steps = sum(r.estimated_affected_steps for r in risks)
        total_rows = sum(r.estimated_affected_rows for r in risks)

        # Determine trigger type (most severe wins)
        trigger = TRIGGER_DECISION_RISK
        for r in risks:
            if r.trigger_type == TRIGGER_EXECUTION_RECOVERY:
                trigger = TRIGGER_EXECUTION_RECOVERY
                break
            if r.trigger_type == TRIGGER_EXPLORATION_EFFICIENCY:
                if trigger != TRIGGER_EXECUTION_RECOVERY:
                    trigger = TRIGGER_EXPLORATION_EFFICIENCY

        # Estimate human value
        human_value = _estimate_human_value(
            total_rows, len(all_hypotheses), len(all_conflicts), trigger)

        # Interaction cost (choice question = low)
        interaction_cost = 'low'

        # Sample evidence (up to 5 items)
        sample_ev = []
        for r in risks[:3]:
            for e in r.triggering_evidence[:2]:
                if e not in sample_ev:
                    sample_ev.append(e)
            if len(sample_ev) >= 5:
                break

        cluster = UncertaintyCluster(
            cluster_id=generate_id('cl'),
            root_cause_hint=key,
            area=risks[0].area,
            category=risks[0].category,
            risk_ids=[r.risk_id for r in risks],
            affected_scope={
                'risk_count': len(risks),
                'areas': list(set(r.area for r in risks)),
            },
            total_affected_steps=total_steps,
            total_affected_rows=total_rows,
            competing_hypotheses=all_hypotheses,
            evidence_conflicts=all_conflicts,
            trigger_type=trigger,
            estimated_human_value=human_value,
            interaction_cost=interaction_cost,
            sample_evidence=sample_ev,
        )
        clusters.append(cluster)

    # Sort by total_affected_rows descending
    clusters.sort(key=lambda c: c.total_affected_rows, reverse=True)
    return clusters


def _estimate_human_value(total_rows: int, num_hypotheses: int,
                          num_conflicts: int, trigger_type: str) -> str:
    """Estimate the value of human guidance for a cluster.

    Factors:
        - affected scope (more rows = higher value)
        - number of competing hypotheses (more = higher value)
        - evidence conflicts (more = higher value)
        - trigger type (execution recovery = higher urgency)

    Returns: 'low', 'medium', or 'high'
    """
    score = 0

    # Scope
    if total_rows > 100:
        score += 3
    elif total_rows > 30:
        score += 2
    elif total_rows > 10:
        score += 1

    # Competing hypotheses
    if num_hypotheses >= 3:
        score += 2
    elif num_hypotheses >= 2:
        score += 1

    # Evidence conflicts
    if num_conflicts >= 2:
        score += 2
    elif num_conflicts >= 1:
        score += 1

    # Trigger type
    if trigger_type == TRIGGER_EXECUTION_RECOVERY:
        score += 1

    if score >= 5:
        return 'high'
    elif score >= 3:
        return 'medium'
    else:
        return 'low'


# ============================================================
# Question Planning
# ============================================================

# Mapping from root_cause_hint to question template metadata.
# These are NOT hardcoded answers -- they are question *frameworks*
# that opencode uses to generate options from real evidence.
# The actual options are always derived from the cluster's
# competing_hypotheses and triggering_evidence.

QUESTION_FRAMEWORKS = {
    'multi_branch_successor_ambiguity': {
        'question_template': (
            '当前工程中有{}个步序的Normal Successor依赖fallback策略，'
            'Gold匹配率很低。这类多分支状态机通常通过什么机制区分正常执行路径？'),
        'area_label': '程序逻辑',
    },
    'actor_identity_mapping_gap': {
        'question_template': (
            '当前设备有{}个动作实例缺乏工程机构名称关联。'
            'Cylinder/Vacuum实例主要通过哪类资源找到真实机构名称？'),
        'area_label': '语义关联',
    },
    'cmt_semantic_ambiguity': {
        'question_template': (
            '当前有{}个动作描述主要来自CMT/注释，'
            '可能仅表示记忆判断而非工程动作。'
            '这类CMT注释通常表示什么？'),
        'area_label': '语义关联',
    },
    'dynamic_step_source': {
        'question_template': (
            '发现{}个运行时定义的步序，其来源无法从静态源码确定。'
            '这套程序中未在PLC源码静态定义的步序数据通常来自哪里？'),
        'area_label': '步序发现',
    },
    'timing_boundary_uncertainty': {
        'question_template': (
            '{}个FinalTimingRow的时序边界依赖fallback。'
            '这类步序的结束标签通常通过什么方式确定？'),
        'area_label': '时序边界',
    },
    'row_granularity_ambiguity': {
        'question_template': (
            '{}个行为Composite Row，合并粒度可能不符合时序采集目标。'
            '对于当前这类程序结构，时序采集通常采用哪种动作组织方式？'),
        'area_label': '动作行粒度',
    },
    'min_target_unreliable': {
        'question_template': (
            'Gold数据显示min-target fallback在{}个案例中选错。'
            '多分支步序中，通常应该选择哪个分支作为正常后继？'),
        'area_label': '程序逻辑',
    },
    'cmt_only_descriptions': {
        'question_template': (
            '所有动作描述均为usable级别，没有strong级别。'
            '这类CMT注释是否能直接作为工程动作描述使用？'),
        'area_label': '语义关联',
    },
}


def plan_question(cluster: UncertaintyCluster,
                  project: str,
                  config: dict = None,
                  knowledge: ProjectGuidanceKnowledge = None) -> dict:
    """Plan a choice question for a cluster.

    Returns a dict with:
        - ask_or_continue: 'ask' or 'continue'
        - decision: HumanGuidanceDecision
        - question: ChoiceQuestion (if ask)
        - reason: why ask or continue

    The question options are generated from the cluster's
    competing_hypotheses, NOT from a fixed template.
    """
    if config is None:
        config = DEFAULT_CONFIG.copy()

    # Check if already asked
    if knowledge and cluster.cluster_id in knowledge.resolved_clusters:
        return {
            'ask_or_continue': 'continue',
            'reason': 'Cluster already resolved in knowledge store',
        }

    if cluster.has_been_asked:
        return {
            'ask_or_continue': 'continue',
            'reason': 'Already asked for this root cause',
        }

    # Check minimum scope
    min_scope = config.get('min_affected_scope_for_question', 10)
    if cluster.total_affected_rows < min_scope:
        return {
            'ask_or_continue': 'continue',
            'reason': (
                'Affected scope ({}) below threshold ({})'.format(
                    cluster.total_affected_rows, min_scope)),
        }

    # Check if we have enough hypotheses for options
    min_evidence = config.get('min_evidence_for_options', 2)
    if len(cluster.competing_hypotheses) < min_evidence:
        return {
            'ask_or_continue': 'continue',
            'reason': (
                'Not enough competing hypotheses ({}/{}) to form '
                'meaningful options'.format(
                    len(cluster.competing_hypotheses), min_evidence)),
        }

    # Estimate human value
    human_value = cluster.estimated_human_value
    if human_value == 'low':
        return {
            'ask_or_continue': 'continue',
            'reason': 'Estimated human value is low',
        }

    # Build decision
    decision = HumanGuidanceDecision(
        decision_id=generate_id('hgd'),
        project=project,
        workflow_stage='post_assessment',
        cluster_id=cluster.cluster_id,
        problem_category=cluster.root_cause_hint,
        triggering_evidence=cluster.sample_evidence,
        affected_scope=cluster.affected_scope,
        estimated_affected_steps=cluster.total_affected_steps,
        estimated_affected_rows=cluster.total_affected_rows,
        competing_hypotheses=cluster.competing_hypotheses,
        evidence_conflicts=cluster.evidence_conflicts,
        estimated_human_value=human_value,
        interaction_cost=cluster.interaction_cost,
        ask_or_continue='ask',
        decision_reason=(
            'Affected {} rows, {} competing hypotheses, '
            '{} evidence conflicts. Human guidance expected to '
            'resolve a class of problems.'.format(
                cluster.total_affected_rows,
                len(cluster.competing_hypotheses),
                len(cluster.evidence_conflicts))),
        created_at=now_iso(),
    )

    # Build question
    framework = QUESTION_FRAMEWORKS.get(
        cluster.root_cause_hint, {})

    question_template = framework.get('question_template', (
        '当前发现{}个受影响实例。请选择最符合该工程实际情况的选项：'))

    area_label = framework.get('area_label', cluster.area)
    question_text = question_template.format(cluster.total_affected_rows)

    # Build options from competing hypotheses
    options = []
    for i, hyp in enumerate(cluster.competing_hypotheses):
        option_id = chr(ord('A') + i)
        options.append(QuestionOption(
            option_id=option_id,
            text=hyp,
            interpretation_hint=hyp,
            expected_scope='all_affected' if human_value == 'high' else 'sample',
        ))

    # Add uncertain / none_of_above options
    if config.get('allow_uncertain', True):
        options.append(QuestionOption(
            option_id='E',
            text='不确定',
            interpretation_hint='engineer_unsure',
            expected_scope='none',
        ))

    if config.get('allow_none_of_above', True):
        last_id = chr(ord('A') + len(cluster.competing_hypotheses))
        if config.get('allow_uncertain', True):
            last_id = chr(ord(last_id) + 1)
        options.append(QuestionOption(
            option_id=last_id,
            text='以上都不符合',
            interpretation_hint='none_of_above',
            expected_scope='none',
        ))

    # Determine recommended option (first hypothesis, but not confirmed)
    recommended = options[0].option_id if options else ''
    recommended_reason = ''
    if options:
        recommended_reason = (
            '推荐{}，因为该假设已有部分程序证据支持，'
            '但尚未验证完整链路。'.format(recommended))

    question = ChoiceQuestion(
        question_id=generate_id('q'),
        triggering_cluster_id=cluster.cluster_id,
        trigger_type=cluster.trigger_type,
        root_cause_cluster=cluster.root_cause_hint,
        question_text=question_text,
        problem_summary='[{}] {}'.format(
            area_label, cluster.root_cause_hint),
        auto_investigation_summary=(
            '已自动识别{}个受影响实例，'
            '{}个竞争假设，{}个证据冲突。'.format(
                cluster.total_affected_rows,
                len(cluster.competing_hypotheses),
                len(cluster.evidence_conflicts))),
        options=options,
        supporting_evidence=cluster.sample_evidence,
        recommended_option=recommended,
        recommended_reason=recommended_reason,
        affected_scope={
            'total_affected_rows': cluster.total_affected_rows,
            'total_affected_steps': cluster.total_affected_steps,
            'risk_count': len(cluster.risk_ids),
        },
        expected_impact=(
            '可能改善{}个FinalTimingRow的可靠性'.format(
                cluster.total_affected_rows)),
        can_skip=config.get('allow_skip', True),
        status='pending',
        created_at=now_iso(),
    )

    return {
        'ask_or_continue': 'ask',
        'decision': decision,
        'question': question,
        'reason': 'Question planned for high-value cluster',
    }


# ============================================================
# Record Engineer Choice + Form Hypothesis
# ============================================================

def record_engineer_choice(question: ChoiceQuestion,
                           selected_option_id: str,
                           short_note: str = '') -> tuple:
    """Record the engineer's choice and form an InterpretationHypothesis.

    Returns:
        (EngineerChoice, InterpretationHypothesis)

    The hypothesis is PENDING -- opencode must validate it before use.
    """
    # Find selected option
    selected_option = None
    for opt in question.options:
        if opt.option_id == selected_option_id:
            selected_option = opt
            break

    if selected_option is None:
        selected_option = QuestionOption(
            option_id=selected_option_id,
            text=short_note or 'unknown',
        )

    choice = EngineerChoice(
        question_id=question.question_id,
        selected_option=selected_option_id,
        optional_short_note=short_note,
        decision_scope=selected_option.expected_scope,
        timestamp=now_iso(),
    )

    # Form hypothesis
    hypothesis_text = selected_option.interpretation_hint or selected_option.text

    hypothesis = InterpretationHypothesis(
        hypothesis_id=generate_id('hyp'),
        source_question_id=question.question_id,
        source_choice=selected_option_id,
        hypothesis_text=hypothesis_text,
        scope={
            'cluster_id': question.triggering_cluster_id,
            'root_cause': question.root_cause_cluster,
            'affected_rows': question.affected_scope.get(
                'total_affected_rows', 0),
        },
        status='pending_verification',
        created_at=now_iso(),
    )

    # Link
    choice.hypothesis_id = hypothesis.hypothesis_id

    return choice, hypothesis


# ============================================================
# Validation Result Helper
# ============================================================

def create_knowledge_entry(hypothesis: InterpretationHypothesis,
                           project: str,
                           choice: EngineerChoice,
                           question: ChoiceQuestion) -> GuidanceKnowledgeEntry:
    """Create a GuidanceKnowledgeEntry from a validated hypothesis.

    Called after opencode has validated the hypothesis against source
    evidence.
    """
    return GuidanceKnowledgeEntry(
        entry_id=generate_id('gke'),
        project=project,
        trigger_type=question.trigger_type,
        root_cause=question.root_cause_cluster,
        engineer_choice=choice.selected_option,
        interpretation_rule=hypothesis.hypothesis_text,
        applicable_structure=hypothesis.scope.get('root_cause', ''),
        scope=hypothesis.scope,
        validation_status=hypothesis.status,
        supporting_evidence=hypothesis.supporting_evidence,
        counterexamples=hypothesis.conflicting_evidence,
        spec_update=hypothesis.spec_update,
        related_question_id=question.question_id,
        related_hypothesis_id=hypothesis.hypothesis_id,
        created_at=hypothesis.created_at,
        validated_at=hypothesis.validated_at or now_iso(),
    )
