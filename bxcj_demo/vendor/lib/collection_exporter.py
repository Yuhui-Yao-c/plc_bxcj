#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/collection_exporter.py — 工程动作采集表导出器

输出4个Sheet:
  Sheet1: 工程动作采集表 (主表，按路径顺序排列)
  Sheet2: 状态机路径图 (Step序列、分支、汇合)
  Sheet3: 工程动作证据 (命令、反馈、匹配方式)
  Sheet4: 待核验与差异 (未解决的多候选)
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


# Style constants
_HEADER_FILL = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
_HEADER_FONT = Font(color='FFFFFF', bold=True, size=11)
_GREEN_FILL = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')
_YELLOW_FILL = PatternFill(start_color='FFEB9C', end_color='FFEB9C', fill_type='solid')
_RED_FILL = PatternFill(start_color='FFC7CE', end_color='FFC7CE', fill_type='solid')
_THIN_BORDER = Border(
    left=Side(style='thin'), right=Side(style='thin'),
    top=Side(style='thin'), bottom=Side(style='thin'),
)

_PATH_TYPE_LABELS = {
    'main': '主路径',
    'branch': '分支',
    'reset': '复位',
    'termination': '终止',
}

_ACTION_TYPE_LABELS = {
    'motion': '运动',
    'wait': '等待',
    'clamp': '夹紧',
    'vacuum': '真空',
    'transition': '转移',
    'action': '动作',
    'unknown': '未知',
}

_END_SOURCE_LABELS = {
    'completion_direct': '完成信号(同步)',
    'completion_cross_step': '完成信号(跨步)',
    'transition': '状态转移',
    'step_exit': '步序退出',
    'wait_signal': '等待信号',
}


def export_collection_table(actions: list, paths: list, event_graph: dict,
                            output_path: str, device_name: str = '',
                            steps_described: list = None) -> tuple:
    """导出工程动作采集表 Excel。

    Returns:
        (total_rows, total_steps)
    """
    wb = openpyxl.Workbook()

    # Sheet 1: 工程动作采集表
    ws1 = wb.active
    ws1.title = '工程动作采集表'
    _build_main_sheet(ws1, actions, device_name)

    # Sheet 2: 状态机路径图
    ws2 = wb.create_sheet('状态机路径图')
    _build_path_sheet(ws2, paths)

    # Sheet 3: 工程动作证据
    ws3 = wb.create_sheet('工程动作证据')
    _build_evidence_sheet(ws3, actions, event_graph)

    # Sheet 4: 待核验
    ws4 = wb.create_sheet('待核验')
    _build_review_sheet(ws4, actions, event_graph)

    wb.save(output_path)

    total_steps = len(set(
        (a.step_identity.get('station', ''), a.step_identity.get('thread_idx', -1),
         a.step_identity.get('step_value', -1))
        for a in actions
    ))
    return len(actions), total_steps


def _build_main_sheet(ws, actions: list, device_name: str):
    """构建主表 — 工程动作采集表。"""
    # Title
    title = '%s 工程动作时序采集表' % device_name if device_name else '工程动作时序采集表'
    title += '  (共%d项动作)' % len(actions)
    ws.merge_cells('A1:K1')
    cell = ws.cell(1, 1, title)
    cell.font = Font(bold=True, size=14)
    cell.alignment = Alignment(horizontal='center')

    # Headers
    headers = [
        '路径', '路径类型', '工站', 'Thread', 'Step',
        '动作类型', '中文动作描述',
        '开始标签', '结束标签',
        '结束标签来源', '程序注释',
    ]
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(2, ci, h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal='center')
        cell.border = _THIN_BORDER

    # Sort actions by station → thread → step → event_seq
    # (like the human table: grouped by station, ordered by process sequence)
    def sort_key(a):
        si = a.step_identity
        station = si.get('station', '')
        # Extract numeric part from station (EM01 -> 1, EM10 -> 10)
        import re as _re
        m = _re.search(r'(\d+)', station)
        station_num = int(m.group(1)) if m else 999
        return (
            station_num,
            si.get('thread_idx', 0),
            si.get('step_value', 0),
            a.event_seq,
        )
    actions_sorted = sorted(actions, key=sort_key)

    for ri, action in enumerate(actions_sorted, 3):
        si = action.step_identity
        values = [
            action.path_id,
            _PATH_TYPE_LABELS.get(action.path_type, action.path_type),
            si.get('station', ''),
            si.get('thread_idx', 0),
            si.get('step_value', 0),
            _ACTION_TYPE_LABELS.get(action.action_type, action.action_type),
            action.description,
            action.start_label,
            action.end_label,
            _END_SOURCE_LABELS.get(action.end_label_source, action.end_label_source),
            action.comment,
        ]

        for ci, val in enumerate(values, 1):
            cell = ws.cell(ri, ci, val)
            cell.border = _THIN_BORDER
            cell.alignment = Alignment(
                horizontal='center' if ci <= 6 else 'left',
                vertical='center',
                wrap_text=ci >= 7,
            )
            # Color by evidence level
            if action.evidence_level == 'source_explicit':
                if ci == 9:
                    cell.fill = _GREEN_FILL
            elif action.evidence_level == 'inferred':
                if ci == 9:
                    cell.fill = _YELLOW_FILL

        # Add branch info as note
        if action.branch_info:
            cell = ws.cell(ri, 7)
            cell.comment = openpyxl.comments.Comment(action.branch_info, 'system')

    # Column widths
    widths = [10, 8, 8, 8, 8, 8, 40, 35, 40, 14, 30]
    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + ci)].width = w
    ws.freeze_panes = 'A3'


def _build_path_sheet(ws, paths: list):
    """构建路径图Sheet。"""
    headers = [
        '路径ID', '路径类型', '工站', 'Thread', 'Step序列',
        '分叉来源', '分叉Step', '路径Guard',
    ]
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(1, ci, h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal='center')
        cell.border = _THIN_BORDER

    ri = 2
    for path in paths:
        if not path.steps:
            continue
        first_step = path.steps[0]
        station = first_step.get('station', '')
        thread = first_step.get('thread_idx', -1)
        step_seq = ' -> '.join(str(s.get('step_value', -1)) for s in path.steps)

        branch_step_str = ''
        if path.branch_step:
            branch_step_str = str(path.branch_step.get('step_value', ''))

        values = [
            path.path_id,
            _PATH_TYPE_LABELS.get(path.path_type, path.path_type),
            station, thread, step_seq,
            path.branch_from, branch_step_str, path.guard,
        ]
        for ci, val in enumerate(values, 1):
            cell = ws.cell(ri, ci, val)
            cell.border = _THIN_BORDER
            cell.alignment = Alignment(horizontal='left', wrap_text=True)
        ri += 1

    widths = [12, 8, 8, 8, 60, 12, 10, 30]
    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + ci)].width = w
    ws.freeze_panes = 'A2'


def _build_evidence_sheet(ws, actions: list, event_graph: dict):
    """构建证据Sheet。"""
    headers = [
        '动作ID', '路径', '工站', 'Step', '动作类型',
        '命令变量', '执行机构', '完成反馈', '匹配方式',
        '证据状态', '描述',
    ]
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(1, ci, h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal='center')
        cell.border = _THIN_BORDER

    ri = 2
    for action in actions:
        si = action.step_identity
        cmd_vars = ' | '.join(action.commands) if action.commands else '(无命令)'
        comp_vars = ' | '.join(action.completions) if action.completions else '(无匹配反馈)'

        # Extract actuator from first command
        actuator = ''
        if action.commands:
            # Parse from first command variable
            var = action.commands[0]
            parts = var.split('.In.')
            if len(parts) > 1:
                actuator = parts[0]
            else:
                parts = var.split('.Out.')
                if len(parts) > 1:
                    actuator = parts[0]

        values = [
            action.action_id, action.path_id,
            si.get('station', ''), si.get('step_value', 0),
            _ACTION_TYPE_LABELS.get(action.action_type, action.action_type),
            cmd_vars, actuator, comp_vars,
            _END_SOURCE_LABELS.get(action.end_label_source, ''),
            action.evidence_level, action.description,
        ]
        for ci, val in enumerate(values, 1):
            cell = ws.cell(ri, ci, val)
            cell.border = _THIN_BORDER
            cell.alignment = Alignment(horizontal='left', wrap_text=True)
        ri += 1

    widths = [10, 8, 8, 8, 8, 50, 25, 40, 14, 14, 35]
    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + ci)].width = w
    ws.freeze_panes = 'A2'


def _build_review_sheet(ws, actions: list, event_graph: dict):
    """构建待核验Sheet。"""
    headers = ['统计项', '数量', '说明']
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(1, ci, h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal='center')
        cell.border = _THIN_BORDER

    # Statistics
    type_counts = Counter(a.action_type for a in actions)
    source_counts = Counter(a.end_label_source for a in actions)
    ev_counts = Counter(a.evidence_level for a in actions)

    # Unmatched completions
    total_completions = len(event_graph.get('completions', []))
    matched = sum(1 for a in actions if a.completions)

    # Steps with no actions
    all_steps = set()
    for a in actions:
        si = a.step_identity
        all_steps.add((si.get('station', ''), si.get('thread_idx', -1),
                       si.get('step_value', -1)))

    rows = [
        ('统计项', '数量', '说明'),
        ('总动作数', len(actions), ''),
        ('覆盖步数', len(all_steps), ''),
        ('平均动作/步', '%.1f' % (len(actions) / max(len(all_steps), 1)), ''),
        ('', '', ''),
        ('动作类型分布', '', ''),
    ]
    for t, c in sorted(type_counts.items()):
        rows.append(('  %s' % _ACTION_TYPE_LABELS.get(t, t), c, ''))

    rows.append(('', '', ''))
    rows.append(('结束标签来源', '', ''))
    for s, c in sorted(source_counts.items()):
        rows.append(('  %s' % _END_SOURCE_LABELS.get(s, s), c, ''))

    rows.append(('', '', ''))
    rows.append(('证据状态', '', ''))
    for e, c in sorted(ev_counts.items()):
        rows.append(('  %s' % e, c, ''))

    rows.append(('', '', ''))
    rows.append(('命令-反馈匹配', '', ''))
    rows.append(('  有匹配反馈的动作', matched, ''))
    rows.append(('  无匹配反馈的动作', len(actions) - matched, '使用状态转移作为结束标签'))
    rows.append(('  EventGraph总完成事件', total_completions, ''))

    for ri, (label, val, note) in enumerate(rows, 1):
        ws.cell(ri, 1, label)
        ws.cell(ri, 2, val)
        ws.cell(ri, 3, note)
        if ri == 1:
            for ci in range(1, 4):
                ws.cell(ri, ci).fill = _HEADER_FILL
                ws.cell(ri, ci).font = _HEADER_FONT

    ws.column_dimensions['A'].width = 25
    ws.column_dimensions['B'].width = 12
    ws.column_dimensions['C'].width = 35
