#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dlib/smc2_fb_recognizer.py — SMC2 FB 调用识别器（v10.39 真实数据版本）

基于真实 Mylar SMC2 解码输出的实际命名模式重写：
  - Axis_Group_{module}.In.ToPos[N] / GroupEnable_Execute / GroupDisable_Execute — 轴组命令
  - Axis_Group_{module}.Out.MoveLinearDone[N] / MoveLinear_Error — 轴组完成反馈（确认）
  - Cylinder[N].HMIAnastole / AutoAnastole — 气缸命令
  - Cylinder[N].AnastoleSafety / StrOutSafety — 气缸安全反馈（候选）
  - AxisValP[N].m_nSaveMotorLoc — 轴位置保存
  - Axis{NNN}Program.* — 轴程序变量

区分三种反馈置信度：
  - confirmed: MoveLinearDone（明确的运动完成信号）
  - candidate: AnastoleSafety / StrOutSafety / MoveLinear_Error（FB输出但完成语义待核验）
  - rejected: 后补变量（abEM##ActionStart/Done）

不修改原始 step_logic_slices.json，仅输出补充事件供 EventGraph 合并。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from collections import defaultdict


# 真实 SMC2 命名模式（基于 Mylar L5A0411_2 解码输出验证）
COMMAND_PATTERNS = [
    # 轴组命令（高置信度）
    (re.compile(r'^(Axis_Group_[\w]+)\.In\.(ToPos\[\d+\])$'), 'axis_group_move', 'confirmed'),
    (re.compile(r'^(Axis_Group_[\w]+)\.In\.(GroupEnable_Execute)$'), 'axis_group_enable', 'confirmed'),
    (re.compile(r'^(Axis_Group_[\w]+)\.In\.(GroupDisable_Execute)$'), 'axis_group_disable', 'confirmed'),
    (re.compile(r'^(HMI_Axis_Group_[\w]+)\.H_In\.(ToPos\[\d+\])$'), 'hmi_axis_group_move', 'confirmed'),
    # 气缸命令
    (re.compile(r'^(Cylinder\[\d+\])\.(HMIAnastole|AutoAnastole)$'), 'cylinder_move', 'confirmed'),
    (re.compile(r'^(Cylinder\[\d+\])\.(HMIStrOut|AutoStrOut)$'), 'cylinder_extend', 'confirmed'),
    # 轴程序
    (re.compile(r'^(Axis\d+Program)\.(Stop|Start|Home|JogCW|JogCCW)$'), 'axis_program_cmd', 'confirmed'),
    # 轴位置保存（命令证据，但完成需另寻）
    (re.compile(r'^(AxisValP\[\d+\])\.(m_nSaveMotorLoc)$'), 'axis_position_save', 'confirmed'),
]

COMPLETION_PATTERNS = [
    # 确认完成反馈（高置信度）
    (re.compile(r'^(Axis_Group_[\w]+)\.Out\.(MoveLinearDone\[\d+\])$'), 'move_linear_done', 'confirmed'),
    (re.compile(r'^(HMI_Axis_Group_[\w]+)\.H_Out\.(MoveLinearDone\[\d+\])$'), 'hmi_move_linear_done', 'confirmed'),
    # 候选完成反馈（FB输出，完成语义待核验）
    (re.compile(r'^(Axis_Group_[\w]+)\.Out\.(MoveLinear_Error)$'), 'move_linear_error', 'candidate'),
    (re.compile(r'^(Cylinder\[\d+\])\.(AnastoleSafety|StrOutSafety)$'), 'cylinder_safety', 'candidate'),
    (re.compile(r'^(Axis\d+Program)\.(bConfirm\[\d+\]|State\d+|Finish\d+)$'), 'axis_program_done', 'candidate'),
    # 通用完成模式
    (re.compile(r'^([\w]+)\.(Finish\d+)$'), 'finish_signal', 'candidate'),
    (re.compile(r'^([\w]+)\.(bConfirm\[\d+\])$'), 'confirm_signal', 'candidate'),
]

BACKFILL_PATTERN = re.compile(
    r'(ab|at|artg|li)EM\d+(_\d+)?Action(Start|Done)'
)


@dataclass
class FBCallEvidence:
    """识别到的 FB 调用证据。"""
    fb_type: str
    instance_name: str
    parameter: str
    confidence: str = "confirmed"
    direction: str = ""
    element_id: str = ""
    unit_name: str = ""
    network_id: str = ""
    step_identity: dict = field(default_factory=dict)
    provenance: str = "smc2_fb_recognizer_v2"
    variable: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SMC2FBRecognizer:
    """从 event_graph.json + evidence_store.json 识别 SMC2 FB 调用。

    v10.39 修复：基于真实 SMC2 命名模式（Axis_Group_*, Cylinder[N], AxisValP[N]）
    而非假设的 AxisMove._bStart 模式。

    算法：
      1. 扫描 event_graph.commands 的 variable 字段
      2. 用 COMMAND_PATTERNS 匹配轴组/气缸/轴程序命令
      3. 扫描 evidence_store.entities 查找 MoveLinearDone 等完成信号
      4. 用 COMPLETION_PATTERNS 匹配完成反馈，标注 confidence
      5. 按 actuator_path 匹配 command-completion 对
      6. 后补变量（abEM##ActionStart）一律拒绝
    """

    def __init__(self):
        self.fb_calls = []

    def recognize(self, step_logic_slices: list, event_graph: dict = None,
                  evidence_store: dict = None) -> dict:
        """识别 FB 调用并生成补充 commands/completions。

        Args:
            step_logic_slices: 来自 build_step_graph 的 slices（v2 不再依赖其 variable 字段）
            event_graph: 来自 build_event_graph 的 event_graph（含 commands/completions）
            evidence_store: 来自 build_evidence 的 evidence_store（含 entities）

        Returns:
            {'commands': [...], 'completions': [...], 'fb_evidence': [...]}
        """
        event_graph = event_graph or {}
        evidence_store = evidence_store or {}

        existing_cmds = event_graph.get('commands', [])
        existing_comps = event_graph.get('completions', [])
        existing_cmd_vars = set(c.get('variable', '') for c in existing_cmds)
        existing_comp_vars = set(c.get('variable', '') for c in existing_comps)

        entities = evidence_store.get('entities', {}) if isinstance(evidence_store, dict) else {}

        new_commands = []
        new_completions = []
        fb_evidence = []

        # 1. 从 event_graph.commands 中识别已存在的 FB 命令（标注但不再生成）
        for cmd in existing_cmds:
            var = cmd.get('variable', '') or ''
            if BACKFILL_PATTERN.search(var):
                continue
            for pattern, fb_type, confidence in COMMAND_PATTERNS:
                m = pattern.match(var)
                if m:
                    instance_name = m.group(1)
                    parameter = m.group(2) if m.lastindex >= 2 else ''
                    ev = FBCallEvidence(
                        fb_type=fb_type,
                        instance_name=instance_name,
                        parameter=parameter,
                        confidence=confidence,
                        direction='Out',
                        element_id=cmd.get('element_id', ''),
                        unit_name=cmd.get('unit_name', ''),
                        step_identity=cmd.get('step_identity', {}),
                        variable=var,
                    )
                    fb_evidence.append(ev)
                    break

        # 2. 从 evidence_store.entities 查找完成反馈（MoveLinearDone 等）
        for eid, entity in entities.items():
            if not isinstance(entity, dict):
                continue
            key = entity.get('key', '') or entity.get('display_name', '') or ''
            if not key or BACKFILL_PATTERN.search(key):
                continue

            for pattern, fb_type, confidence in COMPLETION_PATTERNS:
                m = pattern.match(key)
                if m:
                    instance_name = m.group(1)
                    parameter = m.group(2) if m.lastindex >= 2 else ''

                    # 跳过已存在的 completion
                    if key in existing_comp_vars:
                        continue

                    ev = FBCallEvidence(
                        fb_type=fb_type,
                        instance_name=instance_name,
                        parameter=parameter,
                        confidence=confidence,
                        direction='In',
                        element_id=eid,
                        unit_name='',
                        step_identity={},
                        variable=key,
                    )
                    fb_evidence.append(ev)

                    # 仅 confirmed 级别生成 completion 事件
                    if confidence == 'confirmed':
                        new_comp = {
                            'event_id': f"fb_comp_{eid}",
                            'step_identity': {},
                            'variable': key,
                            'actuator_path': instance_name,
                            'feedback_type': fb_type,
                            'index': 0,
                            'element_id': eid,
                            'matched_command_id': '',
                            'match_confidence': f'smc2_{confidence}',
                            'source': 'smc2_fb_recognizer_v2',
                            'evidence_path': [],
                            'provenance': 'smc2_fb_recognizer_v2',
                        }
                        new_completions.append(new_comp)
                    break

        # 3. 从 event_graph.commands 中也查找完成反馈模式（已存在但未分类的）
        for cmd in existing_cmds:
            var = cmd.get('variable', '') or ''
            if BACKFILL_PATTERN.search(var):
                continue
            if var in existing_comp_vars:
                continue
            for pattern, fb_type, confidence in COMPLETION_PATTERNS:
                m = pattern.match(var)
                if m:
                    instance_name = m.group(1)
                    parameter = m.group(2) if m.lastindex >= 2 else ''
                    ev = FBCallEvidence(
                        fb_type=fb_type,
                        instance_name=instance_name,
                        parameter=parameter,
                        confidence=confidence,
                        direction='In',
                        element_id=cmd.get('element_id', ''),
                        unit_name=cmd.get('unit_name', ''),
                        step_identity=cmd.get('step_identity', {}),
                        variable=var,
                    )
                    fb_evidence.append(ev)

                    if confidence == 'confirmed' and var not in existing_comp_vars:
                        new_comp = {
                            'event_id': f"fb_comp_reclass_{cmd.get('event_id', '')}",
                            'step_identity': cmd.get('step_identity', {}),
                            'variable': var,
                            'actuator_path': cmd.get('actuator_path', instance_name),
                            'feedback_type': fb_type,
                            'index': cmd.get('index', 0),
                            'element_id': cmd.get('element_id', ''),
                            'matched_command_id': '',
                            'match_confidence': f'smc2_{confidence}',
                            'source': 'smc2_fb_recognizer_v2',
                            'evidence_path': cmd.get('evidence_path', []),
                            'provenance': 'smc2_fb_recognizer_v2',
                        }
                        new_completions.append(new_comp)
                    break

        # 4. 匹配 command-completion（按 actuator_path）
        new_completions = self._matchCommandCompletion(existing_cmds, new_completions)

        return {
            'commands': [],
            'completions': new_completions,
            'fb_evidence': [ev.to_dict() for ev in fb_evidence],
            'stats': {
                'total_fb_evidence': len(fb_evidence),
                'confirmed_completions': sum(1 for ev in fb_evidence if ev.confidence == 'confirmed'),
                'candidate_completions': sum(1 for ev in fb_evidence if ev.confidence == 'candidate'),
                'new_completions_generated': len(new_completions),
                'existing_commands_analyzed': len(existing_cmds),
                'existing_completions_analyzed': len(existing_comps),
                'entities_scanned': len(entities),
            }
        }

    def _matchCommandCompletion(self, commands: list, completions: list) -> list:
        """按 actuator_path 匹配 command-completion。"""
        comp_by_path = defaultdict(list)
        for c in completions:
            comp_by_path[c.get('actuator_path', '')].append(c)

        for cmd in commands:
            path = cmd.get('actuator_path', '')
            path_comps = comp_by_path.get(path, [])
            if path_comps:
                comp = path_comps.pop(0)
                comp['matched_command_id'] = cmd.get('event_id', '')
                comp['step_identity'] = cmd.get('step_identity', {})

        return list(completions)


def supplementEventGraph(event_graph: dict, supplemented: dict) -> dict:
    """将 SMC2FBRecognizer 的补充事件合并到 event_graph。"""
    eg = dict(event_graph)
    eg['commands'] = list(eg.get('commands', [])) + supplemented.get('commands', [])
    eg['completions'] = list(eg.get('completions', [])) + supplemented.get('completions', [])
    eg['_smc2_fb_evidence'] = supplemented.get('fb_evidence', [])
    eg['_smc2_fb_stats'] = supplemented.get('stats', {})
    return eg
