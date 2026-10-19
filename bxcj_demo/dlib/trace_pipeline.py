#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bxcj_demo/lib/trace_pipeline.py — P4 Trace 配对

功能：
  1. 读取 CODESYS XML trace 文件（UTF-16 LE + <TraceVariable> 结构）
  2. 转换为 TraceDataSet
  3. 对每个 filtered_action 的 start_label/end_label 做信号解析与事件配对
  4. 输出 trace_match_report.json

支持的 trace 格式：
  - CODESYS XML (.trace)：含 <TraceVariable VarName=... ><Values>..</Values><Timestamps>..</Timestamps></TraceVariable>
  - CSV：channel_id,timestamp,value

严格的配对条件：
  1. start 与 end 信号都在 trace 中存在
  2. 通道映射有效
  3. 起止属于同一个执行周期（cycle_id 一致）
  4. end 发生在 start 之后
  5. 重复状态进入不产生伪时间
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TraceSignalData:
    """单个 trace 信号。"""
    var_name: str
    values: list           # 状态值序列
    timestamps: list       # ms 时间戳序列


@dataclass
class ActionTraceMatch:
    """单个 action 的 trace 配对结果。"""
    action_id: str
    start_label: str
    end_label: str
    start_signal: str      # 解析后的 trace 信号名
    end_signal: str
    start_signal_resolved: bool
    end_signal_resolved: bool
    trace_start_timestamp: float = None   # ms
    trace_end_timestamp: float = None
    duration_ms: float = None
    trace_cycle_id: str = ''
    match_status: str = 'no_attempt'      # matched | no_signal | no_pair | cycle_violation | version_mismatch
    confidence: str = ''                  # high | medium | low
    uncertainty_reason: str = ''
    all_occurrences: list = field(default_factory=list)  # 所有配对（多次循环）


class CodesysXmlTraceReader:
    """读取 CODESYS XML trace 文件。"""

    def __init__(self, trace_path: Path):
        self.trace_path = trace_path
        self.signals: list[TraceSignalData] = []
        self.format = 'codesys_xml'

    def read(self) -> list[TraceSignalData]:
        with open(self.trace_path, 'rb') as f:
            raw = f.read()
        # 处理 UTF-16 LE BOM
        if raw[:2] == b'\xff\xfe':
            text = raw[2:].decode('utf-16-le', errors='replace')
        elif raw[:3] == b'\xef\xbb\xbf':
            text = raw[3:].decode('utf-8', errors='replace')
        else:
            text = raw.decode('utf-8', errors='replace')

        # 提取所有 TraceVariable 块
        pattern = re.compile(
            r'<TraceVariable\s+VarName="([^"]+)"[^>]*>\s*'
            r'<Values>([^<]*)</Values>\s*'
            r'<Timestamps>([^<]*)</Timestamps>',
            re.DOTALL
        )
        for m in pattern.finditer(text):
            var_name = m.group(1).strip()
            values_str = m.group(2).strip()
            ts_str = m.group(3).strip()
            if not values_str or not ts_str:
                continue
            try:
                values = [float(v) for v in values_str.split(',') if v.strip()]
                timestamps = [float(t) for t in ts_str.split(',') if t.strip()]
                if len(values) != len(timestamps):
                    # 长度不匹配，取较小
                    n = min(len(values), len(timestamps))
                    values = values[:n]
                    timestamps = timestamps[:n]
                self.signals.append(TraceSignalData(
                    var_name=var_name,
                    values=values,
                    timestamps=timestamps
                ))
            except Exception as e:
                print(f'  [TraceReader] 跳过信号 {var_name}: {e}')
        return self.signals


class WeintekCsvTraceReader:
    """读取威纶通(InoProShop)导出的 CSV trace 文件。

    格式：
      - 前 ~21 行为配置头（TraceType,SamplePeriod,...）
      - 第 21 行为列头：Index,Date,ClockTime,RawTime,TraceSampID,<var>:<type>,...
      - 数据行：0,2026-09-04,14:12:07.49,...,510,510,...
    支持多文件合并（同一次采集的 _00001/_00002/_00003 分片）。
    """

    def __init__(self, trace_path: Path):
        self.trace_path = trace_path
        self.format = 'weintek_csv'

    def _parse_clock_time(self, date_str: str, clock_str: str) -> float:
        """将 '2026-09-04' + '14:12:07.49' 转为毫秒时间戳，与TraceSignalData契约一致。"""
        import datetime
        try:
            # 合并日期和时间
            dt = datetime.datetime.strptime(
                f'{date_str} {clock_str}', '%Y-%m-%d %H:%M:%S.%f'
            )
            return dt.timestamp() * 1000.0
        except ValueError:
            try:
                dt = datetime.datetime.strptime(
                    f'{date_str} {clock_str}', '%Y-%m-%d %H:%M:%S'
                )
                return dt.timestamp() * 1000.0
            except ValueError:
                return None

    def read(self) -> list[TraceSignalData]:
        return self._read_single(self.trace_path)

    def read_multiple(self, paths: list) -> list[TraceSignalData]:
        """读取多个 CSV 文件并合并信号。"""
        if not paths:
            return []
        # 用第一个文件初始化
        all_signals = self._read_single(paths[0])
        # 后续文件追加数据
        for p in paths[1:]:
            more = self._read_single(p)
            # 按 var_name 合并
            by_name = {s.var_name: s for s in all_signals}
            for s in more:
                if s.var_name in by_name:
                    by_name[s.var_name].values.extend(s.values)
                    by_name[s.var_name].timestamps.extend(s.timestamps)
                else:
                    all_signals.append(s)
                    by_name[s.var_name] = s
        return all_signals

    def _read_single(self, path) -> list[TraceSignalData]:
        signals = []
        # 尝试多种编码
        for enc in ['gbk', 'utf-8-sig', 'utf-8', 'latin-1']:
            try:
                with open(path, 'r', encoding=enc) as f:
                    lines = f.readlines()
                break
            except UnicodeDecodeError:
                continue
        else:
            print(f'  [WeintekCsv] 无法解码: {path}')
            return []

        # 找列头行（含 Index,Date,ClockTime）
        header_idx = -1
        for i, line in enumerate(lines):
            if 'Index' in line and 'Date' in line and 'ClockTime' in line:
                header_idx = i
                break
        if header_idx < 0:
            print(f'  [WeintekCsv] 未找到列头: {path}')
            return []

        header = lines[header_idx].strip().split(',')
        # 解析信号列（从第 6 列开始，去掉 :INT 等类型后缀）
        sig_names = []
        sig_indices = []
        for ci, col in enumerate(header):
            col = col.strip()
            if col in ('Index', 'Date', 'ClockTime', 'RawTime', 'TraceSampID', ''):
                continue
            # 去掉 :INT/:BOOL 等类型后缀
            var_name = col.split(':')[0]
            sig_names.append(var_name)
            sig_indices.append(ci)

        # 初始化信号
        sig_data = {name: TraceSignalData(var_name=name, values=[], timestamps=[])
                    for name in sig_names}

        # 读数据行
        for line in lines[header_idx + 1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split(',')
            if len(parts) < 5:
                continue
            date_str = parts[1]
            clock_str = parts[2]
            ts = self._parse_clock_time(date_str, clock_str)
            if ts is None:
                continue  # 时间解析失败的样本不能伪装为t=0的有效Trace
            for si, ci in enumerate(sig_indices):
                if ci < len(parts):
                    try:
                        val = float(parts[ci])
                    except ValueError:
                        val = 0.0
                    sig_data[sig_names[si]].values.append(val)
                    sig_data[sig_names[si]].timestamps.append(ts)

        signals = list(sig_data.values())
        print(f'  [WeintekCsv] {path.name}: {len(signals)} 信号, '
              f'{len(signals[0].values) if signals else 0} 采样点')
        return signals


def detect_and_read(trace_path: Path) -> tuple[list[TraceSignalData], str]:
    """自动检测格式并读取单个 trace 文件。"""
    if not trace_path.exists():
        return [], 'not_found'
    # 优先按扩展名判断
    ext = trace_path.suffix.lower()
    if ext == '.csv':
        return WeintekCsvTraceReader(trace_path).read(), 'weintek_csv'
    if ext == '.trace':
        # .trace 通常是 CODESYS XML
        return CodesysXmlTraceReader(trace_path).read(), 'codesys_xml'
    # 无已知扩展名：检测文件头
    with open(trace_path, 'rb') as f:
        head = f.read(4)
    if head[:2] == b'\xff\xfe' or head[:3] == b'\xef\xbb\xbf':
        # UTF-16/UTF-8 BOM：可能是 CODESYS XML 或带 BOM 的 CSV
        # 检查内容是否以 < 开头（XML）
        with open(trace_path, 'rb') as f:
            sniff = f.read(200)
        # 去 BOM 后检查
        if sniff[:2] == b'\xff\xfe':
            text = sniff[2:].decode('utf-16-le', errors='replace')
        elif sniff[:3] == b'\xef\xbb\xbf':
            text = sniff[3:].decode('utf-8', errors='replace')
        else:
            text = sniff.decode('utf-8', errors='replace')
        if text.lstrip().startswith('<'):
            return CodesysXmlTraceReader(trace_path).read(), 'codesys_xml'
        # 带 BOM 的 CSV
        return WeintekCsvTraceReader(trace_path).read(), 'weintek_csv'
    # 默认尝试 XML
    return CodesysXmlTraceReader(trace_path).read(), 'codesys_xml'


def discover_trace_files(trace_path: Path) -> list:
    """发现 trace 文件：如果输入是目录，返回其中所有 trace 文件；
    如果是文件，返回单元素列表。"""
    if not trace_path.exists():
        return []
    if trace_path.is_file():
        return [trace_path]
    # 目录：扫描 .trace 和 .csv 文件
    files = []
    for f in sorted(trace_path.iterdir()):
        if f.suffix.lower() in ('.trace', '.csv'):
            files.append(f)
    return files


class TracePipeline:
    """主入口：加载 trace + filtered_actions，做配对。"""

    def __init__(self, work_output_dir: Path, cfg: dict = None):
        self.work_output_dir = work_output_dir
        self.cfg = cfg or {}
        self.signals: list[TraceSignalData] = []
        self.signal_by_name: dict[str, TraceSignalData] = {}
        self.trace_format = ''
        self.trace_source_file = ''
        self.version_warning = False

    def _resolve_signal(self, label: str) -> tuple[str, bool]:
        """将 start/end 标签解析为 trace 信号名。

        支持的标签格式：
          - ST01.AutoStep[2]=220 → 信号 ST01.AutoStep[2]，期望值 220
          - ST01.AutoStep[2] → 信号 ST01.AutoStep[2]
          - Process_steps[2]=120 → 信号 Process_steps[2]
          - Robot1.Out.PosDone[21] → 信号 Robot1.Out.PosDone[21]（如 trace 中有）
          - NOT (X.Y=120) → 信号 X.Y，期望值非 120
        """
        if not label:
            return '', False
        # 处理 NOT
        negate = False
        clean = label.strip()
        if clean.startswith('NOT (') and clean.endswith(')'):
            negate = True
            clean = clean[5:-1].strip()
        # 提取 signal=value 或 signal
        m = re.match(r'^([\w\.\[\]]+)(?:\s*=\s*(\w+))?$', clean)
        if not m:
            return '', False
        signal_name = m.group(1)
        expected_value = m.group(2)
        # 在 signals 中查找（精确匹配或前缀匹配）
        target = None
        for s in self.signals:
            if s.var_name == signal_name:
                target = s
                break
        if not target:
            # 尝试模糊匹配：去括号
            base = re.sub(r'\[\d+\]', '', signal_name)
            for s in self.signals:
                if s.var_name == signal_name or s.var_name.startswith(base):
                    target = s
                    break
        if not target:
            return signal_name, False
        return target.var_name, True

    def _find_entry_events(self, signal: TraceSignalData,
                           expected_value: float = None,
                           negate: bool = False) -> list[tuple[float, float]]:
        """找信号进入期望值的时刻（false→true 边沿）。

        返回 [(entry_timestamp, exit_timestamp), ...]
        """
        events = []
        if not signal.values:
            return events
        in_state = False
        entry_ts = None
        for i, (v, ts) in enumerate(zip(signal.values, signal.timestamps)):
            if expected_value is not None:
                matches = (abs(v - expected_value) < 0.5) != negate
            else:
                # 无期望值：任何非零值算"成立"
                matches = (v != 0) != negate
            if matches and not in_state:
                in_state = True
                entry_ts = ts
            elif not matches and in_state:
                in_state = False
                events.append((entry_ts, ts))
        # 如果最后还在状态中
        if in_state and entry_ts is not None:
            events.append((entry_ts, signal.timestamps[-1]))
        return events

    def _find_value_entry_events(self, signal: TraceSignalData,
                                  expected_value: float) -> list[tuple[float, float]]:
        """找信号进入特定 step_value 的事件。"""
        return self._find_entry_events(signal, expected_value=expected_value, negate=False)

    def _parse_label_value(self, label: str) -> tuple[str, float, bool]:
        """解析 'ST01.AutoStep[2]=220' → ('ST01.AutoStep[2]', 220.0, False)
        解析 'NOT (ST01.AutoStep[2]=220)' → ('ST01.AutoStep[2]', 220.0, True)
        解析 'm_nNextAutoStep[29]=INT#510' → ('m_nNextAutoStep[29]', 510.0, False)
        """
        negate = False
        clean = label.strip()
        if clean.startswith('NOT (') and clean.endswith(')'):
            negate = True
            clean = clean[5:-1].strip()
        # 支持 INT# 前缀
        clean = re.sub(r'=INT#', '=', clean)
        m = re.match(r'^([\w\.\[\]]+)(?:\s*=\s*(\w+))?$', clean)
        if not m:
            return '', 0.0, negate
        signal_name = m.group(1)
        if m.group(2):
            try:
                return signal_name, float(m.group(2)), negate
            except ValueError:
                return signal_name, 0.0, negate
        return signal_name, 0.0, negate

    def _pair_events_for_action(self, action: dict) -> ActionTraceMatch:
        """为单个 action 做事件配对。"""
        action_id = action.get('action_id', '')
        start_label = action.get('start_label', '')
        end_label = action.get('selected_end_tag', '') or action.get('end_label', '')

        # 解析 start
        start_sig_name, start_val, start_neg = self._parse_label_value(start_label)
        start_signal_resolved = any(s.var_name == start_sig_name for s in self.signals)
        # 解析 end
        end_sig_name, end_val, end_neg = self._parse_label_value(end_label)
        end_signal_resolved = any(s.var_name == end_sig_name for s in self.signals)

        match = ActionTraceMatch(
            action_id=action_id,
            start_label=start_label,
            end_label=end_label,
            start_signal=start_sig_name,
            end_signal=end_sig_name,
            start_signal_resolved=start_signal_resolved,
            end_signal_resolved=end_signal_resolved,
        )

        # 版本警告
        if self.version_warning:
            match.match_status = 'version_mismatch'
            match.confidence = 'low'
            match.uncertainty_reason = 'Trace 与源码版本不匹配（L8A trace vs L7A 源码）'
            # 仍然尝试配对，但标低置信度

        if not start_signal_resolved or not end_signal_resolved:
            match.match_status = 'no_signal'
            match.confidence = 'low'
            missing = []
            if not start_signal_resolved:
                missing.append(f'start={start_sig_name}')
            if not end_signal_resolved:
                missing.append(f'end={end_sig_name}')
            match.uncertainty_reason = f'Trace 中未找到信号: {", ".join(missing)}'
            return match

        # 找信号
        start_signal = next((s for s in self.signals if s.var_name == start_sig_name), None)
        end_signal = next((s for s in self.signals if s.var_name == end_sig_name), None)
        if not start_signal or not end_signal:
            match.match_status = 'no_signal'
            return match

        # 找 start 事件（信号进入期望值）
        if start_val > 0:
            start_events = self._find_entry_events(start_signal, expected_value=start_val, negate=start_neg)
        else:
            start_events = self._find_entry_events(start_signal, negate=start_neg)

        # 找 end 事件
        # v10.36 FIX: 当 end_label 含 NOT（end_neg=True）时，需要找信号退出
        # expected_value 的事件（即变为 NOT expected_value），而不是再次进入
        # expected_value 的事件。之前的 bug 导致 start 和 end 找到完全相同
        # 的事件，所有 duration=0。
        if end_neg:
            # NOT (signal=value): 找信号退出 value 的事件
            end_events = self._find_entry_events(end_signal, expected_value=end_val, negate=True)
        elif start_sig_name == end_sig_name and start_val == end_val and not (start_neg or end_neg):
            # start=end（无NOT）：找下一个状态进入事件
            end_events = self._find_entry_events(end_signal, expected_value=end_val, negate=False)
        elif end_val > 0:
            end_events = self._find_entry_events(end_signal, expected_value=end_val, negate=False)
        else:
            end_events = self._find_entry_events(end_signal)

        if not start_events or not end_events:
            match.match_status = 'no_pair'
            match.uncertainty_reason = '未找到事件边沿'
            return match

        # 配对：每个 start_event 找其后最近的 end_event
        occurrences = []
        for s_entry, s_exit in start_events:
            # 找 s_entry 之后的第一个 end_event
            best_end = None
            for e_entry, e_exit in end_events:
                if e_entry >= s_entry - 1:  # 容忍 1ms 抖动
                    best_end = e_entry
                    break
            if best_end is not None and best_end >= s_entry:
                duration = best_end - s_entry
                if duration >= 0:
                    occurrences.append({
                        'cycle_id': f'c{len(occurrences)+1}',
                        'start_timestamp': s_entry,
                        'end_timestamp': best_end,
                        'duration_ms': duration,
                    })

        if not occurrences:
            match.match_status = 'no_pair'
            match.uncertainty_reason = 'start 后未找到 end 事件'
            return match

        # 取统计：所有 occurrences 的中位数
        durations = [o['duration_ms'] for o in occurrences]
        durations.sort()
        median_ms = durations[len(durations) // 2]
        mean_ms = sum(durations) / len(durations)

        match.trace_start_timestamp = occurrences[0]['start_timestamp']
        match.trace_end_timestamp = occurrences[0]['end_timestamp']
        match.duration_ms = median_ms  # 用中位数作为代表
        match.trace_cycle_id = occurrences[0]['cycle_id']
        match.match_status = 'matched'
        match.confidence = 'high' if len(occurrences) >= 3 else 'medium'
        match.all_occurrences = occurrences
        if self.version_warning:
            match.confidence = 'low'
            match.uncertainty_reason = '版本不匹配，配对结果仅供参考'

        return match

    def run(self) -> bool:
        trace_cfg = self.cfg.get('pipeline', {}).get('trace', {})
        if not trace_cfg.get('enabled', False):
            print('  [TracePipeline] 未启用，写空报告')
            with open(self.work_output_dir / 'trace_match_report.json', 'w',
                      encoding='utf-8') as f:
                json.dump({
                    'enabled': False,
                    'reason': 'trace_not_configured',
                    'matched_actions': 0,
                    'unmatched_actions': 0,
                    'matches': []
                }, f, ensure_ascii=False, indent=2)
            return True

        # 加载 trace 文件（支持文件或目录）
        trace_file = self.cfg.get('project', {}).get('trace_file')
        if not trace_file:
            print('  [TracePipeline] 未配置 trace_file')
            return False

        trace_path = Path(trace_file)
        if not trace_path.is_absolute():
            trace_path = Path(self.cfg.get('_repo_root', '.')) / trace_file

        if not trace_path.exists():
            print(f'  [TracePipeline] trace 路径不存在: {trace_path}')
            return False

        self.version_warning = trace_cfg.get('version_warning', False)

        # 发现 trace 文件（可能是目录含多个 CSV 分片）
        trace_files = discover_trace_files(trace_path)
        if not trace_files:
            print(f'  [TracePipeline] 未发现 trace 文件: {trace_path}')
            return False

        print(f'  [TracePipeline] 发现 {len(trace_files)} 个 trace 文件')

        # 读取所有 trace 文件
        all_signals = []
        self.trace_format = ''
        for tf in trace_files:
            print(f'  [TracePipeline] 加载: {tf.name}')
            sigs, fmt = detect_and_read(tf)
            if sigs:
                if not self.trace_format:
                    self.trace_format = fmt
                # 合并信号（同名信号追加数据）
                existing = {s.var_name: s for s in all_signals}
                for s in sigs:
                    if s.var_name in existing:
                        existing[s.var_name].values.extend(s.values)
                        existing[s.var_name].timestamps.extend(s.timestamps)
                    else:
                        all_signals.append(s)
                        existing[s.var_name] = s

        self.signals = all_signals
        self.trace_source_file = trace_path.name
        self.signal_by_name = {s.var_name: s for s in self.signals}
        print(f'  [TracePipeline] 格式: {self.trace_format}, 信号数: {len(self.signals)}')
        if self.signals:
            print(f'  [TracePipeline] 采样点数(首个信号): {len(self.signals[0].values)}')

        if not self.signals:
            print('  [TracePipeline] 未读到信号')
            with open(self.work_output_dir / 'trace_match_report.json', 'w',
                      encoding='utf-8') as f:
                json.dump({
                    'enabled': True,
                    'trace_files': [str(t) for t in trace_files],
                    'format': self.trace_format,
                    'signal_count': 0,
                    'reason': 'no_signals_parsed',
                    'matched_actions': 0,
                    'unmatched_actions': 0,
                    'matches': []
                }, f, ensure_ascii=False, indent=2)
            return True

        # 加载 filtered_actions
        fa_path = self.work_output_dir / 'filtered_actions.json'
        if not fa_path.exists():
            print(f'  [TracePipeline] filtered_actions.json 不存在')
            return False
        with open(fa_path, 'r', encoding='utf-8') as f:
            fa_data = json.load(f)
        actions = fa_data.get('actions', [])
        actions_in_main = [a for a in actions if a.get('in_main_table', True)]
        print(f'  [TracePipeline] 待配对动作: {len(actions_in_main)}')

        # 配对
        matches = []
        for a in actions_in_main:
            m = self._pair_events_for_action(a)
            matches.append(asdict(m))

        # 统计
        from collections import Counter
        status_counts = Counter(m['match_status'] for m in matches)
        print(f'  [TracePipeline] 配对状态: {dict(status_counts)}')
        matched = status_counts.get('matched', 0)

        # 写报告
        report = {
            'enabled': True,
            'trace_files': [str(t) for t in trace_files],
            'trace_file_count': len(trace_files),
            'trace_format': self.trace_format,
            'signal_count': len(self.signals),
            'signal_names': [s.var_name for s in self.signals[:50]],
            'version_warning': self.version_warning,
            'total_actions': len(matches),
            'matched_actions': matched,
            'unmatched_actions': len(matches) - matched,
            'status_distribution': dict(status_counts),
            'matches': matches,
        }
        out_path = self.work_output_dir / 'trace_match_report.json'
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f'  [TracePipeline] 输出: {out_path}')
        return True
