#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bxcj_demo/dlib/comparison_builder.py — v10.35 工程步序对照表生成器

生成 5-Sheet 对照 Excel：
  1. 步序标签对比（18列 A:R，双层表头，工站/子流程分隔）
  2. 统计总览
  3. 不一致原因分析
  4. 动作描述核查
  5. Trace计时核查

严格遵守 P0 隔离原则：只读冻结的生产产物，不修改生产答案。
"""
from __future__ import annotations

import json
import re
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ==== 配色（严格复刻参考表）====
COLOR_HUMAN_GROUP = 'FF5B9BD5'      # 人工组标题
COLOR_AUTO_GROUP = 'FF70AD47'       # 程序组标题
COLOR_CONSIST_GROUP = 'FF4472C4'    # 一致性组标题
COLOR_COL_HEADER = 'FF4472C4'       # 列标题
COLOR_STATION_ROW = 'FF2F5496'      # 工站行
COLOR_SUBFLOW_ROW = 'FFD9E1F2'      # 子流程行
COLOR_LABEL_MATCH = 'FFC6EFCE'      # 标签匹配绿色
COLOR_TRACE_GREEN = 'FFE2EFDA'      # 有效Trace时长浅绿
COLOR_REVIEW_YELLOW = 'FFFFEB9C'    # 需复核黄色
COLOR_DIFF_RED = 'FFFFC7CE'         # 明确差异浅红
COLOR_EXTRA_YELLOW = 'FFFFF2CC'     # 额外候选浅黄

# Font
FONT_WHITE_BOLD = Font(color='FFFFFF', bold=True, size=10)
FONT_STATION = Font(color='FFFFFF', bold=True, size=11)
FONT_NORMAL = Font(size=10)
FONT_BOLD = Font(size=10, bold=True)

# Border
BORDER_THIN = Border(
    left=Side(style='thin'), right=Side(style='thin'),
    top=Side(style='thin'), bottom=Side(style='thin')
)

# Alignment
ALIGN_CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
ALIGN_LEFT = Alignment(horizontal='left', vertical='center', wrap_text=True)


def _fill(color: str) -> PatternFill:
    return PatternFill(start_color=color, end_color=color, fill_type='solid')


# ==== 数据模型 ====

@dataclass
class HumanAction:
    """人工表中的一行动作。"""
    module_name: str = ''        # 构成要素/模块名
    action_desc: str = ''        # 动作描述
    start_time: str = ''         # 开始适用
    duration: str = ''           # 动作时间
    end_time: str = ''           # 完成时间
    start_label: str = ''        # 开始标签（可能为空）
    end_label: str = ''          # 结束标签（可能为空）
    remark: str = ''             # 备注
    action_no: str = ''          # 动作号
    sync_no: str = ''            # 同步号
    process_no: str = ''         # 工序号
    station: str = ''            # 推断的工站
    thread: int = -1             # 推断的线程
    step_value: int = -1         # 推断的步值（配对后填入）


@dataclass
class AutoAction:
    """自动采集表中的一行动作。"""
    action_id: str = ''
    station: str = ''
    thread: int = -1
    step_value: int = -1
    state_var: str = ''
    description: str = ''
    start_label: str = ''
    end_label: str = ''
    selected_end_tag: str = ''
    action_type: str = ''
    timing_intent: str = ''
    evidence_level: str = ''
    review_reason_code: str = ''
    commands: list = field(default_factory=list)
    completions: list = field(default_factory=list)
    comment: str = ''
    path_id: str = ''
    path_type: str = ''
    # Trace 信息
    trace_duration_ms: float = None
    trace_match_status: str = ''
    trace_confidence: str = ''
    trace_cycle_id: str = ''
    trace_occurrences: list = field(default_factory=list)


@dataclass
class PairResult:
    """配对结果。"""
    human: HumanAction = None
    auto: AutoAction = None
    pair_type: str = 'unpaired'   # paired / human_only / auto_only
    i_consistent: str = ''        # Y / N / 不适用
    j_consistent: str = ''
    status: str = ''              # 完全一致 / I一致J不一致 / 仅程序 / 仅人工 / ...
    primary_reason: str = ''
    secondary_reasons: list = field(default_factory=list)
    evidence_ref: str = ''
    review_suggestion: str = ''


class ComparisonBuilder:
    """对照表生成器。"""

    def __init__(self, project_name: str, display_name: str,
                 work_output_dir: Path, demo_output_dir: Path,
                 manual_table_path: str = None, repo_root: str = '.'):
        self.project_name = project_name
        self.display_name = display_name
        self.work_output_dir = work_output_dir
        self.demo_output_dir = demo_output_dir
        self.manual_table_path = manual_table_path
        self.repo_root = Path(repo_root)

        self.auto_actions: list[AutoAction] = []
        self.human_actions: list[HumanAction] = []
        self.pair_results: list[PairResult] = []
        self.trace_report: dict = {}

    # ==== 加载生产产物 ====

    def load_production(self):
        """加载冻结的生产产物（只读）。"""
        fa_path = self.work_output_dir / 'filtered_actions.json'
        with open(fa_path, 'r', encoding='utf-8') as f:
            fa = json.load(f)
        actions = [a for a in fa['actions'] if a.get('in_main_table', True)]

        # 加载 trace
        tr_path = self.work_output_dir / 'trace_match_report.json'
        trace_by_action = {}
        if tr_path.exists():
            with open(tr_path, 'r', encoding='utf-8') as f:
                self.trace_report = json.load(f)
            for m in self.trace_report.get('matches', []):
                trace_by_action[m.get('action_id', '')] = m

        for a in actions:
            si = a.get('step_identity', {})
            tm = trace_by_action.get(a.get('action_id', ''), {})
            auto = AutoAction(
                action_id=a.get('action_id', ''),
                station=si.get('station', ''),
                thread=si.get('thread_idx', -1),
                step_value=si.get('step_value', -1),
                state_var=si.get('state_variable', ''),
                description=a.get('description', ''),
                start_label=a.get('start_label', ''),
                end_label=a.get('selected_end_tag', '') or a.get('end_label', ''),
                selected_end_tag=a.get('selected_end_tag', '') or a.get('end_label', ''),
                action_type=a.get('action_type', ''),
                timing_intent=a.get('timing_intent', ''),
                evidence_level=a.get('evidence_level', ''),
                review_reason_code=a.get('review_reason_code', ''),
                commands=a.get('commands', []),
                completions=a.get('completions', []),
                comment=a.get('comment', ''),
                path_id=a.get('path_id', ''),
                path_type=a.get('path_type', ''),
                trace_duration_ms=tm.get('duration_ms'),
                trace_match_status=tm.get('match_status', ''),
                trace_confidence=tm.get('confidence', ''),
                trace_cycle_id=tm.get('trace_cycle_id', ''),
                trace_occurrences=tm.get('all_occurrences', []),
            )
            self.auto_actions.append(auto)

        print(f'  [Comparison] 加载生产动作: {len(self.auto_actions)}')

    # ==== 加载人工表 ====

    def load_human_table(self):
        """加载人工 Gold 表。"""
        if not self.manual_table_path:
            print('  [Comparison] 未配置人工表')
            return

        p = Path(self.manual_table_path)
        if not p.is_absolute():
            p = self.repo_root / self.manual_table_path
        if not p.exists():
            print(f'  [Comparison] 人工表不存在: {p}')
            return

        if self.project_name == 'transfer':
            self._load_transfer_human(p)
        elif self.project_name == 'ultrasound':
            self._load_ultrasound_human(p)
        elif self.project_name == 'mylar':
            self._load_mylar_human(p)

        print(f'  [Comparison] 加载人工动作: {len(self.human_actions)}')

    def _load_transfer_human(self, path: Path):
        """转接片人工表（.xls）。"""
        import xlrd
        wb = xlrd.open_workbook(str(path))
        sh = wb.sheet_by_index(0)
        current_module = ''
        for r in range(4, sh.nrows):  # 从第5行开始
            module = str(sh.cell_value(r, 1)).strip()
            action = str(sh.cell_value(r, 2)).strip()
            if not action or action == '合计':
                continue
            if module:
                current_module = module
            ha = HumanAction(
                module_name=current_module,
                action_desc=action,
                start_time=str(sh.cell_value(r, 3)),
                duration=str(sh.cell_value(r, 4)),
                end_time=str(sh.cell_value(r, 5)),
                start_label=str(sh.cell_value(r, 6)).strip(),
                end_label=str(sh.cell_value(r, 7)).strip(),
                remark=str(sh.cell_value(r, 8)).strip(),
            )
            self.human_actions.append(ha)

    def _load_ultrasound_human(self, path: Path):
        """超声人工表（.xls）。"""
        import xlrd
        wb = xlrd.open_workbook(str(path))
        sh = wb.sheet_by_index(0)
        current_station = ''
        for r in range(1, sh.nrows):
            station = str(sh.cell_value(r, 0)).strip()
            action_name = str(sh.cell_value(r, 1)).strip()
            action_logic = str(sh.cell_value(r, 2)).strip()
            action_no = str(sh.cell_value(r, 3)).strip()
            if not action_name and not action_logic:
                continue
            if station:
                current_station = station
            desc = action_name
            if action_logic and action_logic != action_name:
                desc = f'{action_name} {action_logic}' if action_name else action_logic
            ha = HumanAction(
                module_name=current_station,
                action_desc=desc,
                action_no=action_no,
            )
            self.human_actions.append(ha)

    def _load_mylar_human(self, path: Path):
        """Mylar人工表（.xlsx）。"""
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True)
        sh = wb.active
        current_module = ''
        for i, row in enumerate(sh.iter_rows(min_row=3, values_only=True)):
            if not row or all(v is None for v in row):
                continue
            module = str(row[0]).strip() if row[0] else ''
            seq = str(row[1]).strip() if row[1] else ''
            mechanism = str(row[2]).strip() if row[2] else ''
            action = str(row[3]).strip() if row[3] else ''
            state = str(row[4]).strip() if row[4] else ''
            process_no = str(row[6]).strip() if len(row) > 6 and row[6] else ''
            sync_no = str(row[7]).strip() if len(row) > 7 and row[7] else ''
            action_no = str(row[8]).strip() if len(row) > 8 and row[8] else ''
            start_label = str(row[9]).strip() if len(row) > 9 and row[9] else ''

            if not action and not mechanism:
                continue
            if module:
                current_module = module
            desc = action
            if mechanism and mechanism != action:
                desc = f'{mechanism} {action}' if action else mechanism
            if state:
                desc = f'{desc} ({state})'
            ha = HumanAction(
                module_name=current_module,
                action_desc=desc,
                action_no=action_no,
                sync_no=sync_no,
                process_no=process_no,
                start_label=start_label,
            )
            self.human_actions.append(ha)

    # ==== 配对 ====

    def pair_actions(self):
        """分层对齐配对。"""
        # 按工站+线程分组自动动作
        auto_groups = defaultdict(list)
        for a in self.auto_actions:
            key = (a.station, a.thread)
            auto_groups[key].append(a)

        # 人工动作按模块分组
        human_groups = defaultdict(list)
        for h in self.human_actions:
            human_groups[h.module_name].append(h)

        # 匹配模块到工站
        # 策略：按顺序匹配（第一组人工 -> 第一组自动）
        auto_keys = sorted(auto_groups.keys(), key=lambda k: (k[0], k[1]))
        human_keys = list(human_groups.keys())

        # 尝试按模块名中的关键词匹配工站
        module_to_station = {}
        used_stations = set()
        for hk in human_keys:
            for ak in auto_keys:
                if ak in used_stations:
                    continue
                # 简单匹配：模块名包含工站名或工站名包含模块名
                station = ak[0]
                if station and station in hk:
                    module_to_station[hk] = ak
                    used_stations.add(ak)
                    break

        # 未匹配的按顺序分配
        remaining_auto = [ak for ak in auto_keys if ak not in used_stations]
        remaining_human = [hk for hk in human_keys if hk not in module_to_station]
        for hk, ak in zip(remaining_human, remaining_auto):
            module_to_station[hk] = ak

        # 在每组内按顺序配对
        paired_auto = set()
        paired_human = set()
        results = []

        for hk in human_keys:
            ak = module_to_station.get(hk)
            if not ak:
                # 仅人工
                for h in human_groups[hk]:
                    idx = self.human_actions.index(h)
                    paired_human.add(idx)
                    results.append(PairResult(
                        human=h, pair_type='human_only',
                        status='仅人工', primary_reason='human_only',
                        review_suggestion='人工有记录但程序未恢复，可能为漏步或版本差异',
                    ))
                continue

            auto_list = auto_groups[ak]
            human_list = human_groups[hk]

            for i, h in enumerate(human_list):
                h_idx = self.human_actions.index(h)
                if i < len(auto_list):
                    a = auto_list[i]
                    a_idx = self.auto_actions.index(a)
                    if a_idx in paired_auto:
                        continue
                    paired_auto.add(a_idx)
                    paired_human.add(h_idx)
                    # 填入推断的 step 信息
                    h.step_value = a.step_value
                    h.station = a.station
                    h.thread = a.thread
                    if not h.start_label:
                        h.start_label = a.start_label
                    if not h.end_label:
                        h.end_label = a.end_label
                    pr = self._evaluate_pair(h, a)
                    results.append(pr)
                else:
                    paired_human.add(h_idx)
                    results.append(PairResult(
                        human=h, pair_type='human_only',
                        status='仅人工', primary_reason='human_only',
                        review_suggestion='人工有记录但程序步数不足',
                    ))

        # 仅程序
        for i, a in enumerate(self.auto_actions):
            if i not in paired_auto:
                results.append(PairResult(
                    auto=a, pair_type='auto_only',
                    status='仅程序', primary_reason='auto_only',
                    review_suggestion='程序额外候选，不等于错误',
                ))

        # 排序：station -> thread -> step
        def sort_key(pr):
            a = pr.auto
            h = pr.human
            if a:
                return (a.station, a.thread, a.step_value, 0)
            if h:
                return (h.station or 'zzz', h.thread, h.step_value, 1)
            return ('zzz', -1, -1, 2)

        results.sort(key=sort_key)
        self.pair_results = results
        print(f'  [Comparison] 配对结果: {len(results)} 行')

    def _evaluate_pair(self, h: HumanAction, a: AutoAction) -> PairResult:
        """评估配对的一致性。"""
        pr = PairResult(human=h, auto=a, pair_type='paired')

        # I列（开始标签）一致性
        i_match = self._labels_equal(h.start_label, a.start_label)
        # J列（结束标签）一致性
        j_match = self._labels_equal(h.end_label, a.end_label)

        pr.i_consistent = 'Y' if i_match else 'N'
        pr.j_consistent = 'Y' if j_match else 'N'

        if i_match and j_match:
            pr.status = '完全一致'
            pr.primary_reason = ''
        elif i_match and not j_match:
            pr.status = 'I一致、J不一致'
            pr.primary_reason = 'end_label_mismatch'
            pr.secondary_reasons = self._analyze_j_mismatch(h, a)
            pr.review_suggestion = '结束标签口径差异，需确认计时边界'
        elif not i_match and j_match:
            pr.status = 'J一致、I不一致'
            pr.primary_reason = 'start_label_mismatch'
            pr.review_suggestion = '开始标签差异，需确认'
        else:
            pr.status = '标签不一致'
            pr.primary_reason = 'both_label_mismatch'
            pr.review_suggestion = '起止标签均不一致，需人工核查'

        pr.evidence_ref = f'action_id={a.action_id}'
        return pr

    def _labels_equal(self, label1: str, label2: str) -> bool:
        """安全比较两个标签是否等价。"""
        if not label1 and not label2:
            return True
        if not label1 or not label2:
            return False
        # 安全规范化
        s1 = label1.strip().replace(' ', '')
        s2 = label2.strip().replace(' ', '')
        if s1 == s2:
            return True
        # 统一格式差异
        # EM02.Process_steps[0]=0 vs EM02V.Process_steps[0]=0
        s1b = re.sub(r'([A-Z]\d+)V?\.', r'\1.', s1)
        s2b = re.sub(r'([A-Z]\d+)V?\.', r'\1.', s2)
        if s1b == s2b:
            return True
        # INT# 前缀差异
        s1c = s1.replace('INT#', '')
        s2c = s2.replace('INT#', '')
        if s1c == s2c:
            return True
        return False

    def _analyze_j_mismatch(self, h: HumanAction, a: AutoAction) -> list:
        """分析J列不一致的原因。"""
        reasons = []
        if 'NOT' in a.end_label and 'NOT' not in h.end_label:
            reasons.append('step_exit_vs_state')
        if 'PosDone' in a.end_label or 'PosOK' in a.end_label:
            reasons.append('completion_signal')
        if a.timing_intent == 'action_done' and h.end_label != a.end_label:
            reasons.append('timing_intent_difference')
        return reasons

    # ==== Excel 生成 ====

    def build_excel(self) -> Path:
        """生成 5-Sheet 对照 Excel。"""
        wb = Workbook()
        wb.remove(wb.active)

        self._build_main_sheet(wb)
        self._build_stats_sheet(wb)
        self._build_reason_sheet(wb)
        self._build_desc_sheet(wb)
        self._build_trace_sheet(wb)

        out_path = self.demo_output_dir / '步序标签对比.xlsx'
        wb.save(str(out_path))
        print(f'  [Comparison] 输出: {out_path}')
        return out_path

    def _build_main_sheet(self, wb: Workbook):
        """Sheet1: 步序标签对比（18列 A:R）。"""
        ws = wb.create_sheet('步序标签对比')

        # === R1: 组标题 ===
        group_ranges = [
            ('A', 'G', '人工表', COLOR_HUMAN_GROUP),
            ('H', 'N', '程序提取', COLOR_AUTO_GROUP),
            ('O', 'R', '一致性', COLOR_CONSIST_GROUP),
        ]
        for start, end, title, color in group_ranges:
            ws.merge_cells(f'{start}1:{end}1')
            cell = ws[f'{start}1']
            cell.value = title
            cell.fill = _fill(color)
            cell.font = FONT_WHITE_BOLD
            cell.alignment = ALIGN_CENTER

        # === R2: 列标题 ===
        headers = [
            '步值', '动作描述', 'I列-开始标签', 'J列-结束标签', '动作号', '同步号', '备注',
            '步值', '动作描述', 'start_tag', 'end_tag', '来源', '步序类型', '标绿/时长',
            '状态', 'I列一致?', 'J列一致?', '不一致原因',
        ]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(2, c, h)
            cell.fill = _fill(COLOR_COL_HEADER)
            cell.font = FONT_WHITE_BOLD
            cell.alignment = ALIGN_CENTER
            cell.border = BORDER_THIN

        # === 数据行 ===
        row = 3
        current_station = None
        current_thread = None

        for pr in self.pair_results:
            a = pr.auto
            h = pr.human

            # 工站分隔行
            station = (a.station if a else h.station) or ''
            thread = (a.thread if a else h.thread) if (a or h) else -1

            if station != current_station:
                ws.merge_cells(f'A{row}:R{row}')
                cell = ws.cell(row, 1, f'  ▶ 工位 {station}')
                cell.fill = _fill(COLOR_STATION_ROW)
                cell.font = FONT_STATION
                cell.alignment = Alignment(horizontal='left', vertical='center')
                row += 1
                current_station = station
                current_thread = None

            # 子流程分隔行
            if thread != current_thread:
                human_count = sum(1 for p in self.pair_results
                                  if p.human and (p.auto.station if p.auto else p.human.station) == station
                                  and (p.auto.thread if p.auto else p.human.thread) == thread)
                auto_count = sum(1 for p in self.pair_results
                                 if p.auto and p.auto.station == station and p.auto.thread == thread)
                ws.merge_cells(f'A{row}:R{row}')
                cell = ws.cell(row, 1, f'    Ps[{thread}]  (人工{human_count}行 / 程序{auto_count}步)')
                cell.fill = _fill(COLOR_SUBFLOW_ROW)
                cell.font = FONT_BOLD
                cell.alignment = Alignment(horizontal='left', vertical='center')
                row += 1
                current_thread = thread

            # 数据行
            if pr.pair_type == 'paired':
                h = pr.human
                a = pr.auto
                trace_info = self._format_trace_info(a)
                row_data = [
                    h.step_value if h.step_value >= 0 else '',
                    h.action_desc,
                    h.start_label,
                    h.end_label,
                    h.action_no,
                    h.sync_no,
                    h.remark,
                    a.step_value,
                    a.description,
                    a.start_label,
                    a.end_label,
                    a.timing_intent,
                    a.action_type,
                    trace_info,
                    pr.status,
                    pr.i_consistent,
                    pr.j_consistent,
                    ' / '.join([pr.primary_reason] + pr.secondary_reasons) if pr.primary_reason else '',
                ]
            elif pr.pair_type == 'human_only':
                h = pr.human
                row_data = [h.step_value, h.action_desc, h.start_label, h.end_label,
                            h.action_no, h.sync_no, h.remark,
                            '', '', '', '', '', '', '',
                            pr.status, '', '', pr.primary_reason]
            else:  # auto_only
                a = pr.auto
                trace_info = self._format_trace_info(a)
                row_data = ['', '', '', '', '', '', '',
                            a.step_value, a.description, a.start_label, a.end_label,
                            a.timing_intent, a.action_type, trace_info,
                            pr.status, '', '', pr.primary_reason]

            for c, val in enumerate(row_data, 1):
                cell = ws.cell(row, c, val)
                cell.font = FONT_NORMAL
                cell.alignment = ALIGN_LEFT if c in (2, 9, 18) else ALIGN_CENTER
                cell.border = BORDER_THIN

            # 配色
            self._apply_row_colors(ws, row, pr)

            row += 1

        # 列宽
        widths = [6, 30, 35, 35, 8, 8, 15, 6, 30, 35, 35, 14, 12, 14, 14, 8, 8, 20]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        # 冻结窗格
        ws.freeze_panes = 'A3'

    def _format_trace_info(self, a: AutoAction) -> str:
        """格式化 N 列的 Trace 信息。"""
        if a.trace_match_status == 'matched' and a.trace_duration_ms is not None:
            count = len(a.trace_occurrences) if a.trace_occurrences else 1
            if count > 1:
                return f'{a.trace_duration_ms:.0f}ms (x{count})'
            return f'{a.trace_duration_ms:.0f}ms'
        return ''

    def _apply_row_colors(self, ws, row: int, pr: PairResult):
        """应用行颜色。"""
        if pr.pair_type == 'paired':
            # I列匹配 -> C列和J列绿色
            if pr.i_consistent == 'Y':
                ws.cell(row, 3).fill = _fill(COLOR_LABEL_MATCH)
                ws.cell(row, 10).fill = _fill(COLOR_LABEL_MATCH)
            else:
                if pr.human and pr.human.start_label:
                    ws.cell(row, 3).fill = _fill(COLOR_DIFF_RED)
                    ws.cell(row, 10).fill = _fill(COLOR_DIFF_RED)
            # J列匹配 -> D列和K列绿色
            if pr.j_consistent == 'Y':
                ws.cell(row, 4).fill = _fill(COLOR_LABEL_MATCH)
                ws.cell(row, 11).fill = _fill(COLOR_LABEL_MATCH)
            else:
                if pr.human and pr.human.end_label:
                    ws.cell(row, 4).fill = _fill(COLOR_DIFF_RED)
                    ws.cell(row, 11).fill = _fill(COLOR_DIFF_RED)
            # N列 Trace 有效 -> 浅绿
            if pr.auto and pr.auto.trace_match_status == 'matched':
                ws.cell(row, 14).fill = _fill(COLOR_TRACE_GREEN)
            # 黄色复核
            if pr.auto and pr.auto.review_reason_code:
                ws.cell(row, 15).fill = _fill(COLOR_REVIEW_YELLOW)
        elif pr.pair_type == 'human_only':
            # 人工侧浅红
            for c in range(1, 8):
                ws.cell(row, c).fill = _fill(COLOR_DIFF_RED)
        elif pr.pair_type == 'auto_only':
            # 程序侧黄色
            for c in range(8, 15):
                ws.cell(row, c).fill = _fill(COLOR_EXTRA_YELLOW)

    def _build_stats_sheet(self, wb: Workbook):
        """Sheet2: 统计总览。"""
        ws = wb.create_sheet('统计总览')
        ws.cell(1, 1, f'{self.display_name} 步序标签对比统计').font = FONT_BOLD

        # 统计
        paired = [p for p in self.pair_results if p.pair_type == 'paired']
        human_only = [p for p in self.pair_results if p.pair_type == 'human_only']
        auto_only = [p for p in self.pair_results if p.pair_type == 'auto_only']

        i_yes = sum(1 for p in paired if p.i_consistent == 'Y')
        i_no = sum(1 for p in paired if p.i_consistent == 'N')
        j_yes = sum(1 for p in paired if p.j_consistent == 'Y')
        j_no = sum(1 for p in paired if p.j_consistent == 'N')

        trace_matched = sum(1 for p in self.pair_results
                            if p.auto and p.auto.trace_match_status == 'matched')
        trace_total = len(self.auto_actions)

        yellow = sum(1 for p in self.pair_results
                     if p.auto and p.auto.review_reason_code)

        # 步序覆盖
        stats = [
            ('', '', ''),
            ('步序覆盖', '', ''),
            ('人工唯一动作数', len(self.human_actions), '人工表中提取的动作行数'),
            ('自动主表动作数', len(self.auto_actions), '程序提取的主表动作数'),
            ('成功配对数', len(paired), '人工与自动成功配对'),
            ('仅人工', len(human_only), '人工有但程序未恢复'),
            ('仅程序', len(auto_only), '程序有但人工无'),
            ('', '', ''),
            ('标签准确性', '', ''),
            ('I列一致', i_yes, f'分母={len(paired)}'),
            ('I列不一致', i_no, f'分母={len(paired)}'),
            ('J列一致', j_yes, f'分母={len(paired)}'),
            ('J列不一致', j_no, f'分母={len(paired)}'),
            ('I/J同时一致', sum(1 for p in paired if p.i_consistent == 'Y' and p.j_consistent == 'Y'),
             f'分母={len(paired)}'),
            ('', '', ''),
            ('Trace计时', '', ''),
            ('总动作数', trace_total, ''),
            ('Trace配对成功', trace_matched, ''),
            ('Trace信号数', self.trace_report.get('signal_count', 0), ''),
            ('Trace文件数', self.trace_report.get('trace_file_count', 0), ''),
            ('版本警告', '是' if self.trace_report.get('version_warning') else '否', ''),
            ('', '', ''),
            ('复核负担', '', ''),
            ('黄色待复核', yellow, ''),
            ('复核原因Top', '', ''),
        ]

        # Top 复核原因
        reason_counts = Counter(p.auto.review_reason_code for p in self.pair_results
                                if p.auto and p.auto.review_reason_code)
        for reason, count in reason_counts.most_common(5):
            stats.append((f'  {reason}', count, ''))

        # 写入
        ws.cell(3, 1, '指标').fill = _fill(COLOR_COL_HEADER)
        ws.cell(3, 2, '数量').fill = _fill(COLOR_COL_HEADER)
        ws.cell(3, 3, '说明').fill = _fill(COLOR_COL_HEADER)
        for c in range(1, 4):
            ws.cell(3, c).font = FONT_WHITE_BOLD
            ws.cell(3, c).alignment = ALIGN_CENTER

        for i, (k, v, desc) in enumerate(stats, 4):
            ws.cell(i, 1, k).font = FONT_BOLD if not k.startswith('  ') else FONT_NORMAL
            ws.cell(i, 2, v)
            ws.cell(i, 3, desc)
            for c in range(1, 4):
                ws.cell(i, c).border = BORDER_THIN

        ws.column_dimensions['A'].width = 28
        ws.column_dimensions['B'].width = 10
        ws.column_dimensions['C'].width = 45

    def _build_reason_sheet(self, wb: Workbook):
        """Sheet3: 不一致原因分析。"""
        ws = wb.create_sheet('不一致原因分析')
        headers = ['工站', 'Thread', '步值', '人工描述', '自动描述',
                   '人工I列', '人工J列', '自动start_tag', '自动end_tag',
                   '状态', '主原因', '次原因', '核查建议', '对照行号']
        for c, h in enumerate(headers, 1):
            cell = ws.cell(1, c, h)
            cell.fill = _fill(COLOR_COL_HEADER)
            cell.font = FONT_WHITE_BOLD
            cell.alignment = ALIGN_CENTER
            cell.border = BORDER_THIN

        row = 2
        for idx, pr in enumerate(self.pair_results, 3):
            if pr.pair_type == 'paired' and pr.status == '完全一致':
                continue  # 只显示有问题的
            a = pr.auto
            h = pr.human
            ws.cell(row, 1, (a.station if a else h.station) if (a or h) else '')
            ws.cell(row, 2, (a.thread if a else h.thread) if (a or h) else '')
            ws.cell(row, 3, (a.step_value if a else h.step_value) if (a or h) else '')
            ws.cell(row, 4, h.action_desc if h else '')
            ws.cell(row, 5, a.description if a else '')
            ws.cell(row, 6, h.start_label if h else '')
            ws.cell(row, 7, h.end_label if h else '')
            ws.cell(row, 8, a.start_label if a else '')
            ws.cell(row, 9, a.end_label if a else '')
            ws.cell(row, 10, pr.status)
            ws.cell(row, 11, pr.primary_reason)
            ws.cell(row, 12, ' / '.join(pr.secondary_reasons))
            ws.cell(row, 13, pr.review_suggestion)
            ws.cell(row, 14, f'Sheet1!R{idx}')
            for c in range(1, 15):
                ws.cell(row, c).border = BORDER_THIN
                ws.cell(row, c).font = FONT_NORMAL
            # 颜色
            if pr.pair_type == 'human_only':
                ws.cell(row, 10).fill = _fill(COLOR_DIFF_RED)
            elif pr.pair_type == 'auto_only':
                ws.cell(row, 10).fill = _fill(COLOR_REVIEW_YELLOW)
            elif pr.status != '完全一致':
                ws.cell(row, 10).fill = _fill(COLOR_REVIEW_YELLOW)
            row += 1

        for i, w in enumerate([8, 8, 6, 30, 30, 30, 30, 30, 30, 14, 18, 18, 25, 12], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def _build_desc_sheet(self, wb: Workbook):
        """Sheet4: 动作描述核查。"""
        ws = wb.create_sheet('动作描述核查')
        headers = ['工站', 'Step', '人工描述', '自动描述', 'PLC注释',
                   'PLC命令', '执行机构匹配', '操作匹配', '目标匹配',
                   '语义差异类型', '证据强度', '建议复核内容']
        for c, h in enumerate(headers, 1):
            cell = ws.cell(1, c, h)
            cell.fill = _fill(COLOR_COL_HEADER)
            cell.font = FONT_WHITE_BOLD
            cell.alignment = ALIGN_CENTER
            cell.border = BORDER_THIN

        row = 2
        for pr in self.pair_results:
            if pr.pair_type != 'paired':
                continue
            h = pr.human
            a = pr.auto

            # 简单语义拆分
            h_actor, h_op, h_target = self._split_desc(h.action_desc)
            a_actor, a_op, a_target = self._split_desc(a.description)

            actor_match = '待核' if not h_actor or not a_actor else ('Y' if h_actor == a_actor or a_actor in h_actor or h_actor in a_actor else 'N')
            op_match = '待核' if not h_op or not a_op else ('Y' if h_op == a_op else 'N')
            target_match = '待核' if not h_target or not a_target else ('Y' if h_target == a_target else 'N')

            diff_type = ''
            if actor_match == 'N':
                diff_type = '执行机构错误'
            elif op_match == 'N':
                diff_type = '操作错误'
            elif target_match == 'N':
                diff_type = '目标错误'
            elif actor_match == '待核' or op_match == '待核':
                diff_type = '泛化描述'
            elif h.action_desc and a.description and h.action_desc != a.description:
                diff_type = '描述措辞差异'

            evidence_strength = a.evidence_level or 'inferred'

            ws.cell(row, 1, a.station)
            ws.cell(row, 2, a.step_value)
            ws.cell(row, 3, h.action_desc)
            ws.cell(row, 4, a.description)
            ws.cell(row, 5, a.comment)
            ws.cell(row, 6, ', '.join(a.commands[:3]))
            ws.cell(row, 7, actor_match)
            ws.cell(row, 8, op_match)
            ws.cell(row, 9, target_match)
            ws.cell(row, 10, diff_type)
            ws.cell(row, 11, evidence_strength)
            ws.cell(row, 12, '核查执行机构和操作方向' if diff_type else '')

            for c in range(1, 13):
                ws.cell(row, c).border = BORDER_THIN
                ws.cell(row, c).font = FONT_NORMAL

            if diff_type in ('执行机构错误', '操作错误', '目标错误'):
                ws.cell(row, 10).fill = _fill(COLOR_DIFF_RED)
            elif diff_type:
                ws.cell(row, 10).fill = _fill(COLOR_REVIEW_YELLOW)
            row += 1

        for i, w in enumerate([8, 6, 30, 30, 25, 25, 10, 10, 10, 14, 12, 20], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def _split_desc(self, desc: str) -> tuple:
        """简单拆分描述为 (执行机构, 操作, 目标)。"""
        if not desc:
            return '', '', ''
        # 去除步号前缀
        desc = re.sub(r'^\d+[\.\s\-]+', '', desc)
        # 简单关键词匹配
        actor = ''
        op = ''
        target = ''
        for kw in ['机器人', '气缸', 'Z轴', 'X轴', '伺服', '真空', '电机', '夹爪', '搬运', '移载', '板链']:
            if kw in desc:
                actor = kw
                break
        for kw in ['上升', '下降', '伸出', '回退', '缩回', '夹紧', '张开', '打开', '关闭',
                    '移动', '运动', '启动', '停止', '等待', '定位', '夹取', '放料', '取料']:
            if kw in desc:
                op = kw
                break
        return actor, op, target

    def _build_trace_sheet(self, wb: Workbook):
        """Sheet5: Trace计时核查。"""
        ws = wb.create_sheet('Trace计时核查')

        # 元信息
        ws.cell(1, 1, f'{self.display_name} Trace计时核查').font = FONT_BOLD
        ws.cell(2, 1, f'Trace文件数: {self.trace_report.get("trace_file_count", 0)}')
        ws.cell(3, 1, f'Trace格式: {self.trace_report.get("trace_format", "")}')
        ws.cell(4, 1, f'信号数: {self.trace_report.get("signal_count", 0)}')
        ws.cell(5, 1, f'版本警告: {"是" if self.trace_report.get("version_warning") else "否"}')
        ws.cell(6, 1, f'配对状态: {self.trace_report.get("status_distribution", {})}')

        # 表头
        headers = ['动作ID', '工站', 'Thread', 'Step', '动作描述',
                   '开始标签', '结束标签', 'Trace文件', '原始信号',
                   '周期', '开始时间(ms)', '结束时间(ms)', '时长(ms)',
                   '有效配对次数', '时长统计', 'Trace版本', '结束标签可信度',
                   '配对状态', '复核原因']
        header_row = 8
        for c, h in enumerate(headers, 1):
            cell = ws.cell(header_row, c, h)
            cell.fill = _fill(COLOR_COL_HEADER)
            cell.font = FONT_WHITE_BOLD
            cell.alignment = ALIGN_CENTER
            cell.border = BORDER_THIN

        row = header_row + 1
        trace_files = self.trace_report.get('trace_files', [])
        trace_file_str = '; '.join(Path(f).name for f in trace_files[:3])

        for pr in self.pair_results:
            a = pr.auto
            if not a:
                continue
            # 只显示有 trace 信息的动作
            if a.trace_match_status and a.trace_match_status != 'no_attempt':
                occ_count = len(a.trace_occurrences) if a.trace_occurrences else 0
                durations = [o.get('duration_ms', 0) for o in (a.trace_occurrences or [])]
                if durations:
                    median = sorted(durations)[len(durations) // 2]
                    mean = sum(durations) / len(durations)
                    stats_str = f'中位数={median:.0f}ms, 均值={mean:.0f}ms'
                else:
                    stats_str = ''

                # 结束标签可信度
                end_tag_confidence = '低' if a.review_reason_code else (
                    '高' if a.trace_match_status == 'matched' else '中')

                ws.cell(row, 1, a.action_id)
                ws.cell(row, 2, a.station)
                ws.cell(row, 3, a.thread)
                ws.cell(row, 4, a.step_value)
                ws.cell(row, 5, a.description)
                ws.cell(row, 6, a.start_label)
                ws.cell(row, 7, a.end_label)
                ws.cell(row, 8, trace_file_str)
                ws.cell(row, 9, a.start_label.split('=')[0] if '=' in a.start_label else a.start_label)
                ws.cell(row, 10, a.trace_cycle_id)
                ws.cell(row, 11, a.trace_occurrences[0].get('start_timestamp', '') if a.trace_occurrences else '')
                ws.cell(row, 12, a.trace_occurrences[0].get('end_timestamp', '') if a.trace_occurrences else '')
                ws.cell(row, 13, a.trace_duration_ms if a.trace_duration_ms is not None else '')
                ws.cell(row, 14, occ_count)
                ws.cell(row, 15, stats_str)
                ws.cell(row, 16, 'L8A' if self.trace_report.get('version_warning') else '匹配')
                ws.cell(row, 17, end_tag_confidence)
                ws.cell(row, 18, a.trace_match_status)
                ws.cell(row, 19, a.review_reason_code)

                for c in range(1, 20):
                    ws.cell(row, c).border = BORDER_THIN
                    ws.cell(row, c).font = FONT_NORMAL

                if a.trace_match_status == 'matched' and not a.review_reason_code:
                    ws.cell(row, 13).fill = _fill(COLOR_TRACE_GREEN)
                elif a.trace_match_status == 'matched':
                    ws.cell(row, 13).fill = _fill(COLOR_REVIEW_YELLOW)
                else:
                    ws.cell(row, 18).fill = _fill(COLOR_REVIEW_YELLOW)
                row += 1

        for i, w in enumerate([10, 8, 8, 6, 25, 28, 28, 20, 20, 8, 14, 14, 12, 10, 20, 10, 10, 12, 18], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # ==== Manifest ====

    def build_manifest(self) -> dict:
        """建立输入 Manifest。"""
        import hashlib
        manifest = {
            'project': self.project_name,
            'display_name': self.display_name,
            'production_outputs': {},
            'human_table': {},
        }
        for name in ['filtered_actions.json', 'trace_match_report.json', 'end_tag_decisions.json']:
            p = self.work_output_dir / name
            if p.exists():
                with open(p, 'rb') as f:
                    h = hashlib.md5(f.read()).hexdigest()
                manifest['production_outputs'][name] = {
                    'path': str(p),
                    'md5': h,
                    'size': p.stat().st_size,
                }
        if self.manual_table_path:
            p = Path(self.manual_table_path)
            if not p.is_absolute():
                p = self.repo_root / self.manual_table_path
            if p.exists():
                with open(p, 'rb') as f:
                    h = hashlib.md5(f.read()).hexdigest()
                manifest['human_table'] = {
                    'path': str(p),
                    'md5': h,
                    'size': p.stat().st_size,
                }
        return manifest

    # ==== 主入口 ====

    def run(self) -> bool:
        """主入口。"""
        print(f'  [Comparison] === {self.display_name} 对照表生成 ===')
        self.load_production()
        self.load_human_table()
        self.pair_actions()
        self.build_excel()

        manifest = self.build_manifest()
        manifest_path = self.demo_output_dir / 'comparison_manifest.json'
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f'  [Comparison] Manifest: {manifest_path}')
        return True
