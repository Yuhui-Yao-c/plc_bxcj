#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/decision_quality.py -- Decision Quality Gate data models + assessment.

Extends the existing exception-driven workflow_failure_model.py to cover
*decision quality* risks that arise even when tool execution succeeds.

The core distinction:

    tool_execution_success = true   !=   decision_quality = trusted

A workflow can run without errors yet produce unreliable results if key
decisions depend on unreliable assumptions, ambiguous evidence, or
systematic fallback heuristics.

Five assessment areas (per spec section 三):

    A -- Step Discovery
    B -- Program Logic
    C -- Semantic Grounding
    D -- FinalTimingRow
    E -- Runtime Trace

Each area produces DecisionRisk items.  Each risk is classified into one
of five severity levels:

    trusted                       -- evidence sufficient, continue
    needs_auto_investigation      -- risk exists, try self-verification
    human_guidance_candidate      -- engineer input may improve reliability
    blocked                       -- missing critical info, cannot continue
    unresolved_nonblocking        -- cannot resolve now, non-blocking

This module provides *data models* and *assessment functions*.  The CLI
entry point is tools/assess_decision_quality.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ============================================================
# Severity Levels
# ============================================================

SEV_TRUSTED = 'trusted'
SEV_NEEDS_AUTO = 'needs_auto_investigation'
SEV_HUMAN_CANDIDATE = 'human_guidance_candidate'
SEV_BLOCKED = 'blocked'
SEV_UNRESOLVED_NONBLOCKING = 'unresolved_nonblocking'

SEVERITY_ORDER = {
    SEV_TRUSTED: 0,
    SEV_UNRESOLVED_NONBLOCKING: 1,
    SEV_NEEDS_AUTO: 2,
    SEV_HUMAN_CANDIDATE: 3,
    SEV_BLOCKED: 4,
}

VALID_SEVERITIES = {
    SEV_TRUSTED, SEV_NEEDS_AUTO, SEV_HUMAN_CANDIDATE,
    SEV_BLOCKED, SEV_UNRESOLVED_NONBLOCKING,
}


# ============================================================
# Assessment Areas
# ============================================================

AREA_A_STEP_DISCOVERY = 'A_step_discovery'
AREA_B_PROGRAM_LOGIC = 'B_program_logic'
AREA_C_SEMANTIC_GROUNDING = 'C_semantic_grounding'
AREA_D_FINAL_TIMING_ROW = 'D_final_timing_row'
AREA_E_RUNTIME_TRACE = 'E_runtime_trace'

AREA_LABELS = {
    AREA_A_STEP_DISCOVERY: 'Step Discovery',
    AREA_B_PROGRAM_LOGIC: 'Program Logic',
    AREA_C_SEMANTIC_GROUNDING: 'Semantic Grounding',
    AREA_D_FINAL_TIMING_ROW: 'FinalTimingRow',
    AREA_E_RUNTIME_TRACE: 'Runtime Trace',
}

VALID_AREAS = set(AREA_LABELS.keys())


# ============================================================
# Trigger Types (extended beyond exception-driven)
# ============================================================

TRIGGER_EXECUTION_RECOVERY = 'T1_execution_recovery'
TRIGGER_DECISION_RISK = 'T2_decision_risk'
TRIGGER_EXPLORATION_EFFICIENCY = 'T3_exploration_efficiency'

TRIGGER_LABELS = {
    TRIGGER_EXECUTION_RECOVERY: '执行恢复型',
    TRIGGER_DECISION_RISK: '决策风险型',
    TRIGGER_EXPLORATION_EFFICIENCY: '探索效率型',
}


# ============================================================
# DecisionRisk -- a single identified risk
# ============================================================

@dataclass
class DecisionRisk:
    """A single decision quality risk identified during assessment.

    Unlike FailureInstance (which fires on tool errors), DecisionRisk fires
    when tool execution succeeded but the *quality* of a key decision is
    uncertain.
    """

    risk_id: str = ''
    area: str = ''               # one of AREA_*
    category: str = ''           # sub-category within the area
    trigger_type: str = ''       # T1 / T2 / T3
    severity: str = ''           # one of SEV_*
    description: str = ''
    triggering_evidence: list = field(default_factory=list)
    affected_scope: dict = field(default_factory=dict)
    estimated_affected_steps: int = 0
    estimated_affected_rows: int = 0
    competing_hypotheses: list = field(default_factory=list)
    evidence_conflicts: list = field(default_factory=list)
    auto_investigation_attempts: list = field(default_factory=list)
    estimated_human_value: str = ''    # low / medium / high
    interaction_cost: str = ''         # low / medium / high
    root_cause_hint: str = ''
    source_files: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'risk_id': self.risk_id,
            'area': self.area,
            'category': self.category,
            'trigger_type': self.trigger_type,
            'severity': self.severity,
            'description': self.description,
            'triggering_evidence': self.triggering_evidence,
            'affected_scope': self.affected_scope,
            'estimated_affected_steps': self.estimated_affected_steps,
            'estimated_affected_rows': self.estimated_affected_rows,
            'competing_hypotheses': self.competing_hypotheses,
            'evidence_conflicts': self.evidence_conflicts,
            'auto_investigation_attempts': self.auto_investigation_attempts,
            'estimated_human_value': self.estimated_human_value,
            'interaction_cost': self.interaction_cost,
            'root_cause_hint': self.root_cause_hint,
            'source_files': self.source_files,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'DecisionRisk':
        return cls(
            risk_id=d.get('risk_id', ''),
            area=d.get('area', ''),
            category=d.get('category', ''),
            trigger_type=d.get('trigger_type', ''),
            severity=d.get('severity', ''),
            description=d.get('description', ''),
            triggering_evidence=d.get('triggering_evidence', []),
            affected_scope=d.get('affected_scope', {}),
            estimated_affected_steps=d.get('estimated_affected_steps', 0),
            estimated_affected_rows=d.get('estimated_affected_rows', 0),
            competing_hypotheses=d.get('competing_hypotheses', []),
            evidence_conflicts=d.get('evidence_conflicts', []),
            auto_investigation_attempts=d.get('auto_investigation_attempts', []),
            estimated_human_value=d.get('estimated_human_value', ''),
            interaction_cost=d.get('interaction_cost', ''),
            root_cause_hint=d.get('root_cause_hint', ''),
            source_files=d.get('source_files', []),
        )


# ============================================================
# AreaAssessment -- results for one of the 5 areas
# ============================================================

@dataclass
class AreaAssessment:
    """Assessment result for one of the five areas."""

    area: str = ''
    overall_severity: str = SEV_TRUSTED
    risks: list = field(default_factory=list)  # list[DecisionRisk]
    summary: str = ''
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'area': self.area,
            'overall_severity': self.overall_severity,
            'risks': [r.to_dict() if isinstance(r, DecisionRisk) else r
                      for r in self.risks],
            'summary': self.summary,
            'metrics': self.metrics,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'AreaAssessment':
        return cls(
            area=d.get('area', ''),
            overall_severity=d.get('overall_severity', SEV_TRUSTED),
            risks=[DecisionRisk.from_dict(r) for r in d.get('risks', [])],
            summary=d.get('summary', ''),
            metrics=d.get('metrics', {}),
        )


# ============================================================
# DecisionQualityAssessment -- top-level container
# ============================================================

@dataclass
class DecisionQualityAssessment:
    """Top-level decision quality assessment for a project workflow run.

    Produced by tools/assess_decision_quality.py, consumed by opencode
    to decide whether to continue autonomously, investigate further, or
    request human guidance.
    """

    project: str = ''
    timestamp: str = ''
    workflow_stage: str = ''
    areas: list = field(default_factory=list)  # list[AreaAssessment]
    overall_quality: str = SEV_TRUSTED
    total_risks: int = 0
    risks_by_severity: dict = field(default_factory=dict)
    risks_by_area: dict = field(default_factory=dict)
    human_guidance_candidates: list = field(default_factory=list)
    auto_investigation_candidates: list = field(default_factory=list)
    blocked_areas: list = field(default_factory=list)
    baseline_metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'project': self.project,
            'timestamp': self.timestamp,
            'workflow_stage': self.workflow_stage,
            'areas': [a.to_dict() if isinstance(a, AreaAssessment) else a
                      for a in self.areas],
            'overall_quality': self.overall_quality,
            'total_risks': self.total_risks,
            'risks_by_severity': self.risks_by_severity,
            'risks_by_area': self.risks_by_area,
            'human_guidance_candidates': self.human_guidance_candidates,
            'auto_investigation_candidates': self.auto_investigation_candidates,
            'blocked_areas': self.blocked_areas,
            'baseline_metrics': self.baseline_metrics,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'DecisionQualityAssessment':
        return cls(
            project=d.get('project', ''),
            timestamp=d.get('timestamp', ''),
            workflow_stage=d.get('workflow_stage', ''),
            areas=[AreaAssessment.from_dict(a) for a in d.get('areas', [])],
            overall_quality=d.get('overall_quality', SEV_TRUSTED),
            total_risks=d.get('total_risks', 0),
            risks_by_severity=d.get('risks_by_severity', {}),
            risks_by_area=d.get('risks_by_area', {}),
            human_guidance_candidates=d.get('human_guidance_candidates', []),
            auto_investigation_candidates=d.get('auto_investigation_candidates', []),
            blocked_areas=d.get('blocked_areas', []),
            baseline_metrics=d.get('baseline_metrics', {}),
        )

    def save(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> 'DecisionQualityAssessment':
        with open(path, 'r', encoding='utf-8') as f:
            return cls.from_dict(json.load(f))


# ============================================================
# Assessment Functions (called by tools/assess_decision_quality.py)
# ============================================================

def _load_json(path: Path) -> dict | list | None:
    """Safely load a JSON file, returning None if not found."""
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _safe_get(d: dict | None, *keys, default=None):
    """Safely traverse nested dict keys."""
    if d is None:
        return default
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k, default)
        else:
            return default
    return cur


def assess_step_discovery(output_dir: Path,
                          project_name: str) -> AreaAssessment:
    """Area A -- Step Discovery quality assessment.

    Checks:
        - state machine mechanism identification
        - missing step definitions (dynamic steps)
        - step universe anomalies (step count, duplicates, gaps)
    """
    area = AreaAssessment(area=AREA_A_STEP_DISCOVERY)
    risks = []

    # Check step_table_discovery.json for dynamic step issues
    step_table = _load_json(output_dir / 'step_table_discovery.json')
    if step_table and isinstance(step_table, dict):
        classification = step_table.get('step_classification', {})
        runtime_defined = classification.get('runtime_defined', 0)
        total = classification.get('total', 0)
        runtime_samples = classification.get('runtime_step_samples', [])

        if runtime_defined > 0:
            risks.append(DecisionRisk(
                risk_id='A001',
                area=AREA_A_STEP_DISCOVERY,
                category='dynamic_step_source_unknown',
                trigger_type=TRIGGER_DECISION_RISK,
                severity=SEV_HUMAN_CANDIDATE,
                description=(
                    '发现{}个运行时定义的步序，其来源无法从静态源码确定。'
                    '步号样本: {}'.format(
                        runtime_defined,
                        runtime_samples[:10])),
                triggering_evidence=[
                    'step_table_discovery.json: runtime_defined={}'.format(
                        runtime_defined),
                    '总步数: {}'.format(total),
                ],
                affected_scope={
                    'runtime_step_count': runtime_defined,
                    'total_steps': total,
                    'runtime_step_samples': runtime_samples,
                },
                estimated_affected_steps=runtime_defined,
                estimated_affected_rows=runtime_defined,
                competing_hypotheses=[
                    'HMI或配方下发',
                    '外部配置/数据库',
                    'PLC其他程序单元初始化',
                    '运行时计算产生',
                ],
                estimated_human_value='high' if runtime_defined > 3 else 'medium',
                interaction_cost='low',
                root_cause_hint='dynamic_step_source',
                source_files=['step_table_discovery.json'],
            ))

    # Check steps_raw.json for step universe anomalies
    steps_raw = _load_json(output_dir / 'steps_raw.json')
    if steps_raw and isinstance(steps_raw, list):
        total_steps = len(steps_raw)
        no_comment = sum(1 for s in steps_raw if not s.get('comment'))
        no_actions = sum(1 for s in steps_raw if not s.get('actions'))

        metrics = {
            'total_steps': total_steps,
            'steps_without_comment': no_comment,
            'steps_without_actions': no_actions,
        }

        if total_steps == 0:
            risks.append(DecisionRisk(
                risk_id='A002',
                area=AREA_A_STEP_DISCOVERY,
                category='empty_step_universe',
                trigger_type=TRIGGER_EXECUTION_RECOVERY,
                severity=SEV_BLOCKED,
                description='Step universe为空，步序发现可能完全失败。',
                triggering_evidence=['steps_raw.json: 0 steps'],
                estimated_affected_steps=0,
                estimated_affected_rows=0,
                estimated_human_value='high',
                interaction_cost='medium',
                root_cause_hint='step_discovery_failure',
                source_files=['steps_raw.json'],
            ))
        else:
            no_comment_ratio = no_comment / total_steps if total_steps else 0
            if no_comment_ratio > 0.3:
                risks.append(DecisionRisk(
                    risk_id='A003',
                    area=AREA_A_STEP_DISCOVERY,
                    category='low_comment_coverage',
                    trigger_type=TRIGGER_DECISION_RISK,
                    severity=SEV_NEEDS_AUTO,
                    description=(
                        '{:.0%}的步序没有注释，可能影响语义恢复。'
                        '({}/{})'.format(
                            no_comment_ratio, no_comment, total_steps)),
                    triggering_evidence=[
                        'steps_raw.json: {}/{} steps without comment'.format(
                            no_comment, total_steps)],
                    affected_scope={
                        'no_comment_count': no_comment,
                        'total_steps': total_steps,
                    },
                    estimated_affected_steps=no_comment,
                    estimated_affected_rows=no_comment,
                    estimated_human_value='low',
                    interaction_cost='low',
                    root_cause_hint='missing_comments',
                    source_files=['steps_raw.json'],
                ))

        area.metrics = metrics

    # Determine overall severity
    if any(r.severity == SEV_BLOCKED for r in risks):
        area.overall_severity = SEV_BLOCKED
    elif any(r.severity == SEV_HUMAN_CANDIDATE for r in risks):
        area.overall_severity = SEV_HUMAN_CANDIDATE
    elif any(r.severity == SEV_NEEDS_AUTO for r in risks):
        area.overall_severity = SEV_NEEDS_AUTO
    else:
        area.overall_severity = SEV_TRUSTED

    area.risks = risks
    area.summary = '{} risks identified'.format(len(risks))
    return area


def assess_program_logic(output_dir: Path,
                         project_name: str) -> AreaAssessment:
    """Area B -- Program Logic quality assessment.

    Checks:
        - normal successor fallback ratio
        - gold match rate for fallbacks
        - ambiguous dataflow
        - fallback pattern distribution
    """
    area = AreaAssessment(area=AREA_B_PROGRAM_LOGIC)
    risks = []

    fallback_audit = _load_json(output_dir / 'fallback_successor_audit.json')
    if fallback_audit and isinstance(fallback_audit, dict):
        summary = fallback_audit.get('summary', {})
        total_fallback = summary.get('total_fallback_steps', 0)
        pattern_3 = summary.get('pattern_3_min_target', 0)
        pattern_4 = summary.get('pattern_4_non_terminal', 0)
        gold_available = summary.get('gold_available_for', 0)
        gold_matched = summary.get('gold_matched_selected', 0)
        gold_rate = summary.get('gold_match_rate', None)
        suggestions = summary.get('pattern_suggestions', {})

        metrics = {
            'total_fallback_steps': total_fallback,
            'pattern_3_min_target': pattern_3,
            'pattern_4_non_terminal': pattern_4,
            'gold_available_for': gold_available,
            'gold_matched_selected': gold_matched,
            'gold_match_rate': gold_rate,
            'pattern_suggestions': suggestions,
        }
        area.metrics = metrics

        # Risk: high fallback ratio with low gold match
        if total_fallback > 50:
            severity = SEV_HUMAN_CANDIDATE
            if gold_rate is not None and gold_rate < 10:
                severity = SEV_HUMAN_CANDIDATE
            elif gold_rate is None and total_fallback > 100:
                severity = SEV_NEEDS_AUTO

            risks.append(DecisionRisk(
                risk_id='B001',
                area=AREA_B_PROGRAM_LOGIC,
                category='normal_successor_fallback',
                trigger_type=TRIGGER_DECISION_RISK,
                severity=severity,
                description=(
                    '大量步序的Normal Successor依赖fallback策略。'
                    '总fallback步数: {} (min-target: {}, non-terminal: {})。'
                    'Gold匹配率: {} (可用gold: {})。'.format(
                        total_fallback, pattern_3, pattern_4,
                        '{:.1f}%'.format(gold_rate) if gold_rate else 'N/A',
                        gold_available)),
                triggering_evidence=[
                    'fallback_successor_audit.json',
                    'pattern_3 (min_target_fallback): {}'.format(pattern_3),
                    'pattern_4 (non_terminal_fallback): {}'.format(pattern_4),
                    'gold_match_rate: {}'.format(gold_rate),
                ],
                affected_scope={
                    'total_fallback': total_fallback,
                    'pattern_3': pattern_3,
                    'pattern_4': pattern_4,
                    'gold_match_rate': gold_rate,
                },
                estimated_affected_steps=total_fallback,
                estimated_affected_rows=total_fallback,
                competing_hypotheses=[
                    '多分支通过动作完成条件区分正常路径',
                    'NG/重试计数与回跳逻辑区分路径',
                    '多分支均为合法生产路径，无唯一正常后继',
                    '正常路径由配方或运行模式决定',
                ],
                evidence_conflicts=[
                    'min-target fallback可能在多合法路径时选错',
                    'non-terminal fallback无法区分正常与条件分支',
                ],
                estimated_human_value='high' if total_fallback > 100 else 'medium',
                interaction_cost='low',
                root_cause_hint='multi_branch_successor_ambiguity',
                source_files=['fallback_successor_audit.json'],
            ))

        # Risk: gold contradicts min-target
        gold_contradicts = suggestions.get('gold_contradicts_min_target', 0)
        if gold_contradicts > 0:
            risks.append(DecisionRisk(
                risk_id='B002',
                area=AREA_B_PROGRAM_LOGIC,
                category='gold_contradicts_fallback',
                trigger_type=TRIGGER_DECISION_RISK,
                severity=SEV_NEEDS_AUTO,
                description=(
                    'Gold数据显示min-target fallback在{}个案例中选错。'.format(
                        gold_contradicts)),
                triggering_evidence=[
                    'gold_contradicts_min_target: {}'.format(gold_contradicts),
                ],
                estimated_affected_steps=gold_contradicts,
                estimated_affected_rows=gold_contradicts,
                estimated_human_value='medium',
                interaction_cost='low',
                root_cause_hint='min_target_unreliable',
                source_files=['fallback_successor_audit.json'],
            ))

    # Determine overall severity
    if any(r.severity == SEV_BLOCKED for r in risks):
        area.overall_severity = SEV_BLOCKED
    elif any(r.severity == SEV_HUMAN_CANDIDATE for r in risks):
        area.overall_severity = SEV_HUMAN_CANDIDATE
    elif any(r.severity == SEV_NEEDS_AUTO for r in risks):
        area.overall_severity = SEV_NEEDS_AUTO
    else:
        area.overall_severity = SEV_TRUSTED

    area.risks = risks
    area.summary = '{} risks identified'.format(len(risks))
    return area


def assess_semantic_grounding(output_dir: Path,
                              project_name: str) -> AreaAssessment:
    """Area C -- Semantic Grounding quality assessment.

    Checks:
        - engineering_actor coverage
        - description quality distribution (strong/usable/weak)
        - evidence source conflicts
        - CMT-only descriptions
    """
    area = AreaAssessment(area=AREA_C_SEMANTIC_GROUNDING)
    risks = []

    # Check v3_rebuild_stats.json
    v3_stats = _load_json(output_dir / 'v3_rebuild_stats.json')
    if v3_stats and isinstance(v3_stats, dict):
        eng_actor = v3_stats.get('engineering_actor_coverage', {})
        has_eng = eng_actor.get('has_eng_actor', 0)
        no_eng = eng_actor.get('no_eng_actor', 0)
        total_actions = eng_actor.get('total_actions', 0)
        coverage_pct = eng_actor.get('coverage_pct', 0)

        quality_dist = v3_stats.get('quality_distribution', {})
        strong_count = quality_dist.get('strong', 0)
        usable_count = quality_dist.get('usable', 0)
        weak_count = quality_dist.get('weak', 0)

        metrics = {
            'engineering_actor_coverage_pct': coverage_pct,
            'has_eng_actor': has_eng,
            'no_eng_actor': no_eng,
            'total_actions': total_actions,
            'strong_descriptions': strong_count,
            'usable_descriptions': usable_count,
            'weak_descriptions': weak_count,
        }
        area.metrics = metrics

        # Risk: low engineering actor coverage
        if coverage_pct < 50 and total_actions > 10:
            risks.append(DecisionRisk(
                risk_id='C001',
                area=AREA_C_SEMANTIC_GROUNDING,
                category='engineering_actor_gap',
                trigger_type=TRIGGER_EXPLORATION_EFFICIENCY,
                severity=SEV_HUMAN_CANDIDATE if coverage_pct == 0 else SEV_NEEDS_AUTO,
                description=(
                    'Engineering Actor覆盖率仅{:.1f}% ({}/{})。'
                    '大量动作缺乏工程机构关联。'.format(
                        coverage_pct, has_eng, total_actions)),
                triggering_evidence=[
                    'v3_rebuild_stats.json: coverage_pct={}'.format(
                        coverage_pct),
                    'has_eng_actor={}, total_actions={}'.format(
                        has_eng, total_actions),
                ],
                affected_scope={
                    'no_eng_actor_count': no_eng,
                    'total_actions': total_actions,
                    'coverage_pct': coverage_pct,
                },
                estimated_affected_steps=no_eng,
                estimated_affected_rows=no_eng,
                competing_hypotheses=[
                    'PLC变量声明或相关注释提供机构名称',
                    'FB实例及输出参数关联EC/地址',
                    'HMI手动操作页面及控件绑定',
                    '外部工程配置或设备库',
                ],
                estimated_human_value='high' if no_eng > 50 else 'medium',
                interaction_cost='low',
                root_cause_hint='actor_identity_mapping_gap',
                source_files=['v3_rebuild_stats.json'],
            ))

        # Risk: no strong descriptions
        if strong_count == 0 and total_actions > 10:
            risks.append(DecisionRisk(
                risk_id='C002',
                area=AREA_C_SEMANTIC_GROUNDING,
                category='no_strong_descriptions',
                trigger_type=TRIGGER_DECISION_RISK,
                severity=SEV_NEEDS_AUTO,
                description=(
                    '所有{}个动作描述均为usable或weak，没有strong级别描述。'
                    '可能存在CMT-only描述被当作工程动作的问题。'.format(
                        total_actions)),
                triggering_evidence=[
                    'quality_distribution: {}'.format(quality_dist),
                ],
                affected_scope={
                    'strong': strong_count,
                    'usable': usable_count,
                    'weak': weak_count,
                },
                estimated_affected_steps=total_actions,
                estimated_affected_rows=total_actions,
                estimated_human_value='medium',
                interaction_cost='low',
                root_cause_hint='cmt_only_descriptions',
                source_files=['v3_rebuild_stats.json'],
            ))

    # Check final_timing_rows for description quality
    ftr = _load_json(output_dir / 'final_timing_rows_v3.json')
    if ftr and isinstance(ftr, dict):
        rows = ftr.get('rows', [])
        if rows:
            desc_quality_dist = {}
            cmt_only_count = 0
            for row in rows:
                dq = row.get('description_quality', 'unknown')
                desc_quality_dist[dq] = desc_quality_dist.get(dq, 0) + 1

                for comp in row.get('description_components', []):
                    sources = comp.get('evidence_sources', [])
                    if (len(sources) == 1
                            and 'source_native_step_semantic' in sources):
                        cmt_only_count += 1

            area.metrics['row_description_quality'] = desc_quality_dist
            area.metrics['cmt_only_components'] = cmt_only_count

            if cmt_only_count > len(rows) * 0.5:
                risks.append(DecisionRisk(
                    risk_id='C003',
                    area=AREA_C_SEMANTIC_GROUNDING,
                    category='cmt_dominant_descriptions',
                    trigger_type=TRIGGER_DECISION_RISK,
                    severity=SEV_HUMAN_CANDIDATE,
                    description=(
                        '{}/{}行的描述主要来自CMT/注释，'
                        '可能仅表示记忆判断而非工程动作。'.format(
                            cmt_only_count, len(rows))),
                    triggering_evidence=[
                        'cmt_only_components: {}/{}'.format(
                            cmt_only_count, len(rows)),
                    ],
                    estimated_affected_steps=cmt_only_count,
                    estimated_affected_rows=cmt_only_count,
                    competing_hypotheses=[
                        'CMT确实描述了工程动作',
                        'CMT仅表示记忆判断/状态保存，不是独立工程动作',
                        'CMT描述了动作但缺少机构信息',
                    ],
                    estimated_human_value='high',
                    interaction_cost='low',
                    root_cause_hint='cmt_semantic_ambiguity',
                    source_files=['final_timing_rows_v3.json'],
                ))

    # Determine overall severity
    if any(r.severity == SEV_BLOCKED for r in risks):
        area.overall_severity = SEV_BLOCKED
    elif any(r.severity == SEV_HUMAN_CANDIDATE for r in risks):
        area.overall_severity = SEV_HUMAN_CANDIDATE
    elif any(r.severity == SEV_NEEDS_AUTO for r in risks):
        area.overall_severity = SEV_NEEDS_AUTO
    else:
        area.overall_severity = SEV_TRUSTED

    area.risks = risks
    area.summary = '{} risks identified'.format(len(risks))
    return area


def assess_final_timing_row(output_dir: Path,
                            project_name: str) -> AreaAssessment:
    """Area D -- FinalTimingRow quality assessment.

    Checks:
        - fallback timing boundary ratio
        - composite row merging uncertainty
        - row granularity issues
        - row status distribution
    """
    area = AreaAssessment(area=AREA_D_FINAL_TIMING_ROW)
    risks = []

    ftr = _load_json(output_dir / 'final_timing_rows_v3.json')
    if ftr and isinstance(ftr, dict):
        rows = ftr.get('rows', [])
        total_rows = len(rows)

        fallback_end_count = 0
        fallback_start_count = 0
        multi_step_rows = 0
        composite_rows = 0

        for row in rows:
            timing_end = row.get('timing_end', {})
            timing_start = row.get('timing_start', {})

            if timing_end.get('is_fallback', False):
                fallback_end_count += 1
            if timing_start.get('is_fallback', False):
                fallback_start_count += 1

            source_steps = row.get('source_steps', [])
            if len(source_steps) > 1:
                multi_step_rows += 1

            sync_actions = row.get('synchronized_actions', [])
            if sync_actions:
                composite_rows += 1

        metrics = {
            'total_rows': total_rows,
            'rows_with_fallback_end': fallback_end_count,
            'rows_with_fallback_start': fallback_start_count,
            'multi_step_rows': multi_step_rows,
            'composite_rows': composite_rows,
            'fallback_end_ratio': (
                fallback_end_count / total_rows if total_rows else 0),
        }
        area.metrics = metrics

        # Risk: high fallback timing boundary ratio
        if total_rows > 0:
            fallback_ratio = fallback_end_count / total_rows
            if fallback_ratio > 0.3:
                risks.append(DecisionRisk(
                    risk_id='D001',
                    area=AREA_D_FINAL_TIMING_ROW,
                    category='timing_boundary_fallback',
                    trigger_type=TRIGGER_DECISION_RISK,
                    severity=SEV_HUMAN_CANDIDATE,
                    description=(
                        '{:.0%}({}/{})的FinalTimingRow结束标签依赖fallback，'
                        '时序边界可靠性不足。'.format(
                            fallback_ratio, fallback_end_count, total_rows)),
                    triggering_evidence=[
                        'fallback_end: {}/{}'.format(
                            fallback_end_count, total_rows),
                    ],
                    affected_scope={
                        'fallback_end_count': fallback_end_count,
                        'total_rows': total_rows,
                    },
                    estimated_affected_steps=fallback_end_count,
                    estimated_affected_rows=fallback_end_count,
                    estimated_human_value='high',
                    interaction_cost='low',
                    root_cause_hint='timing_boundary_uncertainty',
                    source_files=['final_timing_rows_v3.json'],
                ))

        # Risk: composite row merging uncertainty
        if composite_rows > total_rows * 0.2 and total_rows > 20:
            risks.append(DecisionRisk(
                risk_id='D002',
                area=AREA_D_FINAL_TIMING_ROW,
                category='composite_row_uncertainty',
                trigger_type=TRIGGER_DECISION_RISK,
                severity=SEV_HUMAN_CANDIDATE,
                description=(
                    '{}个行为Composite Row(含同步动作)，'
                    '合并粒度可能不符合时序采集目标。'.format(
                        composite_rows)),
                triggering_evidence=[
                    'composite_rows: {}'.format(composite_rows),
                ],
                affected_scope={
                    'composite_rows': composite_rows,
                    'total_rows': total_rows,
                },
                estimated_affected_rows=composite_rows,
                competing_hypotheses=[
                    '共享同一计时区间的同步动作组合成一行',
                    '不同执行机构独立形成时序行',
                    '主动作与辅助动作组合，内部不独立计时',
                ],
                estimated_human_value='medium',
                interaction_cost='low',
                root_cause_hint='row_granularity_ambiguity',
                source_files=['final_timing_rows_v3.json'],
            ))

    # Determine overall severity
    if any(r.severity == SEV_BLOCKED for r in risks):
        area.overall_severity = SEV_BLOCKED
    elif any(r.severity == SEV_HUMAN_CANDIDATE for r in risks):
        area.overall_severity = SEV_HUMAN_CANDIDATE
    elif any(r.severity == SEV_NEEDS_AUTO for r in risks):
        area.overall_severity = SEV_NEEDS_AUTO
    else:
        area.overall_severity = SEV_TRUSTED

    area.risks = risks
    area.summary = '{} risks identified'.format(len(risks))
    return area


def assess_runtime_trace(output_dir: Path,
                         project_name: str) -> AreaAssessment:
    """Area E -- Runtime Trace quality assessment.

    Checks:
        - trace readiness gate results
        - observable boundaries
        - trace coverage
    """
    area = AreaAssessment(area=AREA_E_RUNTIME_TRACE)
    risks = []

    trace_gate = _load_json(output_dir / 'trace_readiness_gate.json')
    if trace_gate and isinstance(trace_gate, dict):
        all_passed = trace_gate.get('all_gates_passed', True)
        gates = trace_gate.get('gates', [])
        summary = trace_gate.get('summary', {})

        metrics = {
            'all_gates_passed': all_passed,
            'total_gates': summary.get('total_gates', 0),
            'passed': summary.get('passed', 0),
            'failed': summary.get('failed', 0),
        }
        area.metrics = metrics

        failed_gates = [g for g in gates if not g.get('passed', True)]
        if failed_gates:
            for g in failed_gates:
                violations = g.get('violations', [])
                risks.append(DecisionRisk(
                    risk_id='E001_{}'.format(g.get('gate', '')),
                    area=AREA_E_RUNTIME_TRACE,
                    category='trace_gate_failure',
                    trigger_type=TRIGGER_EXECUTION_RECOVERY,
                    severity=SEV_NEEDS_AUTO,
                    description=(
                        'Trace readiness gate {} ({}) failed: '
                        '{} violations'.format(
                            g.get('gate', ''),
                            g.get('name', ''),
                            len(violations))),
                    triggering_evidence=[
                        'gate: {}'.format(g.get('gate')),
                        'name: {}'.format(g.get('name')),
                        'violations: {}'.format(len(violations)),
                    ],
                    estimated_affected_rows=len(violations),
                    estimated_human_value='medium',
                    interaction_cost='low',
                    root_cause_hint='trace_readiness',
                    source_files=['trace_readiness_gate.json'],
                ))

    # Determine overall severity
    if any(r.severity == SEV_BLOCKED for r in risks):
        area.overall_severity = SEV_BLOCKED
    elif any(r.severity == SEV_HUMAN_CANDIDATE for r in risks):
        area.overall_severity = SEV_HUMAN_CANDIDATE
    elif any(r.severity == SEV_NEEDS_AUTO for r in risks):
        area.overall_severity = SEV_NEEDS_AUTO
    else:
        area.overall_severity = SEV_TRUSTED

    area.risks = risks
    area.summary = '{} risks identified'.format(len(risks))
    return area


# ============================================================
# Full Assessment
# ============================================================

def assess_all(output_dir: Path, project_name: str,
               workflow_stage: str = 'post_final_timing') -> DecisionQualityAssessment:
    """Run all five area assessments and produce the top-level result.

    Args:
        output_dir: Path to the project's output/ directory
        project_name: Project name (e.g. '包mylar')
        workflow_stage: Current workflow stage

    Returns:
        DecisionQualityAssessment with all areas assessed.
    """
    areas = [
        assess_step_discovery(output_dir, project_name),
        assess_program_logic(output_dir, project_name),
        assess_semantic_grounding(output_dir, project_name),
        assess_final_timing_row(output_dir, project_name),
        assess_runtime_trace(output_dir, project_name),
    ]

    # Aggregate
    all_risks = []
    for a in areas:
        all_risks.extend(a.risks)

    risks_by_severity = {}
    for r in all_risks:
        risks_by_severity[r.severity] = risks_by_severity.get(
            r.severity, 0) + 1

    risks_by_area = {}
    for a in areas:
        risks_by_area[a.area] = len(a.risks)

    human_candidates = [r.risk_id for r in all_risks
                        if r.severity == SEV_HUMAN_CANDIDATE]
    auto_candidates = [r.risk_id for r in all_risks
                       if r.severity == SEV_NEEDS_AUTO]
    blocked = [a.area for a in areas if a.overall_severity == SEV_BLOCKED]

    # Overall quality = worst area severity
    if blocked:
        overall = SEV_BLOCKED
    elif human_candidates:
        overall = SEV_HUMAN_CANDIDATE
    elif auto_candidates:
        overall = SEV_NEEDS_AUTO
    else:
        overall = SEV_TRUSTED

    # Baseline metrics
    baseline = {}
    for a in areas:
        baseline[a.area] = a.metrics

    return DecisionQualityAssessment(
        project=project_name,
        timestamp=datetime.now().isoformat(),
        workflow_stage=workflow_stage,
        areas=areas,
        overall_quality=overall,
        total_risks=len(all_risks),
        risks_by_severity=risks_by_severity,
        risks_by_area=risks_by_area,
        human_guidance_candidates=human_candidates,
        auto_investigation_candidates=auto_candidates,
        blocked_areas=blocked,
        baseline_metrics=baseline,
    )
