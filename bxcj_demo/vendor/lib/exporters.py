#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib/exporters.py — 通用步序表导出器（被 opencode 调用的工具库）

合并自 plc_zjph/exporters/universal_table.py 的精简版。

功能:
  - build_universal_table   : 构建步序表数据（开始/结束标签 + 动作描述）
  - export_universal_table  : 导出 Excel，trace 出现的行填绿色高亮
  - generate_universal_table: 一键生成（读 step_logic.json + trace.csv → xlsx）

输出三列核心：开始标签 | 结束标签 | 动作描述
  开始标签: {step_var_name}[{thread}]=INT#{step}
  结束标签: NOT ({step_var_name}[{thread}]=INT#{step})

step_var_name 可灵活适配不同工序的步序变量名，如 m_nNextAutoStep /
Process_steps / StepNo 等，标签格式保持一致。

由 tools/export_table.py 调用，也可被 opencode 直接 import 使用。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


_STEP_COL_RE = re.compile(
    r'(?:m_nNextAutoStep|Process_steps|StepNo|step_no)\[(\d+)\]',
    re.IGNORECASE,
)


def parse_trace(trace_path: str | None) -> set[tuple[int, int]]:
    if not trace_path or not os.path.exists(trace_path):
        return set()
    with open(trace_path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith('Index,'):
            header_idx = i
            break
    if header_idx is None:
        return set()
    header = lines[header_idx].strip().split(',')
    step_col_map: dict[int, int] = {}
    for ci, col_name in enumerate(header):
        m = _STEP_COL_RE.search(col_name)
        if m:
            step_col_map[ci] = int(m.group(1))
    if not step_col_map:
        return set()
    appeared: set[tuple[int, int]] = set()
    for line in lines[header_idx + 1:]:
        line = line.strip()
        if not line:
            continue
        cols = line.split(',')
        for ci, thread_num in step_col_map.items():
            if ci < len(cols):
                val = cols[ci].strip()
                if val:
                    try:
                        appeared.add((thread_num, int(val)))
                    except ValueError:
                        pass
    return appeared


def _clean_comment(comment: str) -> str:
    if not comment or not comment.strip():
        return ''
    text = ' / '.join(l.strip() for l in comment.split('\n') if l.strip())
    text = re.sub(r'^步?\d+[-.\s：:]*', '', text)
    return text.strip()


def _build_action_desc(comment: str,
                       actions: list,
                       action_fact: Any = None) -> str:
    if action_fact is not None:
        if hasattr(action_fact, 'summary'):
            desc = action_fact.summary()
            if desc and '未解析' not in desc:
                return desc
        elif isinstance(action_fact, dict):
            desc = action_fact.get('description', '')
            if desc:
                return desc
    cleaned = _clean_comment(comment)
    if cleaned:
        return cleaned
    if not actions:
        return '(无动作描述)'
    if isinstance(actions[0], dict):
        parts = []
        for a in actions[:5]:
            text = (a.get('resolved_text', '')
                    or a.get('hmi_text', '')
                    or a.get('dut_action_word', ''))
            if text:
                parts.append(text)
        if parts:
            return '; '.join(parts)
        parts = [a.get('actuator_expr', '') for a in actions[:5] if a.get('actuator_expr')]
        return '; '.join(parts) if parts else '(无动作描述)'
    return '; '.join(str(a) for a in actions[:5])


def build_universal_table(step_logic: list[dict],
                          step_var_name: str,
                          trace_appeared: set[tuple[int, int]] | None = None,
                          action_facts: dict[tuple[int, int], Any] | None = None,
                          timing_boundaries: list[dict] | None = None,
                          ) -> list[dict]:
    """Build step table rows with timing boundaries from Resolver.

    Args:
        timing_boundaries: list of TimingBoundary.to_dict() from Resolver.
            If provided, end_label comes from boundary.end_expression
            with source_kind, boundary_semantics, evidence_level.
            If None, falls back to NOT(step).
    """
    # Build boundary lookup from timing_boundaries
    boundary_map = {}
    if timing_boundaries:
        for tb in timing_boundaries:
            si = tb.get('step_identity', {})
            station = si.get('station', '')
            thread = si.get('thread_idx', -1)
            step = si.get('step_value', -1)
            sv = si.get('state_variable', '')
            # Normalize: SMC2 state vars use 'Station'
            if not station and sv.startswith('m_n'):
                station = 'Station'
            key = (station, thread, step)
            boundary_map[key] = tb

    rows = []
    for sl in step_logic:
        # Support both old schema (thread_idx at top level) and new schema (step_identity)
        if 'step_identity' in sl:
            identity = sl['step_identity']
            thread = identity['thread_idx']
            step = identity['step_value']
            sv = identity.get('state_variable', step_var_name)
            station = identity.get('station', '')
            description = sl.get('description', '')
            comment = sl.get('fact_evidence', {}).get('comment', '') if isinstance(sl.get('fact_evidence'), dict) else sl.get('comment', '')
            actions = [sf.get('variable', sf.get('raw_action', '')) for sf in sl.get('action_semantic_facts', [])[:10]]
            fact = description if description else None
        else:
            thread = sl['thread_idx']
            step = sl['step_value']
            sv = step_var_name
            station = sl.get('station', '')
            actions = sl.get('actions', [])
            comment = sl.get('comment', '')
            fact = action_facts.get((thread, step)) if action_facts else None
            if isinstance(fact, dict):
                fact = fact.get('description', '')

        start_label = _format_start_label(sv, station, thread, step)

        # Use timing boundary if available, otherwise fallback
        # Normalize station for SMC2
        lookup_station = station
        if not lookup_station and sv.startswith('m_n'):
            lookup_station = 'Station'
        boundary_key = (lookup_station, thread, step)
        tb = boundary_map.get(boundary_key)
        if tb:
            end_label = tb.get('end_expression', '')
            source_kind = tb.get('provenance', '')
            boundary_semantics = tb.get('boundary_kind', '')
            confidence = tb.get('confidence', '')
            if confidence == 'high':
                evidence_level = 'source_explicit'
            elif confidence == 'medium':
                evidence_level = 'control_flow_verified'
            else:
                evidence_level = 'inferred'
            action_index = tb.get('action_index', -1)
            parallel_actions = tb.get('parallel_actions', [])
        else:
            end_label = _format_end_label(sv, station, thread, step)
            source_kind = 'fallback'
            boundary_semantics = 'step_exit'
            evidence_level = 'inferred'
            action_index = -1
            parallel_actions = []

        action_desc = fact if fact else _build_action_desc(comment, actions)
        in_trace = bool(trace_appeared) and (thread, step) in trace_appeared
        rows.append({
            'thread': thread,
            'step': step,
            'state_variable': sv,
            'station': station,
            'start_label': start_label,
            'end_label': end_label,
            'action_desc': action_desc,
            'comment': comment,
            'in_trace': in_trace,
            'has_fact': fact is not None,
            'source_kind': source_kind,
            'boundary_semantics': boundary_semantics,
            'evidence_level': evidence_level,
            'action_index': action_index,
            'parallel_actions': parallel_actions,
        })
    rows.sort(key=lambda r: (r.get('state_variable',''), r.get('station',''), r['thread'], r['step']))
    return rows


def _format_start_label(sv, station, thread, step):
    """Format start label based on state variable type."""
    if sv == 'Process_steps':
        return f'{station}V.Process_steps[{thread}]={step}'
    elif sv == 'AutoStep':
        return f'{station}.AutoStep[{thread}]={step}'
    elif sv == 'm_nNextAutoStep':
        return f'm_nNextAutoStep[{thread}]=INT#{step}'
    elif sv:
        if station:
            return f'{station}V.{sv}[{thread}]={step}'
        return f'{sv}[{thread}]=INT#{step}'
    return f'{step_var_name}[{thread}]=INT#{step}'


def _format_end_label(sv, station, thread, step):
    """Format fallback end label (NOT step)."""
    expr = _format_start_label(sv, station, thread, step)
    return f'NOT ({expr})' if expr else ''


def export_universal_table(rows: list[dict],
                           output_path: str,
                           device_name: str = '',
                           ) -> tuple[int, int]:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '步序表'
    green_fill = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')
    green_font = Font(color='006100')
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True, size=11)
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin'),
    )
    # Evidence level fills
    high_fill = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')
    med_fill = PatternFill(start_color='FFEB9C', end_color='FFEB9C', fill_type='solid')
    low_fill = PatternFill(start_color='FFC7CE', end_color='FFC7CE', fill_type='solid')

    title = f'{device_name} 工程动作时序表' if device_name else '工程动作时序表'
    trace_count = sum(1 for r in rows if r['in_trace'])
    title += f'  (共{len(rows)}步, trace覆盖{trace_count}步)'
    ws.merge_cells('A1:L1')
    cell = ws.cell(1, 1, title)
    cell.font = Font(bold=True, size=14)
    cell.alignment = Alignment(horizontal='center')
    headers = ['StateVar', 'Station', 'Thread', 'Step',
               '开始标签', '结束标签', '动作描述', '程序注释',
               'source_kind', 'boundary_semantics', 'evidence_level', 'action_index']
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(2, ci, h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
        cell.border = thin_border
    for ri, row in enumerate(rows, 3):
        values = [
            row.get('state_variable', ''),
            row.get('station', ''),
            row['thread'],
            row['step'],
            row['start_label'],
            row['end_label'],
            row['action_desc'],
            row['comment'],
            row.get('source_kind', ''),
            row.get('boundary_semantics', ''),
            row.get('evidence_level', ''),
            row.get('action_index', -1),
        ]
        for ci, val in enumerate(values, 1):
            cell = ws.cell(ri, ci, val)
            cell.border = thin_border
            cell.alignment = Alignment(
                horizontal='center' if ci <= 4 else 'left',
                vertical='center',
                wrap_text=ci >= 5,
            )
            if row['in_trace']:
                cell.fill = green_fill
                cell.font = green_font
            # Color evidence_level column
            if ci == 11:
                ev = row.get('evidence_level', '')
                if ev == 'source_explicit':
                    cell.fill = high_fill
                elif ev == 'control_flow_verified':
                    cell.fill = med_fill
                elif ev == 'inferred':
                    cell.fill = low_fill
    col_widths = [14, 8, 8, 8, 35, 40, 50, 30, 18, 20, 18, 10]
    for ci, w in enumerate(col_widths, 1):
        ws.column_dimensions[chr(64 + ci) if ci <= 26 else 'L'].width = w
    ws.freeze_panes = 'A3'
    wb.save(output_path)
    return len(rows), trace_count


def generate_universal_table(step_logic_path: str,
                             trace_path: str | None,
                             output_path: str,
                             step_var_name: str = 'm_nNextAutoStep',
                             device_name: str = '',
                             action_facts: dict[tuple[int, int], Any] | None = None,
                             timing_boundaries: list[dict] | None = None,
                             ) -> tuple[int, int, list[dict]]:
    with open(step_logic_path, 'r', encoding='utf-8') as f:
        step_logic = json.load(f)
    trace_appeared = parse_trace(trace_path) if trace_path else set()
    rows = build_universal_table(
        step_logic, step_var_name, trace_appeared, action_facts=action_facts,
        timing_boundaries=timing_boundaries,
    )
    total, trace_count = export_universal_table(rows, output_path, device_name)
    return total, trace_count, rows
