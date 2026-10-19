#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bxcj_demo/lib/demo_exporter.py — P5 + P3 最终 Excel 导出

输出 4 个 Sheet：
  Sheet1: 工程动作采集表（主表，含颜色 + review_reason_code）
  Sheet2: 完整Step与状态路径
  Sheet3: 计时与Trace证据
  Sheet4: 黄色待复核清单

颜色规则：
  绿色 (C6EFCE)：Trace 实测有效 + 无 review_reason_code
  黄色 (FFEB9C)：有 review_reason_code 或 trace 配对歧义
  无填充：静态可信但未实测（无 trace，但 start/end 标签均解析成功）

review_reason_code 字典（中文说明）：
  no_verified_end_tag          - 无法证明唯一结束条件
  no_verified_next_state       - 无验证的下一业务状态
  completion_without_next_state - 有完成信号但无下一业务状态
  ambiguous_end_tag            - 多个候选结束标签
  missing_description_evidence - 中文动作描述缺少必要依据
  trace_cycle_ambiguity        - Trace 周期配对歧义
  trace_signal_missing         - Trace 信号缺失
  trace_version_mismatch       - 源码与 Trace 版本不匹配
  unknown_action_direction     - 无法确认动作方向
  source_trace_mismatch        - 源码与 trace 矛盾
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


# 颜色定义
GREEN_FILL = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')
YELLOW_FILL = PatternFill(start_color='FFEB9C', end_color='FFEB9C', fill_type='solid')
NO_FILL = PatternFill(fill_type=None)
HEADER_FILL = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
HEADER_FONT = Font(color='FFFFFF', bold=True)
BORDER_THIN = Border(
    left=Side(style='thin'), right=Side(style='thin'),
    top=Side(style='thin'), bottom=Side(style='thin')
)

# review_reason_code 中文说明
REVIEW_REASON_DESC = {
    'no_verified_end_tag': '无法证明唯一结束条件',
    'no_verified_next_state': '无验证的下一业务状态',
    'completion_without_next_state': '有完成信号但无下一业务状态',
    'ambiguous_end_tag': '多个候选结束标签',
    'missing_description_evidence': '中文动作描述缺少必要依据',
    'trace_cycle_ambiguity': 'Trace 周期配对歧义',
    'trace_signal_missing': 'Trace 信号缺失',
    'trace_version_mismatch': '源码与 Trace 版本不匹配',
    'unknown_action_direction': '无法确认动作方向',
    'source_trace_mismatch': '源码与 trace 矛盾',
    'no_pair': 'Trace 中未找到配对事件',
    'no_signal': 'Trace 中未找到信号',
}


# ===== P3 中文动作描述增强 =====

# 已知执行机构前缀（仅作 hint，非硬规则）
ACTUATOR_HINTS = {
    'Cy_': '气缸',
    'Cy_Clamp': '夹紧气缸',
    'Cy_Push': '推送气缸',
    'CY': '气缸',
    'SV_': '伺服',
    'Robot': '机器人',
    'Axis': '轴',
    'Va_': '真空',
    'Vacuum': '真空',
    'PS_': '压力',
    'PL_': '位置',
    'Weld': '焊接',
    'Scan': '扫码',
    'Conveyor': '输送',
}

# 操作动词（来自命令模式）
OPERATION_HINTS = {
    'ToPos': '移动至位置',
    'Location': '定位至',
    'Auto_V': '伸出',
    'Auto_R': '缩回',
    'ToHome': '回零',
    'AbsEx': '绝对执行至',
    'AbsExecute': '绝对执行至',
    'Start': '启动',
}


class DescriptionComposer:
    """P3: 中文动作描述合成器。"""

    def __init__(self, steps_described: list = None):
        self.steps_described = steps_described or []
        self.desc_lookup = {}
        for s in self.steps_described:
            si = s.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('step_value', -1))
            self.desc_lookup[key] = s

    def compose(self, action: dict) -> tuple[str, str]:
        """返回 (描述, review_reason_code)。"""
        si = action.get('step_identity', {})
        step_key = (si.get('station', ''), si.get('thread_idx', -1),
                    si.get('step_value', -1))
        commands = action.get('commands', [])
        action_type = action.get('action_type', '')
        original_desc = action.get('description', '')

        # 1. 优先用原始描述（如来自注释）
        if original_desc and not self._is_low_quality(original_desc):
            return self._refine_desc(original_desc, si, commands), ''

        # 2. 从 steps_described 取
        sd = self.desc_lookup.get(step_key, {})
        sd_desc = sd.get('description', '') if sd else ''
        if sd_desc and not self._is_low_quality(sd_desc):
            return self._refine_desc(sd_desc, si, commands), ''

        # 3. 从命令变量合成
        if commands:
            desc, reason = self._compose_from_commands(commands, action_type, si)
            return desc, reason

        # 4. 等待动作特殊处理
        if action_type == 'wait':
            return original_desc or '等待信号', ''

        # 5. 转移动作
        if action_type == 'transition':
            return original_desc or '步序转移', ''

        # 6. 兜底
        return original_desc or '执行动作', 'missing_description_evidence'

    def _is_low_quality(self, desc: str) -> bool:
        """判断描述是否低质量（仅变量名/无动词）。"""
        if not desc:
            return True
        # 仅含变量名/编号
        if re.match(r'^[\w\.\[\]\d\-\s]+$', desc) and not any(
            c in desc for c in '移动等待夹紧真空伸出缩回回零启动定位执行'):
            return True
        return False

    def _refine_desc(self, desc: str, si: dict, commands: list) -> str:
        """轻微优化描述（去除多余空白、统一格式）。"""
        desc = desc.strip()
        # 去除前导步号
        desc = re.sub(r'^\d+[\.\s\-]+', '', desc)
        return desc

    def _compose_from_commands(self, commands: list, action_type: str,
                                si: dict) -> tuple[str, str]:
        """从命令变量合成描述。"""
        station = si.get('station', '')
        parts = []
        reason = ''

        for cmd in commands[:3]:  # 最多3个命令
            actor = self._extract_actor(cmd)
            operation = self._extract_operation(cmd)
            target = self._extract_target(cmd)

            if actor:
                parts.append(actor)
            if operation:
                parts.append(operation)
            if target:
                parts.append(target)

        if not parts:
            # 完全无法解析
            return f'{station or ""}执行动作', 'missing_description_evidence'

        desc = ''.join(parts)
        # 如果只有 actor 没有 operation
        if parts and not any(self._extract_operation(c) for c in commands):
            reason = 'unknown_action_direction'
            # 加默认动词
            if action_type == 'clamp':
                desc += '动作'
            elif action_type == 'vacuum':
                desc += '吸放'
            elif action_type == 'motion':
                desc += '运动'

        return desc, reason

    def _extract_actor(self, cmd_var: str) -> str:
        """提取执行机构名。"""
        if not cmd_var:
            return ''
        # 取第一段（actuator 路径）
        first = cmd_var.split('.')[0]
        # 检查 hint
        for prefix, hint in ACTUATOR_HINTS.items():
            if first.startswith(prefix) or first == prefix:
                # 中文化
                if prefix == 'Robot':
                    return '机器人'
                if prefix == 'Axis':
                    return first  # 保留轴名
                return hint
        return first  # 保留原名

    def _extract_operation(self, cmd_var: str) -> str:
        """提取操作动词。"""
        if not cmd_var:
            return ''
        for op_name, op_verb in OPERATION_HINTS.items():
            if op_name in cmd_var:
                return op_verb
        return ''

    def _extract_target(self, cmd_var: str) -> str:
        """提取目标位置/对象。"""
        if not cmd_var:
            return ''
        # ToPos[N] → 位置 N
        m = re.search(r'ToPos\[(\d+)\]', cmd_var)
        if m:
            return m.group(1)
        m = re.search(r'AbsEx\[(\d+)\]', cmd_var)
        if m:
            return m.group(1)
        return ''


class DemoExporter:
    """P5: 最终 Excel 导出器。"""

    def __init__(self, work_output_dir: Path, demo_output_dir: Path,
                 cfg: dict = None):
        self.work_output_dir = work_output_dir
        self.demo_output_dir = demo_output_dir
        self.cfg = cfg or {}
        self.project_name = self.cfg.get('project', {}).get('name', 'unknown')
        self.display_name = self.cfg.get('project', {}).get('display_name', '')

    def _load_json(self, name: str, default=None):
        p = self.work_output_dir / name
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
        return default if default is not None else {}

    def run(self) -> bool:
        print('  [DemoExporter] 开始导出...')

        # 加载所有产物
        fa_data = self._load_json('filtered_actions.json', {'actions': []})
        trace_data = self._load_json('trace_match_report.json', {'matches': [], 'enabled': False})
        steps_described = self._load_json('steps_described.json', [])
        slices = self._load_json('step_logic_slices.json', [])

        actions = fa_data.get('actions', [])
        trace_matches = trace_data.get('matches', [])
        trace_enabled = trace_data.get('enabled', False)

        # 建立 action_id → trace_match 映射
        trace_by_action = {m['action_id']: m for m in trace_matches}

        # 描述合成器
        composer = DescriptionComposer(steps_described)

        # 准备主表行（含颜色判定）
        main_rows = []
        for a in actions:
            if not a.get('in_main_table', True):
                continue
            # 合成描述
            desc, desc_reason = composer.compose(a)
            # 合并 review_reason_code
            reasons = []
            if a.get('review_reason_code'):
                reasons.append(a['review_reason_code'])
            if desc_reason:
                reasons.append(desc_reason)

            # trace 信息
            tm = trace_by_action.get(a.get('action_id', ''), {})
            duration_ms = tm.get('duration_ms')
            match_status = tm.get('match_status', 'no_attempt')
            trace_confidence = tm.get('confidence', '')

            # trace 相关的 review_reason
            if trace_enabled:
                if match_status == 'no_signal':
                    reasons.append('trace_signal_missing')
                elif match_status == 'no_pair':
                    reasons.append('no_pair')
                elif match_status == 'version_mismatch':
                    reasons.append('trace_version_mismatch')
                elif match_status == 'matched' and trace_confidence == 'low':
                    if 'trace_version_mismatch' not in reasons:
                        reasons.append('trace_version_mismatch')

            # 颜色判定
            color, status_text = self._judge_color(
                trace_enabled, match_status, duration_ms, reasons
            )

            # 唯一 review_reason_code（取第一个）
            primary_reason = reasons[0] if reasons else ''
            review_desc = REVIEW_REASON_DESC.get(primary_reason, primary_reason)

            main_rows.append({
                'station': a.get('step_identity', {}).get('station', ''),
                'thread': a.get('step_identity', {}).get('thread_idx', -1),
                'step': a.get('step_identity', {}).get('step_value', -1),
                'action_no': a.get('event_seq', 0),
                'description': desc,
                'action_type': a.get('action_type', ''),
                'start_label': a.get('start_label', ''),
                'end_label': a.get('selected_end_tag', '') or a.get('end_label', ''),
                'timing_intent': a.get('timing_intent', ''),
                'duration_ms': duration_ms if duration_ms is not None else '',
                'path': a.get('path_id', ''),
                'path_type': a.get('path_type', ''),
                'evidence_level': a.get('evidence_level', ''),
                'completion_evidence': a.get('completion_evidence', ''),
                'alternative_end_tags': ', '.join(a.get('alternative_end_tags', [])),
                'selection_reason': a.get('selection_reason', ''),
                'review_reason_code': primary_reason,
                'review_reason_desc': review_desc,
                'verify_status': status_text,
                'color': color,
                'trace_match_status': match_status,
                'trace_confidence': trace_confidence,
                'trace_start_ts': tm.get('trace_start_timestamp', ''),
                'trace_end_ts': tm.get('trace_end_timestamp', ''),
                'trace_cycle_id': tm.get('trace_cycle_id', ''),
                'comment': a.get('comment', ''),
                'commands': ', '.join(a.get('commands', [])),
                'completions': ', '.join(a.get('completions', [])),
            })

        print(f'  [DemoExporter] 主表行数: {len(main_rows)}')

        # 排序：station → thread → step
        main_rows.sort(key=lambda r: (r['station'], r['thread'], r['step'], r['action_no']))

        # 重新分配动作号
        for i, row in enumerate(main_rows, 1):
            row['action_no'] = i

        # 颜色统计
        color_counts = Counter(r['color'] for r in main_rows)
        print(f'  [DemoExporter] 颜色分布: {dict(color_counts)}')

        # ===== 构建 Excel =====
        wb = Workbook()
        wb.remove(wb.active)

        # Sheet1: 工程动作采集表（主表）
        ws1 = wb.create_sheet('工程动作采集表')
        headers1 = ['工序/构成要素', '动作号', '动作描述', '开始标签', '结束标签',
                    '时长(ms)', '路径/分支', '核验状态', '备注',
                    '动作类型', '证据等级', '完成信号证据', '备选结束标签',
                    '选择原因', '复核原因码', '复核说明']
        ws1.append(headers1)
        for cell in ws1[1]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = BORDER_THIN

        for r in main_rows:
            station_thread = f"{r['station']} Ps[{r['thread']}]" if r['thread'] >= 0 else r['station']
            row_data = [
                station_thread,
                r['action_no'],
                r['description'],
                r['start_label'],
                r['end_label'],
                r['duration_ms'],
                f"{r['path']} ({r['path_type']})" if r['path_type'] else r['path'],
                r['verify_status'],
                r['review_reason_desc'] if r['review_reason_code'] else '',
                r['action_type'],
                r['evidence_level'],
                r['completion_evidence'],
                r['alternative_end_tags'],
                r['selection_reason'],
                r['review_reason_code'],
                r['review_reason_desc'],
            ]
            ws1.append(row_data)
            row_idx = ws1.max_row
            # 整行着色
            fill = GREEN_FILL if r['color'] == 'green' else (
                YELLOW_FILL if r['color'] == 'yellow' else NO_FILL
            )
            for col_idx in range(1, len(row_data) + 1):
                cell = ws1.cell(row=row_idx, column=col_idx)
                cell.fill = fill
                cell.border = BORDER_THIN
                cell.alignment = Alignment(vertical='center', wrap_text=True)

        # 列宽
        col_widths1 = [16, 8, 28, 30, 30, 12, 14, 14, 24, 10, 14, 22, 24, 28, 14, 24]
        for i, w in enumerate(col_widths1, 1):
            ws1.column_dimensions[get_column_letter(i)].width = w

        # Sheet2: 完整Step与状态路径
        ws2 = wb.create_sheet('完整Step与状态路径')
        self._build_path_sheet(ws2, slices)
        print(f'  [DemoExporter] Sheet2: {len(slices)} 个 step')

        # Sheet3: 计时与Trace证据
        ws3 = wb.create_sheet('计时与Trace证据')
        self._build_trace_evidence_sheet(ws3, main_rows, trace_data)
        print(f'  [DemoExporter] Sheet3: trace 证据')

        # Sheet4: 黄色待复核清单
        ws4 = wb.create_sheet('黄色待复核清单')
        yellow_rows = [r for r in main_rows if r['color'] == 'yellow']
        self._build_review_sheet(ws4, yellow_rows)
        print(f'  [DemoExporter] Sheet4: {len(yellow_rows)} 行待复核')

        # 保存
        out_path = self.demo_output_dir / '步序采集表.xlsx'
        wb.save(str(out_path))
        print(f'  [DemoExporter] 输出: {out_path}')

        # 保存 evidence.json（合并所有证据）
        evidence = {
            'project': self.project_name,
            'display_name': self.display_name,
            'total_actions': len(main_rows),
            'color_distribution': dict(color_counts),
            'trace_enabled': trace_enabled,
            'trace_format': trace_data.get('trace_format', ''),
            'trace_signal_count': trace_data.get('signal_count', 0),
            'trace_matched': trace_data.get('matched_actions', 0),
            'review_reasons': dict(Counter(r['review_reason_code'] for r in main_rows
                                            if r['review_reason_code'])),
            'sample_actions': main_rows[:10],
        }
        ev_path = self.demo_output_dir / 'evidence.json'
        with open(ev_path, 'w', encoding='utf-8') as f:
            json.dump(evidence, f, ensure_ascii=False, indent=2, default=str)
        print(f'  [DemoExporter] evidence: {ev_path}')

        return True

    def _judge_color(self, trace_enabled: bool, match_status: str,
                     duration_ms: any, reasons: list) -> tuple[str, str]:
        """判定颜色与状态文字。"""
        has_reasons = bool(reasons)
        if trace_enabled and match_status == 'matched' and duration_ms is not None:
            if has_reasons:
                return 'yellow', '已测时/待复核'
            return 'green', '已测时'
        if trace_enabled and match_status != 'matched':
            # trace 启用但未配对
            if has_reasons:
                return 'yellow', '待复核'
            return 'yellow', '待复核（Trace未配对）'
        # 无 trace
        if has_reasons:
            return 'yellow', '待复核'
        return 'none', '静态已验证'

    def _build_path_sheet(self, ws, slices: list):
        """Sheet2: 完整 Step 与状态路径。"""
        headers = ['工站', 'Thread', 'Step', '状态变量', '注释',
                   '转移目标', '转移条件', '动作数', '调用', '分支信息']
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = BORDER_THIN

        for s in slices:
            si = s.get('step_identity', {})
            trans = s.get('transitions', [])
            trans_strs = []
            for t in trans:
                ts = t.get('target_step', {})
                tv = ts.get('value', -1)
                guard = t.get('boolean_expr', {}).get('raw', '') or ''
                trans_strs.append(f'→{tv} ({guard})' if guard else f'→{tv}')
            actions = s.get('actions', [])
            calls = s.get('calls', [])
            ws.append([
                si.get('station', ''),
                si.get('thread_idx', -1),
                si.get('step_value', -1),
                si.get('state_variable', ''),
                s.get('comment', ''),
                '; '.join(trans_strs),
                '; '.join(t.get('boolean_expr', {}).get('raw', '') for t in trans),
                len(actions),
                '; '.join(c.get('callee', '') for c in calls),
                s.get('branch_topology', {}).get('type', '') if isinstance(s.get('branch_topology'), dict) else '',
            ])
            row_idx = ws.max_row
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.border = BORDER_THIN
                cell.alignment = Alignment(vertical='center', wrap_text=True)

        for i, w in enumerate([12, 8, 8, 18, 30, 30, 30, 8, 20, 14], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def _build_trace_evidence_sheet(self, ws, main_rows: list, trace_data: dict):
        """Sheet3: 计时与 Trace 证据。"""
        headers = ['动作号', '工站', 'Step', '开始标签', '结束标签',
                   'start信号', 'end信号', 'start已解析', 'end已解析',
                   '开始时间(ms)', '结束时间(ms)', '时长(ms)',
                   '配对状态', '置信度', '周期ID', '不确定性原因']
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = BORDER_THIN

        # 顶部加 trace 元信息
        ws.append(['[Trace 元信息]'])
        ws.append(['Trace 文件', trace_data.get('trace_file', '未配置')])
        ws.append(['格式', trace_data.get('trace_format', '')])
        ws.append(['信号数', trace_data.get('signal_count', 0)])
        ws.append(['版本警告', '是' if trace_data.get('version_warning') else '否'])
        ws.append(['已配对动作', trace_data.get('matched_actions', 0)])
        ws.append(['未配对动作', trace_data.get('unmatched_actions', 0)])
        ws.append([])

        # 重新写表头
        ws.append(headers)
        header_row_idx = ws.max_row
        for cell in ws[header_row_idx]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = BORDER_THIN

        for r in main_rows:
            ws.append([
                r['action_no'],
                r['station'],
                r['step'],
                r['start_label'],
                r['end_label'],
                '',  # start_signal
                '',  # end_signal
                '',  # start_resolved
                '',  # end_resolved
                r['trace_start_ts'],
                r['trace_end_ts'],
                r['duration_ms'],
                r['trace_match_status'],
                r['trace_confidence'],
                r['trace_cycle_id'],
                REVIEW_REASON_DESC.get(r['review_reason_code'], r['review_reason_code']),
            ])
            row_idx = ws.max_row
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.border = BORDER_THIN
                cell.alignment = Alignment(vertical='center', wrap_text=True)

        for i, w in enumerate([8, 12, 8, 28, 28, 22, 22, 10, 10, 14, 14, 12, 14, 10, 12, 28], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def _build_review_sheet(self, ws, yellow_rows: list):
        """Sheet4: 黄色待复核清单。"""
        headers = ['动作号', '工站', 'Step', '动作描述', '开始标签', '结束标签',
                   '复核原因码', '中文说明', '选择原因', '动作类型']
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = BORDER_THIN

        for r in yellow_rows:
            ws.append([
                r['action_no'],
                r['station'],
                r['step'],
                r['description'],
                r['start_label'],
                r['end_label'],
                r['review_reason_code'],
                r['review_reason_desc'],
                r['selection_reason'],
                r['action_type'],
            ])
            row_idx = ws.max_row
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.fill = YELLOW_FILL
                cell.border = BORDER_THIN
                cell.alignment = Alignment(vertical='center', wrap_text=True)

        for i, w in enumerate([8, 12, 8, 28, 28, 28, 22, 24, 28, 12], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
