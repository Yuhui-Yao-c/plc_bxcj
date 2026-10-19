#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/event_exporter.py — 事件级 Excel 导出器

从 EventGraph 生成多行 Excel，每个 Step 的每个事件独立成行。

输出 Sheet:
  1. 事件采集表 — 主表，按工站/Step/事件序号排序
  2. 命令反馈账本 — 所有 CommandFact 与 CompletionEvent 匹配状态
  3. 分支审计 — 所有 TransitionEdge 的分支结构
  4. 事件统计 — 按 event_type/boundary_kind 的汇总

主表列:
  工站 | Step | 事件序号 | 事件类型 | 中文描述 |
  开始标签 | 结束标签 | 分支类型 | 并行组 |
  完成条件 | 证据状态 | 源变量
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


# ============================================================
# Chinese Description Generator
# ============================================================

class DescriptionGenerator:
    """从 EventGraph 事件 + action_semantic_facts 生成中文描述。

    原则:
      1. 优先使用 action_semantic_facts (已解析的语义)
      2. 回退到变量结构解析 (接口约定)
      3. 格式: 执行机构 + 操作 + 目标位置/对象
      4. 不出现变量名/地址/工站编号
    """

    # General operation mapping (based on PLC FB interface conventions)
    _OP_MAP = {
        'ToPos': '移动至位置',
        'Location_Auto': '自动定位至',
        'Location': '定位至',
        'ToHome': '回原点',
        'Jog': '点动',
        'Stop': '停止',
        'Reset': '复位',
        'Start': '启动',
    }

    _FEEDBACK_MAP = {
        'PosDone': '到位',
        'InPlace': '到位',
        'InPos': '到位',
        'Cmd_Busy': '执行中',
        'Cmd_Done': '执行完成',
        'Alarm': '报警',
        'Ready': '就绪',
    }

    def __init__(self, steps_described: list = None):
        self.facts_by_step = {}
        self.facts_by_var = {}
        if steps_described:
            self._index_facts(steps_described)

    def _index_facts(self, steps_described: list):
        for s in steps_described:
            si = s.get('step_identity', {})
            station = si.get('station', '')
            thread = si.get('thread_idx', -1)
            step = si.get('step_value', -1)
            key = (station, thread, step)

            facts = s.get('action_semantic_facts', [])
            self.facts_by_step[key] = facts
            for f in facts:
                raw = f.get('raw_action', '')
                if raw:
                    self.facts_by_var[(key, raw)] = f

    def describe_command(self, command: dict, step_identity: dict) -> str:
        """生成命令的中文描述。"""
        si = step_identity
        key = (si.get('station', ''), si.get('thread_idx', -1),
               si.get('step_value', -1))
        var = command.get('variable', '')

        # Try action_semantic_facts first
        fact = self.facts_by_var.get((key, var))
        if fact:
            actor = self._extract_value(fact, 'actor')
            operation = self._extract_value(fact, 'operation')
            pos_idx = self._extract_value(fact, 'position_index')
            target = self._extract_value(fact, 'target')

            parts = []
            if actor and actor != 'unknown':
                parts.append(actor)
            if operation and operation != 'unknown':
                parts.append(operation)
            if pos_idx and pos_idx != 'None':
                parts.append(str(pos_idx))
            elif target:
                parts.append(str(target))

            if parts:
                return ''.join(parts)

        # Fallback: parse from variable structure
        return self._describe_from_var(var, command)

    def describe_completion(self, completion: dict, step_identity: dict) -> str:
        """生成完成事件的中文描述。"""
        var = completion.get('variable', '')
        actuator = completion.get('actuator_path', '')
        fb_type = completion.get('feedback_type', '')

        # Extract actuator name from path
        actuator_name = self._extract_actuator_name(actuator)

        fb_desc = self._FEEDBACK_MAP.get(fb_type, fb_type)
        if actuator_name:
            return f'{actuator_name}{fb_desc}'
        return f'等待{fb_desc}'

    def describe_wait(self, wait: dict, step_identity: dict) -> str:
        """生成等待事件的中文描述。"""
        var = wait.get('variable', '')
        source_station = wait.get('source_station', '')

        # Extract meaningful part from variable
        parts = var.split('.')
        signal_name = parts[-1] if parts else var
        if '[' in signal_name:
            signal_name = signal_name.split('[')[0]

        if source_station:
            return f'等待{source_station}{signal_name}'
        return f'等待{signal_name}'

    def describe_condition(self, condition: dict, step_identity: dict) -> str:
        """生成条件的中文描述。"""
        expr = condition.get('expression', '')
        cond_type = condition.get('condition_type', '')

        if cond_type == 'comparison':
            # e.g., "Cell_Info[2,0].Quality EQ 0"
            return f'条件: {expr}'
        else:
            var = condition.get('variable', '')
            # Try to extract meaningful name
            parts = var.split('.')
            name = parts[-1] if parts else var
            return f'条件: {name}'

    def describe_transition(self, transition: dict, step_identity: dict) -> str:
        """生成转移的中文描述。"""
        target = transition.get('target_step', {})
        target_val = target.get('value', -1)
        branch_count = transition.get('branch_count', 0)

        if branch_count > 1:
            return f'步序转移至{target_val} (多分支:{branch_count}条)'
        return f'步序转移至{target_val}'

    def _describe_from_var(self, var: str, command: dict) -> str:
        """从变量结构生成描述 (回退方案)。"""
        cmd_type = command.get('command_type', '')
        idx = command.get('index', '')
        is_interface = command.get('is_interface_cmd', False)

        if is_interface and cmd_type:
            op = self._OP_MAP.get(cmd_type, cmd_type)
            actuator = self._extract_actuator_name(command.get('actuator_path', ''))
            if actuator and idx:
                return f'{actuator}{op}{idx}'
            elif actuator:
                return f'{actuator}{op}'
            elif idx:
                return f'{op}{idx}'
            return op

        # Non-interface variable
        if cmd_type:
            return cmd_type
        return var

    def _extract_actuator_name(self, path: str) -> str:
        """从路径提取执行机构名称 (最后一段)。"""
        if not path:
            return ''
        parts = path.split('.')
        return parts[-1] if parts else path

    def _extract_value(self, fact: dict, field_name: str) -> str:
        """从 action_semantic_fact 提取字段值。"""
        field = fact.get(field_name, {})
        if isinstance(field, dict):
            val = field.get('value', '')
            if val and val != 'unknown' and val != 'None':
                return str(val)
        return ''


# ============================================================
# Event Table Exporter
# ============================================================

# Evidence level colors
_HIGH_FILL = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')
_MED_FILL = PatternFill(start_color='FFEB9C', end_color='FFEB9C', fill_type='solid')
_LOW_FILL = PatternFill(start_color='FFC7CE', end_color='FFC7CE', fill_type='solid')
_HEADER_FILL = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
_HEADER_FONT = Font(color='FFFFFF', bold=True, size=11)
_THIN_BORDER = Border(
    left=Side(style='thin'), right=Side(style='thin'),
    top=Side(style='thin'), bottom=Side(style='thin'),
)

# Event type labels (Chinese)
_EVENT_TYPE_LABELS = {
    'command': '命令',
    'completion': '完成',
    'wait': '等待',
    'condition': '条件',
    'transition': '转移',
}

# Branch type labels
_BRANCH_LABELS = {
    'normal': '正常',
    'normal_with_sync': '正常(含同步)',
    'normal_with_condition': '正常(含条件)',
    'sync_only': '仅同步',
    'condition_only': '仅条件',
    'bypass': '旁路',
    'empty': '空',
}


def export_event_table(event_graph: dict,
                       output_path: str,
                       steps_described: list = None,
                       device_name: str = '',
                       ) -> tuple:
    """导出事件级 Excel。

    Returns:
        (total_rows, total_steps)
    """
    desc_gen = DescriptionGenerator(steps_described)

    wb = openpyxl.Workbook()

    # === Sheet 1: 事件采集表 (主表) ===
    ws1 = wb.active
    ws1.title = '事件采集表'
    _build_main_sheet(ws1, event_graph, desc_gen, device_name)

    # === Sheet 2: 命令反馈账本 ===
    ws2 = wb.create_sheet('命令反馈账本')
    _build_command_completion_sheet(ws2, event_graph)

    # === Sheet 3: 分支审计 ===
    ws3 = wb.create_sheet('分支审计')
    _build_branch_audit_sheet(ws3, event_graph)

    # === Sheet 4: 事件统计 ===
    ws4 = wb.create_sheet('事件统计')
    _build_stats_sheet(ws4, event_graph)

    wb.save(output_path)

    total_rows = len(event_graph.get('actions', []))
    total_steps = len(event_graph.get('steps', []))
    return total_rows, total_steps


def _build_main_sheet(ws, event_graph: dict, desc_gen: DescriptionGenerator,
                      device_name: str):
    """构建主表 — 事件采集表。"""
    actions = event_graph.get('actions', [])

    # Sort: station → thread → step → event_seq
    def sort_key(a):
        si = a.get('step_identity', {})
        return (
            si.get('station', ''),
            si.get('state_variable', ''),
            si.get('thread_idx', 0),
            si.get('step_value', 0),
            a.get('event_seq', 0),
        )
    actions_sorted = sorted(actions, key=sort_key)

    # Title
    title = f'{device_name} 工程动作事件采集表'
    title += f'  (共{len(actions_sorted)}个事件, {len(event_graph.get("steps",[]))}个Step)'
    ws.merge_cells('A1:L1')
    cell = ws.cell(1, 1, title)
    cell.font = Font(bold=True, size=14)
    cell.alignment = Alignment(horizontal='center')

    # Headers
    headers = [
        '工站', 'Step', '事件序号', '事件类型',
        '中文描述', '开始标签', '结束标签',
        '分支类型', '并行组', '完成条件',
        '证据状态', '源变量',
    ]
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(2, ci, h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal='center')
        cell.border = _THIN_BORDER

    # Data rows
    prev_step_key = None
    for ri, action in enumerate(actions_sorted, 3):
        si = action.get('step_identity', {})
        station = si.get('station', '')
        step_val = si.get('step_value', 0)
        thread = si.get('thread_idx', 0)
        event_type = action.get('event_type', '')
        event_seq = action.get('event_seq', 0)

        # Generate description
        desc = _generate_description(action, desc_gen)

        # Start/end labels
        start_label = action.get('timing_start', '')
        end_label = action.get('timing_end', '')

        # Branch type
        branch_id = action.get('branch_id', '')
        branch_type = ''
        if action.get('transition'):
            for b in action['transition'].get('branches', []):
                bt = b.get('branch_type', '')
                branch_type = _BRANCH_LABELS.get(bt, bt)
                break
            if not branch_type and action['transition'].get('branch_count', 0) > 1:
                branch_type = '多分支'

        # Completion condition
        completion_cond = ''
        if action.get('completion'):
            completion_cond = action['completion'].get('variable', '')

        # Evidence level
        ev_level = action.get('evidence_level', '')

        # Source variable
        src_var = ''
        if action.get('command'):
            src_var = action['command'].get('variable', '')
        elif action.get('wait'):
            src_var = action['wait'].get('variable', '')
        elif action.get('condition'):
            src_var = action['condition'].get('variable', '')
        elif action.get('transition'):
            src_var = action['transition'].get('element_id', '')

        values = [
            station, step_val, event_seq,
            _EVENT_TYPE_LABELS.get(event_type, event_type),
            desc, start_label, end_label,
            branch_type, action.get('parallel_group', 0),
            completion_cond, ev_level, src_var,
        ]

        for ci, val in enumerate(values, 1):
            cell = ws.cell(ri, ci, val)
            cell.border = _THIN_BORDER
            cell.alignment = Alignment(
                horizontal='center' if ci <= 4 else 'left',
                vertical='center',
                wrap_text=ci >= 5,
            )
            # Color evidence level
            if ci == 11:
                if ev_level == 'source_explicit':
                    cell.fill = _HIGH_FILL
                elif ev_level == 'control_flow_verified':
                    cell.fill = _MED_FILL
                elif ev_level == 'inferred':
                    cell.fill = _LOW_FILL

    # Column widths
    col_widths = [8, 8, 8, 8, 40, 35, 40, 14, 8, 35, 18, 40]
    for ci, w in enumerate(col_widths, 1):
        ws.column_dimensions[chr(64 + ci) if ci <= 26 else 'L'].width = w
    ws.freeze_panes = 'A3'


def _generate_description(action: dict, desc_gen: DescriptionGenerator) -> str:
    """为 EngineeringAction 生成中文描述。"""
    if action.get('description'):
        return action['description']

    si = action.get('step_identity', {})
    event_type = action.get('event_type', '')

    if event_type == 'command':
        cmd = action.get('command')
        if cmd:
            return desc_gen.describe_command(cmd, si)
    elif event_type == 'completion':
        comp = action.get('completion')
        if comp:
            return desc_gen.describe_completion(comp, si)
    elif event_type == 'wait':
        wait = action.get('wait')
        if wait:
            return desc_gen.describe_wait(wait, si)
    elif event_type == 'condition':
        cond = action.get('condition')
        if cond:
            return desc_gen.describe_condition(cond, si)
    elif event_type == 'transition':
        trans = action.get('transition')
        if trans:
            return desc_gen.describe_transition(trans, si)

    return '(未描述)'


def _build_command_completion_sheet(ws, event_graph: dict):
    """构建命令-反馈匹配账本。"""
    commands = event_graph.get('commands', [])
    completions = event_graph.get('completions', [])

    # Build completion lookup by matched_command_id
    comp_by_cmd = defaultdict(list)
    for comp in completions:
        if comp.get('matched_command_id'):
            comp_by_cmd[comp['matched_command_id']].append(comp)

    headers = [
        '工站', 'Thread', 'Step', '命令变量', '执行机构',
        '命令类型', '索引', '匹配反馈', '匹配状态', '反馈来源',
    ]
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(1, ci, h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal='center')
        cell.border = _THIN_BORDER

    ri = 2
    for cmd in commands:
        si = cmd.get('step_identity', {})
        matched = comp_by_cmd.get(cmd.get('event_id', ''), [])

        if matched:
            for comp in matched:
                values = [
                    si.get('station', ''), si.get('thread_idx', ''),
                    si.get('step_value', ''),
                    cmd.get('variable', ''), cmd.get('actuator_path', ''),
                    cmd.get('command_type', ''), cmd.get('index', ''),
                    comp.get('variable', ''),
                    comp.get('match_confidence', ''),
                    comp.get('source', ''),
                ]
                for ci, val in enumerate(values, 1):
                    cell = ws.cell(ri, ci, val)
                    cell.border = _THIN_BORDER
                    cell.alignment = Alignment(horizontal='left', wrap_text=True)
                ri += 1
        else:
            values = [
                si.get('station', ''), si.get('thread_idx', ''),
                si.get('step_value', ''),
                cmd.get('variable', ''), cmd.get('actuator_path', ''),
                cmd.get('command_type', ''), cmd.get('index', ''),
                '(未匹配)', 'unmatched', '',
            ]
            for ci, val in enumerate(values, 1):
                cell = ws.cell(ri, ci, val)
                cell.border = _THIN_BORDER
                cell.alignment = Alignment(horizontal='left', wrap_text=True)
                if ci == 9:
                    cell.fill = _LOW_FILL
            ri += 1

    col_widths = [8, 8, 8, 40, 25, 18, 12, 40, 15, 18]
    for ci, w in enumerate(col_widths, 1):
        ws.column_dimensions[chr(64 + ci)].width = w
    ws.freeze_panes = 'A2'


def _build_branch_audit_sheet(ws, event_graph: dict):
    """构建分支审计表。"""
    transitions = event_graph.get('transitions', [])

    headers = [
        '工站', 'Thread', 'Step', '目标Step', '分支数',
        '分支ID', '分支类型', '完成事件数', '等待事件数', '条件数',
    ]
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(1, ci, h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal='center')
        cell.border = _THIN_BORDER

    ri = 2
    for trans in transitions:
        si = trans.get('step_identity', {})
        target = trans.get('target_step', {})
        branches = trans.get('branches', [])

        for b in branches:
            bt = b.get('branch_type', '')
            values = [
                si.get('station', ''), si.get('thread_idx', ''),
                si.get('step_value', ''),
                target.get('value', ''),
                trans.get('branch_count', 0),
                b.get('branch_id', ''),
                _BRANCH_LABELS.get(bt, bt),
                len(b.get('completions', [])),
                len(b.get('waits', [])),
                len(b.get('conditions', [])),
            ]
            for ci, val in enumerate(values, 1):
                cell = ws.cell(ri, ci, val)
                cell.border = _THIN_BORDER
                cell.alignment = Alignment(horizontal='center')
            ri += 1

    col_widths = [8, 8, 8, 10, 8, 8, 16, 10, 10, 8]
    for ci, w in enumerate(col_widths, 1):
        ws.column_dimensions[chr(64 + ci)].width = w
    ws.freeze_panes = 'A2'


def _build_stats_sheet(ws, event_graph: dict):
    """构建统计表。"""
    from collections import Counter

    actions = event_graph.get('actions', [])
    commands = event_graph.get('commands', [])
    completions = event_graph.get('completions', [])
    waits = event_graph.get('waits', [])
    conditions = event_graph.get('conditions', [])
    transitions = event_graph.get('transitions', [])
    steps = event_graph.get('steps', [])

    # Event type distribution
    type_counts = Counter(a.get('event_type', '') for a in actions)

    # Match statistics
    matched = sum(1 for c in completions if c.get('matched_command_id'))
    unmatched = len(completions) - matched

    # Multi-action steps
    cmd_by_step = defaultdict(int)
    for a in actions:
        if a.get('event_type') == 'command':
            si = a.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('step_value', -1))
            cmd_by_step[key] += 1
    multi_cmd = sum(1 for v in cmd_by_step.values() if v > 1)

    # Multi-branch transitions
    multi_branch = sum(1 for t in transitions if t.get('branch_count', 0) > 1)

    rows = [
        ('统计项', '数量'),
        ('Steps (总步数)', len(steps)),
        ('EngineeringActions (总事件)', len(actions)),
        ('Commands (命令)', len(commands)),
        ('Completions (完成反馈)', len(completions)),
        ('Waits (等待)', len(waits)),
        ('Conditions (条件)', len(conditions)),
        ('Transitions (转移)', len(transitions)),
        ('', ''),
        ('事件类型分布', ''),
    ]
    for et, cnt in sorted(type_counts.items()):
        rows.append((f'  {et}', cnt))

    rows.extend([
        ('', ''),
        ('命令-反馈匹配', ''),
        ('  已匹配', matched),
        ('  未匹配', unmatched),
        ('  匹配率', f'{100*matched/max(len(completions),1):.1f}%'),
        ('', ''),
        ('多命令步骤', f'{multi_cmd}/{len(cmd_by_step)}'),
        ('多分支转移', f'{multi_branch}/{len(transitions)}'),
    ])

    for ri, (label, val) in enumerate(rows, 1):
        cell_l = ws.cell(ri, 1, label)
        cell_v = ws.cell(ri, 2, val)
        if ri == 1:
            cell_l.fill = _HEADER_FILL
            cell_v.fill = _HEADER_FILL
            cell_l.font = _HEADER_FONT
            cell_v.font = _HEADER_FONT
        cell_l.border = _THIN_BORDER
        cell_v.border = _THIN_BORDER

    ws.column_dimensions['A'].width = 30
    ws.column_dimensions['B'].width = 20


# ============================================================
# CLI helper
# ============================================================

def generate_event_excel(event_graph_path: str,
                         steps_described_path: str,
                         output_path: str,
                         device_name: str = '',
                         ) -> tuple:
    """从 JSON 文件生成事件级 Excel。"""
    with open(event_graph_path, 'r', encoding='utf-8') as f:
        event_graph = json.load(f)

    steps_described = None
    if steps_described_path:
        try:
            with open(steps_described_path, 'r', encoding='utf-8') as f:
                steps_described = json.load(f)
        except FileNotFoundError:
            pass

    return export_event_table(event_graph, output_path, steps_described,
                              device_name)
