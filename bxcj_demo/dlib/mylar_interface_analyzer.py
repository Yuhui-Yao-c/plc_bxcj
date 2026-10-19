#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bxcj_demo/lib/mylar_interface_analyzer.py — Mylar SMC2 项目 interface_spec 生成器

SMC2 格式特点：
  - 命令是 FB 调用（AxisMove/AxisHome/twoCyControl 等），不是变量写入
  - 变量命名无 .In./.Out. 约定
  - 高频变量多为内部状态（Workstation_execution_time/m_bJam 等）

策略：
  1. 排除已知噪声变量（execution_time/jam/suspended 等）
  2. 识别真实命令模式（bOpen/bConfirm/JogCW/HMIStrOut/Start/Stop 等）
  3. 识别反馈模式（State/Finish/OK/Done/Alarm 等）
  4. 兜底：未识别变量归为 uncertain，进主表但标黄
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


# 已知噪声变量（高频率但无独立计时意义）
# 注意：不能用 \b\d+\b 因为会匹配数组下标 [2]
NOISE_PATTERNS = [
    r'Workstation_execution_time',
    r'm_bJam\b',
    r'm_bSuspended',
    r'Auto_Threadmedian',
    r'Sequence_Time',
    r'RunTime\b',
    r'InputOverTime',
    r'InputMemory',
    r'ClearString',
    r'ArySHL',
    r'AryMove',
    r'SetBlock',
    r'StatusDisplay',
    r'KeepShellProcess',
    r'PC_CurTem',
    r'PlcToPc_',
    r'PcToPlc_',
    r'HMIAnastole',
    r'Alarmcode',
    r'H_Messages',
    r'Cell_Info',
    r'\bTRUE\b',
    r'\bFALSE\b',
    r'^\d+$',  # 仅整个字符串是数字时才匹配
]

# 真实命令模式（SMC2 中常见的动作命令变量后缀）
# 这些是 FB 实例的输出变量或直接控制变量
COMMAND_PATTERNS = [
    r'\.bOpen\b',
    r'\.bClose\b',
    r'\.bConfirm\b',
    r'\.bStart\b',
    r'\.bStop\b',
    r'\.bEnable\b',
    r'\.bReset\b',
    r'\.JogCW\b',
    r'\.JogCCW\b',
    r'\.HMIStrOut\b',
    r'\.m_bMotorRunstart\b',
    r'\.m_nSaveMotorLoc\b',
    r'\.Trigger\b',
    r'\.Start\b',
    r'\.Stop\b',
    r'\.Enable\b',
    r'\.ToPos\b',
    r'\.AbsEx\b',
    r'\.AbsExecute\b',
    r'\.Auto_V\b',
    r'\.Auto_R\b',
    r'\.ToHome\b',
]

# 反馈模式（状态/完成/到位信号）
FEEDBACK_PATTERNS = [
    r'\.State\d*\b',
    r'\.Finish\d*\b',
    r'\.Done\b',
    r'\.OK\b',
    r'\.Ready\b',
    r'\.PosDone\b',
    r'\.PosOK\b',
    r'\.HaveProduct\b',
    r'\.Alarm\b',
    r'\.Warn\b',
    r'\.bConfirm\b',  # bConfirm 也常作反馈
    r'\.Status\b',
    r'\.InPos\b',
    r'\.InitPos\b',
    r'\.LimitPos\b',
]

# 反馈推导规则：(命令模式, group_idx, 反馈模板)
FEEDBACK_DERIVATION = [
    (r'\.bOpen\b', 0, '{actuator}.bConfirm'),
    (r'\.bClose\b', 0, '{actuator}.bConfirm'),
    (r'\.bStart\b', 0, '{actuator}.State1'),
    (r'\.ToPos\b', 0, '{actuator}.PosDone'),
    (r'\.AbsEx\b', 0, '{actuator}.PosDone'),
    (r'\.ToHome\b', 0, '{actuator}.InitPos'),
]


class MylarInterfaceAnalyzer:
    """分析 Mylar SMC2 源码，生成 interface_spec。"""

    def __init__(self, work_output_dir: Path, cfg: dict = None):
        self.work_output_dir = work_output_dir
        self.cfg = cfg or {}

    def _load_json(self, name: str, default=None):
        p = self.work_output_dir / name
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
        return default if default is not None else {}

    def _is_noise(self, var: str) -> bool:
        """判断是否为噪声变量。"""
        for pat in NOISE_PATTERNS:
            if re.search(pat, var):
                return True
        return False

    def _analyze_variables(self, slices: list, evidence: dict) -> dict:
        """从 slices 中分析变量，识别命令/反馈模式。"""
        # 收集所有动作变量
        action_vars = Counter()
        transition_vars = Counter()
        call_callees = Counter()
        for s in slices:
            for a in s.get('actions', []):
                v = a.get('variable', '')
                if v and not self._is_noise(v):
                    action_vars[v] += 1
            for c in s.get('calls', []):
                callee = c.get('callee', '')
                if callee:
                    call_callees[callee] += 1
            for t in s.get('transitions', []):
                be = t.get('boolean_expr', {})
                atoms = be.get('atoms', []) if isinstance(be, dict) else []
                for atom in atoms:
                    v = atom.get('variable', '') if isinstance(atom, dict) else ''
                    if v and not self._is_noise(v):
                        transition_vars[v] += 1
                raw = be.get('raw', '') if isinstance(be, dict) else ''
                if raw:
                    for m in re.finditer(r'[A-Za-z_][\w\.]*(?:\[\d+\])?', raw):
                        v = m.group(0)
                        if not self._is_noise(v):
                            transition_vars[v] += 1

        # 统计匹配情况
        cmd_matched = set()
        for v in action_vars:
            for pat in COMMAND_PATTERNS:
                if re.search(pat, v):
                    cmd_matched.add(v)
                    break

        fb_matched = set()
        for v in transition_vars:
            for pat in FEEDBACK_PATTERNS:
                if re.search(pat, v):
                    fb_matched.add(v)
                    break

        # 如果标准模式没匹配到足够多的命令，使用兜底模式
        # 兜底：所有非噪声的动作变量都视为命令
        if len(cmd_matched) < 50:
            print(f'  [MylarAnalyzer] 标准模式仅匹配 {len(cmd_matched)} 命令，启用兜底模式')
            # 兜底命令模式：任何 "X.Y" 形式的变量
            COMMAND_PATTERNS_FALLBACK = [r'\b\w+\.\w+\b']
            cmd_patterns_final = COMMAND_PATTERNS + COMMAND_PATTERNS_FALLBACK
        else:
            cmd_patterns_final = COMMAND_PATTERNS

        if len(fb_matched) < 10:
            FEEDBACK_PATTERNS_FALLBACK = [r'\b\w+\.\w+\b']
            fb_patterns_final = FEEDBACK_PATTERNS + FEEDBACK_PATTERNS_FALLBACK
        else:
            fb_patterns_final = FEEDBACK_PATTERNS

        return {
            'command_patterns': cmd_patterns_final,
            'feedback_patterns': fb_patterns_final,
            'feedback_derivation': FEEDBACK_DERIVATION,
            'non_timing_patterns': NOISE_PATTERNS,
            'grouping_rules': {
                'motion': ['Axis', 'Robot', 'SV', 'Motor', 'UnloadRobot'],
                'clamp': ['Cy', 'Clamp', 'Push', 'Grip'],
                'vacuum': ['Va', 'Vacuum'],
            },
            '_analysis_meta': {
                'total_action_vars': len(action_vars),
                'total_transition_vars': len(transition_vars),
                'cmd_matched_by_standard': len(cmd_matched),
                'fb_matched_by_standard': len(fb_matched),
                'top_action_vars': action_vars.most_common(15),
                'top_transition_vars': transition_vars.most_common(15),
                'top_fb_calls': call_callees.most_common(15),
                'noise_patterns_count': len(NOISE_PATTERNS),
            }
        }

    def run(self) -> bool:
        print('  [MylarAnalyzer] 分析 SMC2 源码生成 interface_spec...')
        slices = self._load_json('step_logic_slices.json', [])
        evidence = self._load_json('evidence_store.json', {})

        if not slices:
            print('  [MylarAnalyzer] slices 为空')
            return False

        print(f'  [MylarAnalyzer] 加载 {len(slices)} 个 slices')

        # 分析
        ispec = self._analyze_variables(slices, evidence)

        print(f'  [MylarAnalyzer] 命令模式: {len(ispec["command_patterns"])}')
        print(f'  [MylarAnalyzer] 反馈模式: {len(ispec["feedback_patterns"])}')
        print(f'  [MylarAnalyzer] 标准模式匹配命令: {ispec["_analysis_meta"]["cmd_matched_by_standard"]}')
        print(f'  [MylarAnalyzer] 标准模式匹配反馈: {ispec["_analysis_meta"]["fb_matched_by_standard"]}')
        print(f'  [MylarAnalyzer] Top 命令变量:')
        for v, c in ispec['_analysis_meta']['top_action_vars'][:5]:
            print(f'    {v}: {c}')
        print(f'  [MylarAnalyzer] Top FB 调用:')
        for v, c in ispec['_analysis_meta']['top_fb_calls'][:5]:
            print(f'    {v}: {c}')

        # 写入 extract_config.json
        ec_path = self.work_output_dir / 'extract_config.json'
        if ec_path.exists():
            with open(ec_path, 'r', encoding='utf-8') as f:
                ec = json.load(f)
        else:
            ec = {}

        ec['interface_spec'] = ispec
        with open(ec_path, 'w', encoding='utf-8') as f:
            json.dump(ec, f, ensure_ascii=False, indent=2)
        print(f'  [MylarAnalyzer] 已写入 interface_spec 到 {ec_path.name}')

        # 保存分析报告
        report_path = self.work_output_dir / 'interface_analysis.json'
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(ispec, f, ensure_ascii=False, indent=2)

        return True
