#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bxcj_demo/lib/end_tag_selector.py — P1 结束标签选择器

策略（按优先级）：
  1. 普通动作默认使用经控制流验证的下一业务状态 (verified_normal_next_state)
  2. 有独立计时目的的动作（如机器人移动、单轴运动）使用完成信号作为结束标签
  3. 跨步完成信号在控制流可证明时可作为独立计时
  4. 多机构协同动作使用下一业务状态（不拆分为多个完成信号）
  5. 无法证明唯一结束条件时标黄

输入：workdir/output/ 下的 event_graph.json + step_logic_slices.json + extract_config.json
输出：workdir/output/end_tag_decisions.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class EndTagDecision:
    """每个 TimingAction 的结束标签决策。"""
    action_id: str
    step_identity: dict
    action_type: str
    selected_end_tag: str
    timing_intent: str            # "action_done" | "business_state" | "wait_signal" | "step_exit" | "ambiguous"
    completion_evidence: str      # 完成信号变量名（如有）
    alternative_end_tags: list    # 候选结束标签列表
    selection_reason: str         # 中文说明
    review_reason_code: str       # 黄色标记原因码（空字符串=无需复核）


# 独立计时目的的命令模式（这些命令的完成信号可作为独立计时）
INDEPENDENT_TIMING_PATTERNS = [
    r'\.In\.ToPos\[',
    r'\.In\.Location',
    r'\.AbsEx\[',
    r'\.AbsExecute\[',
    r'\.In\.ToHome',
]

# 协同动作模式（多机构同时执行，不应拆分为独立计时）
GROUPED_ACTION_PATTERNS = [
    r'\.In\.Auto_V\b',   # 气缸伸缩通常成组
    r'\.In\.Auto_R\b',
    r'\.Start\b',        # 通用启动信号
]


class EndTagSelector:
    """对 EngineeringActionPlanner 产出的动作做结束标签二次选择。"""

    def __init__(self, output_dir: Path, cfg: dict = None):
        self.output_dir = output_dir
        self.cfg = cfg or {}
        self.slices = []
        self.event_graph = {}
        self.steps_described = []
        self.config = {}
        self.slice_lookup = {}
        self.transition_lookup = {}
        self._load_inputs()

    def _load_inputs(self):
        """加载已生成的中间产物。"""
        for name, fn, default in [
            ('step_logic_slices.json', 'slices', []),
            ('event_graph.json', 'event_graph', {}),
            ('steps_described.json', 'steps_described', []),
            ('extract_config.json', 'config', {}),
        ]:
            p = self.output_dir / name
            if p.exists():
                with open(p, 'r', encoding='utf-8') as f:
                    setattr(self, fn, json.load(f))
            else:
                setattr(self, fn, default)

        # 建立 (station, thread, step) → slice 查找表
        for s in self.slices:
            si = s.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('step_value', -1))
            self.slice_lookup[key] = s
            # 收集转移信息
            trans_list = s.get('transitions', [])
            self.transition_lookup[key] = trans_list

    def _format_step_label(self, si: dict) -> str:
        """格式化步序标签（与 action_planner._format_step_label 保持一致）。"""
        sv = si.get('state_variable', '')
        st = si.get('station', '')
        th = si.get('thread_idx', -1)
        val = si.get('step_value', -1)
        if sv == 'Process_steps':
            return '%sV.Process_steps[%d]=%d' % (st, th, val)
        elif sv == 'AutoStep':
            return '%s.AutoStep[%d]=%d' % (st, th, val)
        elif sv == 'm_nNextAutoStep':
            return '%s[%d]=INT#%d' % (sv, th, val)
        elif sv:
            if st:
                return '%sV.%s[%d]=%d' % (st, sv, th, val)
            return '%s[%d]=INT#%d' % (sv, th, val)
        return ''

    def _get_step_transitions(self, si: dict) -> list:
        key = (si.get('station', ''), si.get('thread_idx', -1),
               si.get('step_value', -1))
        return self.transition_lookup.get(key, [])

    def _get_verified_next_state(self, si: dict) -> str:
        """获取经控制流验证的下一业务状态。

        判定：单一非零直接后继 → verified_normal_next_state
              多个不同 target → 空字符串（ambiguous）
              target=0 → NOT (current)
        """
        trans = self._get_step_transitions(si)
        if not trans:
            return ''
        # 收集所有 target
        targets = []
        for t in trans:
            ts = t.get('target_step', {})
            tv = ts.get('value', -1)
            tt = ts.get('thread', si.get('thread_idx', -1))
            if tv >= 0:
                targets.append((tt, tv))
        if not targets:
            return ''
        # 唯一性判断
        unique_targets = set(targets)
        if len(unique_targets) == 1:
            tt, tv = targets[0]
            next_si = {
                'station': si.get('station', ''),
                'thread_idx': tt,
                'state_variable': si.get('state_variable', ''),
                'step_value': tv,
            }
            return self._format_step_label(next_si)
        # 多分支：检查是否所有 target 都相同 step_value（条件不同但同目标）
        target_values = set(tv for _, tv in targets)
        if len(target_values) == 1:
            tt, tv = targets[0]
            next_si = {
                'station': si.get('station', ''),
                'thread_idx': tt,
                'state_variable': si.get('state_variable', ''),
                'step_value': tv,
            }
            return self._format_step_label(next_si)
        # 真正的多分支
        return ''

    def _has_independent_timing_purpose(self, commands: list,
                                         action_type: str) -> bool:
        """判断是否有独立计时目的。"""
        if not commands:
            return False
        # motion 类型且命令匹配独立计时模式
        if action_type == 'motion':
            for cmd in commands:
                for pat in INDEPENDENT_TIMING_PATTERNS:
                    if re.search(pat, cmd):
                        return True
        # 单命令的 motion/clamp/vacuum 也算独立计时
        if action_type in ('motion', 'clamp', 'vacuum') and len(commands) == 1:
            return True
        return False

    def _is_grouped_action(self, commands: list) -> bool:
        """判断是否为协同动作（多机构同时执行）。"""
        if len(commands) <= 1:
            return False
        # 检查是否所有命令都来自同一 actuator
        actuators = set()
        for cmd in commands:
            parts = cmd.split('.')
            if parts:
                actuators.add(parts[0])
        # 多个不同 actuator 的命令 = 协同
        return len(actuators) > 1

    def _find_completion_in_next_steps(self, si: dict, completion_var: str,
                                        max_lookahead: int = 3) -> bool:
        """检查完成信号是否在后续步的转移条件中出现（控制流可证明）。"""
        if not completion_var:
            return False
        station = si.get('station', '')
        thread = si.get('thread_idx', -1)
        step_val = si.get('step_value', -1)

        # 收集同 station/thread 的所有 step 值
        step_values = []
        for s in self.slices:
            s_si = s.get('step_identity', {})
            if (s_si.get('station', '') == station and
                s_si.get('thread_idx', -1) == thread):
                step_values.append(s_si.get('step_value', -1))
        step_values = sorted([v for v in step_values if v > step_val])

        for i, sv in enumerate(step_values[:max_lookahead]):
            key = (station, thread, sv)
            slice_data = self.slice_lookup.get(key, {})
            for t in slice_data.get('transitions', []):
                be = t.get('boolean_expr', {})
                trans_vars = self._extract_vars(be)
                if completion_var in trans_vars:
                    return True
        return False

    def _extract_vars(self, boolean_expr: dict) -> set:
        """从 boolean_expr 提取变量名。"""
        result = set()
        if not boolean_expr:
            return result
        atoms = boolean_expr.get('atoms', [])
        for atom in atoms:
            v = atom.get('variable', '')
            if v:
                result.add(v)
        # 也提取 raw 字符串中的变量
        raw = boolean_expr.get('raw', '')
        if raw:
            for m in re.finditer(r'([A-Za-z_][\w\.]*(?:\[\d+\])?)', raw):
                result.add(m.group(1))
        return result

    def _decide_one(self, action: dict) -> EndTagDecision:
        """对单个动作做结束标签决策。"""
        action_id = action.get('action_id', '')
        si = action.get('step_identity', {})
        action_type = action.get('action_type', 'transition')
        commands = action.get('commands', [])
        completions = action.get('completions', [])
        start_label = action.get('start_label', '')
        original_end = action.get('end_label', '')
        original_source = action.get('end_label_source', '')

        # 候选列表
        alternatives = []
        # 候选1: 完成信号
        completion_var = completions[0] if completions else ''
        # 候选2: 下一业务状态
        next_state = self._get_verified_next_state(si)
        # 候选3: NOT (current) - 步退出
        step_exit = f'NOT ({start_label})' if start_label else ''

        if completion_var:
            alternatives.append(completion_var)
        if next_state:
            alternatives.append(next_state)
        if step_exit:
            alternatives.append(step_exit)

        # ===== 决策逻辑 =====

        # wait 动作：使用等待信号
        if action_type == 'wait':
            return EndTagDecision(
                action_id=action_id,
                step_identity=si,
                action_type=action_type,
                selected_end_tag=original_end,
                timing_intent='wait_signal',
                completion_evidence='',
                alternative_end_tags=alternatives,
                selection_reason='等待动作使用等待信号作为结束标签',
                review_reason_code='',
            )

        # transition 动作：使用下一业务状态
        if action_type == 'transition':
            if next_state:
                return EndTagDecision(
                    action_id=action_id,
                    step_identity=si,
                    action_type=action_type,
                    selected_end_tag=next_state,
                    timing_intent='business_state',
                    completion_evidence='',
                    alternative_end_tags=alternatives,
                    selection_reason='纯转移步使用下一业务状态作为结束标签',
                    review_reason_code='',
                )
            return EndTagDecision(
                action_id=action_id,
                step_identity=si,
                action_type=action_type,
                selected_end_tag=step_exit or original_end,
                timing_intent='step_exit',
                completion_evidence='',
                alternative_end_tags=alternatives,
                selection_reason='无下一业务状态，使用步退出作为结束标签',
                review_reason_code='no_verified_next_state',
            )

        # 真实动作（motion/clamp/vacuum/action）
        has_independent_timing = self._has_independent_timing_purpose(commands, action_type)
        is_grouped = self._is_grouped_action(commands)

        # 有独立计时目的 + 完成信号存在 → 使用完成信号
        if has_independent_timing and completion_var:
            # 验证完成信号在控制流中可见（同步或跨步）
            in_current = self._is_completion_in_current_step(si, completion_var)
            in_next = self._find_completion_in_next_steps(si, completion_var)
            if in_current or in_next:
                timing_intent = 'action_done'
                reason = '命令有独立计时目的，使用完成信号作为结束标签（控制流验证）'
                if in_next and not in_current:
                    reason += '；完成信号在后续步转移条件中检查'
                return EndTagDecision(
                    action_id=action_id,
                    step_identity=si,
                    action_type=action_type,
                    selected_end_tag=completion_var,
                    timing_intent=timing_intent,
                    completion_evidence=completion_var,
                    alternative_end_tags=alternatives,
                    selection_reason=reason,
                    review_reason_code='',
                )

        # 协同动作 / 无独立计时目的 → 使用下一业务状态
        if next_state:
            timing_intent = 'business_state'
            if is_grouped:
                reason = '多机构协同动作，使用下一业务状态作为结束标签'
            elif not has_independent_timing:
                reason = '无独立计时目的，使用下一业务状态作为结束标签'
            else:
                reason = '使用下一业务状态作为结束标签'
            return EndTagDecision(
                action_id=action_id,
                step_identity=si,
                action_type=action_type,
                selected_end_tag=next_state,
                timing_intent=timing_intent,
                completion_evidence=completion_var,
                alternative_end_tags=alternatives,
                selection_reason=reason,
                review_reason_code='',
            )

        # 有完成信号但无下一状态 → 使用完成信号（标黄，因为控制流不可证明）
        if completion_var:
            return EndTagDecision(
                action_id=action_id,
                step_identity=si,
                action_type=action_type,
                selected_end_tag=completion_var,
                timing_intent='action_done',
                completion_evidence=completion_var,
                alternative_end_tags=alternatives,
                selection_reason='有完成信号但无验证的下一业务状态',
                review_reason_code='completion_without_next_state',
            )

        # 都没有 → 标黄
        return EndTagDecision(
            action_id=action_id,
            step_identity=si,
            action_type=action_type,
            selected_end_tag=step_exit or original_end,
            timing_intent='step_exit',
            completion_evidence='',
            alternative_end_tags=alternatives,
            selection_reason='无完成信号且无验证的下一业务状态',
            review_reason_code='no_verified_end_tag',
        )

    def _is_completion_in_current_step(self, si: dict, completion_var: str) -> bool:
        """完成信号是否在当前步的转移条件中。"""
        trans = self._get_step_transitions(si)
        for t in trans:
            be = t.get('boolean_expr', {})
            trans_vars = self._extract_vars(be)
            if completion_var in trans_vars:
                return True
        return False

    def _normalize_station(self, eg: dict) -> dict:
        """对 event_graph 中空 station 的 step_identity 统一为 'Station'。

        PathGraphBuilder 会把空 station 改为 'Station'，但 event_graph 仍是空。
        此处同步 normalize，确保 _step_match 能成功。
        """
        eg = dict(eg)  # shallow copy
        for key in ('commands', 'completions', 'waits', 'conditions',
                    'transitions', 'actions', 'steps'):
            items = eg.get(key, [])
            new_items = []
            for item in items:
                item = dict(item)
                si = item.get('step_identity', {})
                if si and not si.get('station', ''):
                    si = dict(si)
                    sv = si.get('state_variable', '')
                    if sv.startswith('m_n') or sv.startswith('Auto'):
                        si['station'] = 'Station'
                        item['step_identity'] = si
                new_items.append(item)
            eg[key] = new_items
        return eg

    def _normalize_slices_station(self, slices: list) -> list:
        """对 slices 中空 station 的 step_identity 统一为 'Station'。"""
        new_slices = []
        for s in slices:
            s = dict(s)
            si = s.get('step_identity', {})
            if si and not si.get('station', ''):
                si = dict(si)
                sv = si.get('state_variable', '')
                if sv.startswith('m_n') or sv.startswith('Auto'):
                    si['station'] = 'Station'
                    s['step_identity'] = si
            new_slices.append(s)
        return new_slices

    def run(self) -> bool:
        """主入口：加载已规划动作，应用结束标签策略，写出决策。"""
        # 重新运行 planner（用最新的 event_graph + slices + described）
        # vendor/lib 已在 sys.path 中（由 run.py 设置）
        from lib.action_planner import EngineeringActionPlanner, PathGraphBuilder

        print('  [EndTagSelector] 重新规划动作...')

        # 预处理：对 Mylar 等空 station 项目，统一 station='Station'
        # PathGraphBuilder 会把空 station 改为 'Station'，但 event_graph 仍是空，
        # 导致 _step_match 失败。这里同步 normalize。
        eg_normalized = self._normalize_station(self.event_graph)
        slices_normalized = self._normalize_slices_station(self.slices)

        planner = EngineeringActionPlanner(
            eg_normalized, self.steps_described, slices_normalized, self.config
        )
        actions = planner.plan()
        print(f'  [EndTagSelector] Planner 产出 {len(actions)} 个动作')

        # 应用结束标签策略
        decisions = []
        for a in actions:
            d = self._decide_one({
                'action_id': a.action_id,
                'step_identity': a.step_identity,
                'action_type': a.action_type,
                'commands': a.commands,
                'completions': a.completions,
                'start_label': a.start_label,
                'end_label': a.end_label,
                'end_label_source': a.end_label_source,
            })
            decisions.append(d)

        # 统计
        from collections import Counter
        intent_counts = Counter(d.timing_intent for d in decisions)
        review_counts = Counter(d.review_reason_code for d in decisions
                                if d.review_reason_code)
        print(f'  [EndTagSelector] timing_intent 分布: {dict(intent_counts)}')
        print(f'  [EndTagSelector] 待复核原因分布: {dict(review_counts)}')

        # 输出
        out_path = self.output_dir / 'end_tag_decisions.json'
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump({
                'actions': [
                    {**asdict(d),
                     'original_end_label': a.end_label,
                     'original_end_label_source': a.end_label_source,
                     'description': a.description,
                     'start_label': a.start_label,
                     'commands': a.commands,
                     'completions': a.completions,
                     'evidence_level': a.evidence_level,
                     'comment': a.comment,
                     'path_id': a.path_id,
                     'path_type': a.path_type,
                     'guard': a.guard,
                     'branch_info': a.branch_info,
                     'event_seq': a.event_seq}
                    for d, a in zip(decisions, actions)
                ],
                'stats': {
                    'total': len(decisions),
                    'timing_intent': dict(intent_counts),
                    'review_reasons': dict(review_counts),
                }
            }, f, ensure_ascii=False, indent=2)
        print(f'  [EndTagSelector] 输出: {out_path}')
        return True
