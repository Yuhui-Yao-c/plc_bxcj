#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dlib/timing_intent.py — TimingIntentSpec 8 类型 + 动态选择器（v10.38 P5）

计时表达类型（不是按项目硬编码的策略）：
  - STATE_TO_NEXT: 当前状态到下一业务状态
  - STATE_RESIDENCE: 当前状态退出（NOT state）
  - FEEDBACK_COMPLETE: 状态保持期间等待反馈到位
  - COMPOUND_FEEDBACK: 状态 AND 反馈复合完成条件
  - CROSS_THREAD_SYNC: 跨线程同步
  - MULTI_STATE_ACTION: 跨多个内部 Step 的动作
  - COMPOSITE_PROCESS: 复合工艺片段（含多个子工艺）
  - UNRESOLVED: 无法判定

所有计时表达式必须由结构化 Boolean AST 产生，不能只做字符串正则替换。
一个计时定义有唯一 selected_start 与 selected_end。
备选边界进入证据表，不直接扩张主表动作数。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any
from collections import defaultdict


class TimingIntentType(Enum):
    STATE_TO_NEXT = "STATE_TO_NEXT"
    STATE_RESIDENCE = "STATE_RESIDENCE"
    FEEDBACK_COMPLETE = "FEEDBACK_COMPLETE"
    COMPOUND_FEEDBACK = "COMPOUND_FEEDBACK"
    CROSS_THREAD_SYNC = "CROSS_THREAD_SYNC"
    MULTI_STATE_ACTION = "MULTI_STATE_ACTION"
    COMPOSITE_PROCESS = "COMPOSITE_PROCESS"
    UNRESOLVED = "UNRESOLVED"


@dataclass
class BooleanASTNode:
    """Boolean 表达式 AST 节点。"""
    node_type: str
    value: str = ""
    operator: str = ""
    operands: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'node_type': self.node_type,
            'value': self.value,
            'operator': self.operator,
            'operands': [o.to_dict() if isinstance(o, BooleanASTNode) else o for o in self.operands],
        }


@dataclass
class TimingIntentSpec:
    """一个 ActionSpan 的计时意图规约。

    一个计时定义有唯一 selected_start 与 selected_end。
    备选边界进入 alternative_starts / alternative_ends，不扩张主表。
    """
    intent_type: TimingIntentType = TimingIntentType.UNRESOLVED
    selected_start: str = ""
    selected_end: str = ""
    start_ast: dict = None
    end_ast: dict = None
    alternative_starts: list = field(default_factory=list)
    alternative_ends: list = field(default_factory=list)
    selection_reason: str = ""
    selection_evidence: dict = field(default_factory=dict)
    source_provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'intent_type': self.intent_type.value,
            'selected_start': self.selected_start,
            'selected_end': self.selected_end,
            'start_ast': self.start_ast,
            'end_ast': self.end_ast,
            'alternative_starts': self.alternative_starts,
            'alternative_ends': self.alternative_ends,
            'selection_reason': self.selection_reason,
            'selection_evidence': self.selection_evidence,
            'source_provenance': self.source_provenance,
        }


class BooleanExprParser:
    """将 PLC 标签字符串解析为 BooleanASTNode。

    支持的形式：
      - ST01.AutoStep[2]=120
      - NOT abEM01ActionStart[3]
      - ST01.AutoStep[2]=125 OR ST01.AutoStep[2]=126
      - ST01.AutoStep[2]=10 AND SV13.PosOK[1] AND SV18.PosOK[1]
      - (m_nNextAutoStep[39]=INT#220 OR m_nNextAutoStep[39]=INT#320) AND m_nNextAutoStep[40]=INT#20
      - NOT (m_nNextAutoStep[2]=INT#1130) AND NOT (m_nNextAutoStep[2]=INT#5010)
    """

    def parse(self, expr: str) -> dict:
        if not expr:
            return None
        expr = expr.strip()
        node = self._parseOr(expr)
        return node.to_dict() if node else None

    def _parseOr(self, expr: str) -> BooleanASTNode:
        parts = self._splitTopLevel(expr, ' OR ')
        if len(parts) > 1:
            operands = [self._parseAnd(p) for p in parts]
            operands = [o for o in operands if o]
            if len(operands) > 1:
                return BooleanASTNode(node_type="operator", operator="OR", operands=operands)
            return operands[0] if operands else None
        return self._parseAnd(expr)

    def _parseAnd(self, expr: str) -> BooleanASTNode:
        parts = self._splitTopLevel(expr, ' AND ')
        if len(parts) > 1:
            operands = [self._parseNot(p) for p in parts]
            operands = [o for o in operands if o]
            if len(operands) > 1:
                return BooleanASTNode(node_type="operator", operator="AND", operands=operands)
            return operands[0] if operands else None
        return self._parseNot(expr)

    def _parseNot(self, expr: str) -> BooleanASTNode:
        expr = expr.strip()
        if expr.upper().startswith('NOT '):
            inner = expr[4:].strip()
            if inner.startswith('(') and inner.endswith(')'):
                inner = inner[1:-1].strip()
            operand = self._parseOr(inner)
            if operand:
                return BooleanASTNode(node_type="operator", operator="NOT", operands=[operand])
            return None
        if expr.startswith('(') and expr.endswith(')'):
            return self._parseOr(expr[1:-1].strip())
        return self._parseLeaf(expr)

    def _parseLeaf(self, expr: str) -> BooleanASTNode:
        expr = expr.strip()
        if not expr:
            return None
        m = re.match(r'([^\s=<>!]+)\s*(=|<>|!=|>=|<=|>|<)\s*(.+)', expr)
        if m:
            return BooleanASTNode(
                node_type="comparison",
                value=expr,
                operator=m.group(2),
                operands=[
                    BooleanASTNode(node_type="variable_ref", value=m.group(1).strip()),
                    BooleanASTNode(node_type="literal", value=m.group(3).strip()),
                ]
            )
        return BooleanASTNode(node_type="variable_ref", value=expr)

    def _splitTopLevel(self, expr: str, sep: str) -> list:
        parts = []
        depth = 0
        current = ""
        i = 0
        while i < len(expr):
            if expr[i] == '(':
                depth += 1
                current += expr[i]
            elif expr[i] == ')':
                depth -= 1
                current += expr[i]
            elif depth == 0 and expr[i:i+len(sep)].upper() == sep:
                parts.append(current)
                current = ""
                i += len(sep)
                continue
            else:
                current += expr[i]
            i += 1
        if current.strip():
            parts.append(current)
        return parts


class TimingIntentSelector:
    """动态选择 TimingIntentSpec。

    根据 ActionSpan 的证据（command/completion/transition/condition）
    和项目配置（允许的工艺阶段先验）动态选择计时类型，
    而非全局固定 end_tag 优先级。

    项目专项配置：
      - transfer: 默认 STATE_TO_NEXT，支持跨内部 Step 和独立同步动作
      - mylar: 充分支持 STATE_RESIDENCE + A/B 分别计时 + 多状态合并
      - ultrasound: 支持 STATE_TO_NEXT + FEEDBACK_COMPLETE + 复合 AND/OR + 跨线程

    不能将人工后补 ActionStart 直接作为自动生成依据。
    """

    def __init__(self, project: str, cfg: dict = None):
        self.project = project
        self.cfg = cfg or {}
        self.parser = BooleanExprParser()
        proj_cfg = self.cfg.get('timing_intent', {}).get(project, {})
        self.default_intent = proj_cfg.get('default', self._projectDefault(project))
        self.allowed_intents = proj_cfg.get('allowed', [t.value for t in TimingIntentType])
        self.backfill_pattern = re.compile(
            r'(ab|at|artg|li)EM\d+(_\d+)?Action(Start|Done)'
        )

    def _projectDefault(self, project: str) -> str:
        defaults = {
            'transfer': TimingIntentType.STATE_TO_NEXT.value,
            'ultrasound': TimingIntentType.STATE_TO_NEXT.value,
            'mylar': TimingIntentType.STATE_RESIDENCE.value,
        }
        return defaults.get(project, TimingIntentType.STATE_TO_NEXT.value)

    def select(self, span) -> TimingIntentSpec:
        """为 ActionSpan 选择 TimingIntentSpec。

        span 可以是 EngineeringActionSpan 对象或 dict。
        """
        if hasattr(span, 'to_dict'):
            s = span.to_dict()
        elif isinstance(span, dict):
            s = span
        else:
            return TimingIntentSpec(
                intent_type=TimingIntentType.UNRESOLVED,
                selection_reason="invalid_span_object"
            )

        cmds = s.get('command_evidence', [])
        comps_raw = s.get('completion_evidence', [])
        covered = s.get('covered_steps', [])
        si = s.get('step_identity', {})

        comps = [c for c in comps_raw if not self._isBackfillRef(c.get('variable', ''))]

        start_label = self._formatStepLabel(si)
        candidate_ends = self._collectEndCandidates(s)
        candidate_ends = [e for e in candidate_ends if not self._isBackfillRef(e)]

        if not candidate_ends:
            candidate_ends = [self._formatNextStateLabel(si)]

        intent_type, selected_end, reason = self._chooseIntent(
            cmds, comps, covered, candidate_ends, si)

        selected_start = start_label
        start_ast = self.parser.parse(selected_start)
        end_ast = self.parser.parse(selected_end)
        alt_ends = [e for e in candidate_ends if e != selected_end]
        alt_starts = []

        spec = TimingIntentSpec(
            intent_type=intent_type,
            selected_start=selected_start,
            selected_end=selected_end,
            start_ast=start_ast,
            end_ast=end_ast,
            alternative_starts=alt_starts,
            alternative_ends=alt_ends,
            selection_reason=reason,
            selection_evidence={
                'cmd_count': len(cmds),
                'comp_count': len(comps),
                'covered_step_count': len(covered),
                'has_interface_cmd': any(c.get('is_interface_cmd') for c in cmds),
            },
            source_provenance={
                'project': self.project,
                'selector': 'TimingIntentSelector v1.0',
            }
        )
        return spec

    def _formatStepLabel(self, si: dict) -> str:
        sv = si.get('state_variable', '')
        st = si.get('station', '')
        th = si.get('thread_idx', -1)
        sv_val = si.get('step_value', -1)
        if sv.startswith('m_nNextAutoStep'):
            return f"m_nNextAutoStep[{th}]=INT#{sv_val}"
        if st and th >= 0:
            return f"{st}.{sv}[{th}]={sv_val}"
        if th >= 0:
            return f"{sv}[{th}]={sv_val}"
        return f"{sv}={sv_val}"

    def _formatNextStateLabel(self, si: dict) -> str:
        sv = si.get('state_variable', '')
        st = si.get('station', '')
        th = si.get('thread_idx', -1)
        sv_val = si.get('step_value', -1)
        next_val = sv_val + 10 if sv_val > 0 else 10
        if sv.startswith('m_nNextAutoStep'):
            return f"m_nNextAutoStep[{th}]=INT#{next_val}"
        if st:
            return f"{st}.{sv}[{th}]={next_val}"
        return f"{sv}[{th}]={next_val}"

    def _formatResidenceLabel(self, si: dict) -> str:
        sv = si.get('state_variable', '')
        st = si.get('station', '')
        th = si.get('thread_idx', -1)
        sv_val = si.get('step_value', -1)
        if sv.startswith('m_nNextAutoStep'):
            return f"NOT (m_nNextAutoStep[{th}]=INT#{sv_val})"
        if st:
            return f"NOT {st}.{sv}[{th}]={sv_val}"
        return f"NOT {sv}[{th}]={sv_val}"

    def _collectEndCandidates(self, span_dict: dict) -> list:
        candidates = []
        si = span_dict.get('step_identity', {})
        sv = si.get('state_variable', '')

        next_state = self._formatNextStateLabel(si)
        candidates.append(next_state)

        residence = self._formatResidenceLabel(si)
        candidates.append(residence)

        for comp in span_dict.get('completion_evidence', []):
            var = comp.get('variable', '')
            if var:
                candidates.append(var)

        covered = span_dict.get('covered_steps', [])
        if len(covered) > 1:
            last_step = covered[-1] if isinstance(covered[-1], dict) else {}
            last_si = last_step.get('step_identity', last_step)
            if last_si and last_si != si:
                candidates.append(self._formatStepLabel(last_si))

        return candidates

    def _chooseIntent(self, cmds: list, comps: list, covered: list,
                      candidates: list, si: dict) -> tuple:
        has_completion = len(comps) > 0
        has_interface_cmd = any(c.get('is_interface_cmd') for c in cmds)
        covered_count = len(covered)
        has_residence_candidate = any(c.upper().startswith('NOT ') for c in candidates)
        has_feedback_candidate = any(
            not c.upper().startswith('NOT ') and
            '=' not in c.split('.')[-1] if '.' in c else '=' not in c
            for c in candidates
        )

        if self.project == 'mylar' and has_residence_candidate:
            residence = self._formatResidenceLabel(si)
            return (TimingIntentType.STATE_RESIDENCE, residence,
                    "mylar_default_state_residence")

        if has_completion and has_interface_cmd and covered_count <= 2:
            for comp in comps:
                var = comp.get('variable', '')
                if var:
                    return (TimingIntentType.FEEDBACK_COMPLETE, var,
                           "feedback_signal_matched_interface_cmd")

        if has_completion and covered_count <= 2:
            for comp in comps:
                var = comp.get('variable', '')
                if var:
                    return (TimingIntentType.COMPOUND_FEEDBACK, var,
                           "feedback_without_interface_cmd")

        if covered_count > 2:
            next_state = self._formatNextStateLabel(si)
            return (TimingIntentType.MULTI_STATE_ACTION, next_state,
                   f"covers_{covered_count}_steps_aggregate")

        if has_interface_cmd and not has_completion:
            next_state = self._formatNextStateLabel(si)
            return (TimingIntentType.STATE_TO_NEXT, next_state,
                   "interface_cmd_no_completion_use_next_state")

        if self.default_intent == TimingIntentType.STATE_RESIDENCE.value and has_residence_candidate:
            residence = self._formatResidenceLabel(si)
            return (TimingIntentType.STATE_RESIDENCE, residence,
                   "project_default_residence")

        next_state = self._formatNextStateLabel(si)
        return (TimingIntentType.STATE_TO_NEXT, next_state,
               "project_default_state_to_next")

    def _isBackfillRef(self, label: str) -> bool:
        if not label:
            return False
        return bool(self.backfill_pattern.search(label))


def saveTimingIntents(specs: dict, output_path: Path):
    """保存 span_id -> TimingIntentSpec 映射到 JSON。"""
    out = {k: v.to_dict() if hasattr(v, 'to_dict') else v for k, v in specs.items()}
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
