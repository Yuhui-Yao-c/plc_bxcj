#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dlib/ultrasound_interface_analyzer.py — 超声接口分析与反馈重分类（v10.39 修正）

针对 P2 审计缺口 3：超声 0 completions。

v10.39 关键修正（基于真实 LD 源码验证）：
  - AbsEx（绝对定位开始）是 COMMAND，不是 completion — LD 中为 coil(write)
  - M_Act（自动启动）是 COMMAND，不是 completion — LD 中为 coil(write)
  - M_Back（自动复位）是 COMMAND，不是 completion — LD 中为 coil(write)
  - 仅 PosOK / PosDone / Homed 等在 LD 中为 contact/inVariable(read) 的才是 completion
  - Done/Ready 需检查 LD 读写方向：read=feedback, write=command

本模块：
  1. 接受 LD variable usage map（read/write counts）作为输入
  2. 仅将 read-only 或 read-dominant 变量分类为 completions
  3. write-only 变量保持 command 身份
  4. 不引用后补变量（abEM##ActionStart）

不修改原始 event_graph.json，输出重分类建议供合并。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from collections import defaultdict


# Candidate completion patterns — these are CANDIDATES, not automatic completions.
# Each candidate must be verified against LD read/write direction.
COMPLETION_CANDIDATE_PATTERNS = [
    (re.compile(r'\.Done\[\d+\]'), 'Done', 'done_signal'),
    (re.compile(r'\.Ready\[\d+\]'), 'Ready', 'ready_signal'),
    (re.compile(r'\.PosOK\[\d+\]'), 'PosOK', 'position_reached'),
    (re.compile(r'\.Homed\b'), 'Homed', 'homing_done'),
    (re.compile(r'\.PosDone\b'), 'PosDone', 'position_done'),
    (re.compile(r'\bClamping_Signal\b'), 'Clamping_Signal', 'clamping_status'),
    (re.compile(r'\bAlUW\d+_Done\b'), 'AlUW_Done', 'weld_done'),
]

# COMMAND patterns — these are definitively commands based on name + declaration comment
# AbsEx = "绝对定位开始" (absolute positioning START) = COMMAND
# M_Act = "自动启动" (auto start) = COMMAND
# M_Back = "自动复位" (auto reset) = COMMAND
COMMAND_PATTERNS = [
    (re.compile(r'\.AbsEx\[\d+\]'), 'AbsEx', 'absolute_positioning_start'),
    (re.compile(r'\.M_Act\b'), 'M_Act', 'motor_start'),
    (re.compile(r'\.M_Back\b'), 'M_Back', 'motor_reset'),
    (re.compile(r'\.Start\b'), 'Start', 'start'),
    (re.compile(r'\.WorkStart\[\d+\]'), 'WorkStart', 'work_start'),
    (re.compile(r'\.Auto_Open\b'), 'Auto_Open', 'auto_open'),
    (re.compile(r'\.Auto_Clamp\b'), 'Auto_Clamp', 'auto_clamp'),
    (re.compile(r'\.Auto_Cy_Down\b'), 'Auto_Cy_Down', 'auto_cy_down'),
    (re.compile(r'\.AutoSuck\b'), 'AutoSuck', 'auto_suck'),
    (re.compile(r'\.AutoVacBreak\b'), 'AutoVacBreak', 'auto_vac_break'),
    (re.compile(r'\.Forward\b'), 'Forward', 'forward'),
    (re.compile(r'\.Reset\b'), 'Reset', 'reset'),
    (re.compile(r'\.Stop\b'), 'Stop', 'stop'),
    (re.compile(r'\.Clear\b'), 'Clear', 'clear'),
    (re.compile(r'\.Pop\b'), 'Pop', 'pop'),
    (re.compile(r'\.Strat\b'), 'Strat', 'strat'),
    (re.compile(r'\.BlowExecute\b'), 'BlowExecute', 'blow_execute'),
    (re.compile(r'\.WorkEnd\[\d+\]'), 'WorkEnd', 'work_end_command'),
    (re.compile(r'\.HomeEx\b'), 'HomeEx', 'home_execute'),
]

INTERFACE_DIR_PATTERNS = {
    'In': [re.compile(r'\.In\.'), re.compile(r'\.Done'), re.compile(r'\.Ready'),
           re.compile(r'\.PosOK'), re.compile(r'\.PosDone'), re.compile(r'\.Homed'),
           re.compile(r'\.Clamping_Signal')],
    'Out': [re.compile(r'\.Out\.'), re.compile(r'\.Start'), re.compile(r'\.WorkStart'),
            re.compile(r'\.Auto_'), re.compile(r'\.Reset'), re.compile(r'\.Stop'),
            re.compile(r'\.Clear'), re.compile(r'\.Forward'), re.compile(r'\.BlowExecute'),
            re.compile(r'\.AbsEx'), re.compile(r'\.M_Act'), re.compile(r'\.M_Back'),
            re.compile(r'\.HomeEx'), re.compile(r'\.WorkEnd')],
}

BACKFILL_PATTERN = re.compile(
    r'(ab|at|artg|li)EM\d+(_\d+)?Action(Start|Done)'
)


@dataclass
class ReclassificationSuggestion:
    """变量重分类建议。"""
    variable: str
    original_type: str
    suggested_type: str
    feedback_type: str = ""
    command_type: str = ""
    interface_dir: str = ""
    reason: str = ""
    step_identity: dict = field(default_factory=dict)
    element_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class UltrasoundInterfaceAnalyzer:
    """超声接口分析器。

    v10.39: 将 event_graph.commands 中的反馈信号重分类为 completions。
    必须基于 LD read/write 方向验证，不得仅凭变量名分类。

    AbsEx/M_Act/M_Back 是 COMMAND（LD 中为 coil write），不得分类为 completion。
    PosOK/PosDone/Homed 是 COMPLETION（LD 中为 contact read）。
    Done/Ready 需检查 LD 方向：read=feedback, write=command。
    """

    def __init__(self, ld_variable_usage: dict = None, variable_comments: dict = None):
        """初始化。

        Args:
            ld_variable_usage: {var_name: {"read": int, "write": int}} from LD source scan.
                               If provided, used as primary classification evidence.
            variable_comments: {var_name: comment} from variable declarations.
        """
        self.suggestions = []
        self.ld_usage = ld_variable_usage or {}
        self.var_comments = variable_comments or {}

    def analyze(self, event_graph: dict) -> dict:
        """返回 {
            'reclassified_commands': [...],
            'new_completions': [...],
            'interface_dir_corrections': {...},
            'suggestions': [...]
        }。"""
        commands = event_graph.get('commands', [])
        completions = event_graph.get('completions', [])

        reclassified_cmd_ids = set()
        new_completions = []
        suggestions = []
        interface_corrections = {}

        for cmd in commands:
            var = cmd.get('variable', '') or ''
            if BACKFILL_PATTERN.search(var):
                suggestions.append(ReclassificationSuggestion(
                    variable=var,
                    original_type='command',
                    suggested_type='rejected_backfill',
                    reason='backfill_variable_excluded',
                    step_identity=cmd.get('step_identity', {}),
                    element_id=cmd.get('element_id', ''),
                ))
                reclassified_cmd_ids.add(cmd.get('event_id', ''))
                continue

            # v10.39: Primary classification via LD read/write direction
            ld_class = self._classifyByLdEvidence(var)
            cmd_match = self._matchCommand(var)

            # Only reclassify as completion if LD evidence confirms read-only/read-dominant
            # AND it's not in the COMMAND patterns
            if ld_class == "completion" and not cmd_match:
                completion_match = self._matchCompletionCandidate(var)
                fb_type = completion_match[0] if completion_match else "feedback"
                feedback_kind = completion_match[2] if completion_match else "ld_verified"
                new_comp = {
                    'event_id': f"reclass_{cmd.get('event_id', '')}",
                    'step_identity': cmd.get('step_identity', {}),
                    'variable': var,
                    'actuator_path': cmd.get('actuator_path', ''),
                    'feedback_type': fb_type,
                    'index': cmd.get('index', 0),
                    'element_id': cmd.get('element_id', ''),
                    'matched_command_id': '',
                    'match_confidence': 'ld_read_verified',
                    'source': 'ultrasound_interface_analyzer',
                    'evidence_path': cmd.get('evidence_path', []),
                    'provenance': 'ultrasound_interface_analyzer_v2',
                }
                new_completions.append(new_comp)
                reclassified_cmd_ids.add(cmd.get('event_id', ''))
                suggestions.append(ReclassificationSuggestion(
                    variable=var,
                    original_type='command',
                    suggested_type='completion',
                    feedback_type=fb_type,
                    reason=f'ld_read_verified_{feedback_kind}',
                    step_identity=cmd.get('step_identity', {}),
                    element_id=cmd.get('element_id', ''),
                ))
            elif ld_class == "command" and cmd_match:
                # Confirm it's a command — keep as command, add interface_dir
                pass
            elif ld_class == "command":
                # LD says command but name doesn't match command patterns
                # Keep as command (don't reclassify)
                pass

            iface_dir = self._inferInterfaceDir(var, ld_class)
            if iface_dir and not cmd.get('interface_dir'):
                interface_corrections[cmd.get('event_id', '')] = iface_dir

        remaining_commands = [c for c in commands
                             if c.get('event_id', '') not in reclassified_cmd_ids]

        for cmd_id, iface_dir in interface_corrections.items():
            for c in remaining_commands:
                if c.get('event_id', '') == cmd_id:
                    c['interface_dir'] = iface_dir
                    c['is_interface_cmd'] = True
                    break

        matched_completions = self._matchCommandCompletion(remaining_commands, new_completions)

        return {
            'reclassified_commands': remaining_commands,
            'new_completions': matched_completions,
            'interface_dir_corrections': interface_corrections,
            'suggestions': [s.to_dict() for s in suggestions],
        }

    def _matchCompletionCandidate(self, var: str) -> tuple:
        """Check if variable matches a completion candidate pattern.
        Returns (feedback_type, pattern_name, kind) or None.
        This is a CANDIDATE — must be verified by LD direction."""
        for pattern, fb_type, kind in COMPLETION_CANDIDATE_PATTERNS:
            if pattern.search(var):
                return (fb_type, fb_type, kind)
        return None

    def _matchCommand(self, var: str) -> tuple:
        """Check if variable matches a command pattern.
        Returns (command_type, pattern_name, kind) or None."""
        for pattern, cmd_type, kind in COMMAND_PATTERNS:
            if pattern.search(var):
                return (cmd_type, cmd_type, kind)
        return None

    def _classifyByLdEvidence(self, var: str) -> str:
        """Classify variable using LD read/write direction (primary evidence).

        Returns: "command" / "completion" / "ambiguous" / "unknown"
        """
        # Find LD usage for this variable
        usage = self._findLdUsage(var)

        if not usage:
            # No LD evidence — use comment if available
            comment = self._getVarComment(var)
            if comment:
                if any(kw in comment for kw in ["开始", "启动", "Start", "Execute", "触发", "按钮"]):
                    return "command"
                if any(kw in comment for kw in ["完成", "到位", "Done", "Ready", "结束", "反馈", "状态", "标志"]):
                    return "completion"
            return "unknown"

        read = usage.get("read", 0)
        write = usage.get("write", 0)

        if write > 0 and read == 0:
            # Write-only = command
            return "command"
        if read > 0 and write == 0:
            # Read-only = completion/feedback
            return "completion"
        if read > 0 and write > 0:
            # Read+Write = check comment for dominant semantic
            comment = self._getVarComment(var)
            if comment:
                if any(kw in comment for kw in ["开始", "启动", "Start", "Execute", "触发", "按钮"]):
                    return "command"
                if any(kw in comment for kw in ["完成", "到位", "Done", "Ready", "结束", "反馈", "状态", "标志"]):
                    return "completion"
            return "ambiguous"
        return "unknown"

    def _findLdUsage(self, var: str) -> dict:
        """Find LD usage for a variable, trying multiple name forms."""
        if not self.ld_usage:
            return {}
        # Try exact match
        if var in self.ld_usage:
            return self.ld_usage[var]
        # Try without array index
        base = re.sub(r'\[\d+\]', '', var)
        if base in self.ld_usage:
            return self.ld_usage[base]
        # Try suffix match
        if "." in var:
            last = var.split(".")[-1]
            last_no_idx = re.sub(r'\[\d+\]', '', last)
            for ld_var, u in self.ld_usage.items():
                if ld_var.endswith(last_no_idx) or last_no_idx == ld_var.split(".")[-1] if "." in ld_var else last_no_idx == ld_var:
                    return u
        return {}

    def _getVarComment(self, var: str) -> str:
        """Get declaration comment for a variable."""
        if "." in var:
            last = var.split(".")[-1]
            last_no_idx = re.sub(r'\[\d+\]', '', last)
            return self.var_comments.get(last_no_idx, "")
        return self.var_comments.get(var, "")

    def _inferInterfaceDir(self, var: str, ld_class: str = "") -> str:
        """Infer interface direction from LD classification or name patterns."""
        if ld_class == "command":
            return "Out"
        if ld_class == "completion":
            return "In"
        # Fall back to name patterns
        for direction, patterns in INTERFACE_DIR_PATTERNS.items():
            for p in patterns:
                if p.search(var):
                    return direction
        return ""

    def _matchCommandCompletion(self, commands: list, completions: list) -> list:
        """按 actuator_path 匹配 command-completion。"""
        comp_by_path = defaultdict(list)
        for c in completions:
            comp_by_path[c.get('actuator_path', '')].append(c)

        matched = []
        for cmd in commands:
            path = cmd.get('actuator_path', '')
            path_comps = comp_by_path.get(path, [])
            if path_comps:
                comp = path_comps.pop(0)
                comp['matched_command_id'] = cmd.get('event_id', '')
                matched.append(comp)

        for remaining in comp_by_path.values():
            matched.extend(remaining)

        return matched


def applyReclassification(event_graph: dict, reclass: dict) -> dict:
    """将重分类结果合并到 event_graph。"""
    eg = dict(event_graph)
    eg['commands'] = reclass.get('reclassified_commands', eg.get('commands', []))
    eg['completions'] = list(eg.get('completions', [])) + reclass.get('new_completions', [])
    eg['_ultrasound_reclass_suggestions'] = reclass.get('suggestions', [])
    return eg
