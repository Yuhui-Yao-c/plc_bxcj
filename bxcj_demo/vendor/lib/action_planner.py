#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/action_planner.py — Engineering Action Planner

将 EventGraph 事件聚合为具有独立计时意义的工程动作。

核心原则:
  1. 一行 = 一项有明确计时意义的工程动作（不是一个PLC元素）
  2. 协同命令（如多气缸夹紧+真空）聚合为一行
  3. 独立计时目的（如机器人移动 vs 跨工站等待）拆分为多行
  4. 非计时事件（常量、数据赋值、互锁）不输出为行
  5. 完成信号可跨步查找（命令在Step A，反馈在Step B检查）

数据流:
  EventGraph + step_logic_slices + steps_described
  → PathGraph (路径恢复)
  → EngineeringActionPlanner (动作聚合)
  → list[TimingAction]
"""
from __future__ import annotations

import re
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# ============================================================
# Data Models
# ============================================================

@dataclass
class TimingAction:
    """一项具有独立计时意义的工程动作。"""
    action_id: str
    path_id: str           # "A", "B", etc.
    path_type: str         # "main", "branch", "reset", "termination"
    step_identity: dict
    event_seq: int         # display order within step
    action_type: str       # "motion", "wait", "clamp", "vacuum", "transition"
    description: str       # engineer-style Chinese
    start_label: str
    end_label: str
    end_label_source: str  # "completion_direct", "completion_cross_step", "transition", "wait_signal"
    guard: str             # transition guard if applicable
    commands: list         # list of command variable strings
    completions: list      # list of completion variable strings
    evidence_level: str    # "source_explicit", "control_flow_verified", "inferred"
    comment: str           # PLC comment
    branch_info: str       # branch description if at a branch point


@dataclass
class PathSegment:
    """一段工序路径。"""
    path_id: str
    path_type: str         # "main", "branch", "reset", "termination"
    steps: list            # list of step_identity dicts (ordered)
    branch_from: str = ''  # path_id that this branches from
    branch_step: dict = None  # step where branch occurs
    guard: str = ''        # branch condition


# ============================================================
# Path Graph Builder
# ============================================================

class PathGraphBuilder:
    """从 step_logic_slices 恢复工序路径。

    通用算法:
      1. 按 (station, thread, state_variable) 分组
      2. 构建邻接表: step → [(target, guard)]
      3. 找入口点（无入边的最小step）
      4. DFS枚举路径，分支点创建新路径
      5. 有界遍历防循环
    """

    def __init__(self, max_depth=30, max_paths=100):
        self.max_depth = max_depth
        self.max_paths = max_paths

    def build(self, step_logic_slices: list) -> list:
        """构建路径列表。"""
        # Group by (station, thread, state_var)
        groups = defaultdict(list)
        for s in step_logic_slices:
            si = s.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('state_variable', ''))
            groups[key].append(s)

        all_paths = []
        path_counter = 0

        for (station, thread, sv), slices in groups.items():
            if thread < 0:
                continue
            # Normalize: SMC2/Mylar state vars have empty station
            if not station and sv.startswith('m_n'):
                station = 'Station'
            if not station:
                continue
            if sv in ('Init_Steps', 'm_nNextPowerUpStep',
                      'm_nNextAutoStep_Up', 'm_nNextAutoStep_load_Up'):
                continue

            # Build adjacency
            adj = {}
            slice_lookup = {}
            for s in slices:
                si = s.get('step_identity', {})
                step_val = si.get('step_value', -1)
                if step_val < 0:
                    continue
                targets = []
                for t in s.get('transitions', []):
                    ts = t.get('target_step', {})
                    tv = ts.get('value', -1)
                    tt = ts.get('thread', thread)
                    if tv >= 0:
                        targets.append((tt, tv, t))
                adj[step_val] = targets
                slice_lookup[step_val] = s

            if not adj:
                continue

            # Find entry points: steps not targeted by any other step
            all_targets = set()
            for step_val, targets in adj.items():
                for tt, tv, _ in targets:
                    if tt == thread:
                        all_targets.add(tv)

            entries = [sv for sv in adj.keys() if sv not in all_targets]
            if not entries:
                entries = [min(adj.keys())]

            # DFS from each entry
            for entry in sorted(entries):
                paths_from_entry = self._dfs_paths(
                    entry, adj, thread, station, sv,
                    slice_lookup, path_counter)
                all_paths.extend(paths_from_entry)
                path_counter += len(paths_from_entry)

        return all_paths

    def _dfs_paths(self, entry: int, adj: dict, thread: int,
                   station: str, sv: str, slice_lookup: dict,
                   path_counter: int) -> list:
        """DFS枚举从入口开始的所有路径。

        关键修复: 对同一目标的多个条件去重，只按不同目标创建分支。
        """
        paths = []
        path_id_counter = path_counter

        def _dfs(step_val, path_steps, visited, path_id, path_type, branch_from='', branch_step=None, guard='', depth=0):
            if len(paths) >= self.max_paths:
                return
            if depth >= self.max_depth:
                return
            if step_val in visited:
                return
            if step_val == 0:
                # Termination step — end path
                paths.append(PathSegment(
                    path_id=path_id, path_type='termination' if path_type == 'main' else path_type,
                    steps=list(path_steps),
                    branch_from=branch_from,
                    branch_step=branch_step,
                    guard=guard,
                ))
                return

            visited.add(step_val)
            si = {'station': station, 'thread_idx': thread,
                  'state_variable': sv, 'step_value': step_val}
            path_steps.append(si)

            targets = adj.get(step_val, [])

            # CRITICAL FIX: deduplicate targets — same target with multiple
            # conditions is NOT a branch, it's one transition with multiple guards
            same_thread_targets = [(tt, tv, t) for tt, tv, t in targets
                                   if tt == thread]
            unique_targets = {}
            for tt, tv, t in same_thread_targets:
                if tv not in unique_targets:
                    unique_targets[tv] = (tt, tv, t)
            distinct_targets = list(unique_targets.values())

            if not distinct_targets:
                paths.append(PathSegment(
                    path_id=path_id, path_type=path_type,
                    steps=list(path_steps),
                    branch_from=branch_from,
                    branch_step=branch_step,
                    guard=guard,
                ))
            elif len(distinct_targets) == 1:
                tt, tv, t = distinct_targets[0]
                _dfs(tv, path_steps, visited, path_id, path_type,
                     branch_from, branch_step, guard, depth + 1)
            else:
                # Real branch — different targets
                paths.append(PathSegment(
                    path_id=path_id, path_type=path_type,
                    steps=list(path_steps),
                    branch_from=branch_from,
                    branch_step=branch_step,
                    guard=guard,
                ))
                for bi, (tt, tv, t) in enumerate(distinct_targets):
                    new_path_id = '%s_b%d' % (path_id, bi)
                    _dfs(tv, [], set(visited), new_path_id,
                         'branch', path_id, si, '', depth + 1)

            visited.discard(step_val)
            if path_steps:
                path_steps.pop()

        _dfs(entry, [], set(), 'P%d' % path_id_counter, 'main')
        return paths


# ============================================================
# Engineering Action Planner
# ============================================================

# ============================================================
# Default interface spec (can be overridden by config)
# ============================================================

DEFAULT_INTERFACE_SPEC = {
    # Command interface patterns (regex on variable name)
    'command_patterns': [
        r'\.In\.ToPos\[',        # PLCopen robot position command
        r'\.In\.Location',        # PLCopen axis location command
        r'\.In\.Auto_V',          # PLCopen cylinder command
        r'\.In\.Auto_R',          # PLCopen vacuum command
        r'\.In\.ToHome',          # PLCopen home command
        r'\.AbsEx\[',             # InoProShop axis absolute execute
        r'\.AbsExecute\[',        # InoProShop variant
        r'\.Start\b',             # Weld/process start
    ],
    # Feedback interface patterns
    'feedback_patterns': [
        r'\.Out\.PosDone\[',      # PLCopen position done
        r'\.Out\.XR',             # PLCopen cylinder position
        r'\.Out\.XV',             # PLCopen vacuum signal
        r'\.PosOK\[',             # InoProShop position OK
        r'\.InitPos\b',           # InoProShop initial position
        r'\.LimitPos\b',          # InoProShop limit position
        r'\.Done\b',              # Process done
    ],
    # Command → feedback derivation rules (regex match → replacement)
    'feedback_derivation': [
        # (command_pattern, command_group_index, feedback_template)
        (r'\.In\.ToPos\[(\w+)\]', 1, '{actuator}.Out.PosDone[{index}]'),
        (r'\.In\.Location\w*\[(\w+)\]', 1, '{actuator}.Out.PosDone[{index}]'),
        (r'\.In\.Auto_V', 0, '{actuator}.Out.XR'),
        (r'\.In\.Auto_R', 0, '{actuator}.Out.XR'),
        (r'\.AbsEx\[(\w+)\]', 1, '{actuator}.PosOK[{index}]'),
        (r'\.AbsExecute\[(\w+)\]', 1, '{actuator}.PosOK[{index}]'),
        (r'(\w+)\.Start\b', 0, '{actuator}.Done'),
    ],
    # Non-timing variable patterns (filter out)
    'non_timing_patterns': [
        r'^TRUE$', r'^FALSE$',
        r'Cell_Info', r'H_Messages', r'Alarmcode', r'popup',
        r'ContinuousNumberRst', r'Clear',
        r'\.M_Back', r'\.M_Act',
    ],
    # Command grouping keywords (actuator type → group)
    'grouping_rules': {
        'motion': ['Robot', 'Axis', 'SV'],  # servo/robot/axis → motion group
        'clamp': ['Cy_', 'Cy_Clamp', 'Cy_Push', 'CY'],
        'vacuum': ['Va_', 'Va'],
    },
}


class EngineeringActionPlanner:
    """将 EventGraph 事件聚合为工程动作。

    通用性: 命令/反馈识别通过 interface_spec 配置驱动, 非硬编码。
    opencode 分析源码格式后生成 interface_spec, 传入config。
    """

    def __init__(self, event_graph: dict, steps_described: list = None,
                 step_logic_slices: list = None, config: dict = None):
        self.eg = event_graph
        self.steps_described = steps_described or []
        self.slices = step_logic_slices or []
        self.config = config or {}

        # Load interface spec from config, or use default
        self.interface_spec = self.config.get('interface_spec', DEFAULT_INTERFACE_SPEC)

        # Compile patterns
        self._cmd_patterns = [re.compile(p) for p in self.interface_spec['command_patterns']]
        self._fb_patterns = [re.compile(p) for p in self.interface_spec['feedback_patterns']]
        self._non_timing_patterns = [re.compile(p, re.IGNORECASE) for p in self.interface_spec['non_timing_patterns']]

        # Build indices
        self._build_feedback_index()
        self._build_step_lookup()
        self._build_comment_index()
        self._build_fact_index()

    def plan(self) -> list:
        """规划所有工程动作。

        核心修复: 按Step生成动作（每个Step只生成一次），路径仅用于排序和上下文。
        """
        # Build paths for ordering
        path_builder = PathGraphBuilder()
        paths = path_builder.build(self.slices)

        # Build step → path context mapping
        # For each step, find the path it belongs to and the next few steps
        step_to_path = {}
        step_to_path_type = {}
        step_to_next_steps = {}  # step → [next step identities in path]

        for path in paths:
            path_steps = path.steps
            for i, si in enumerate(path_steps):
                key = (si.get('station', ''), si.get('thread_idx', -1),
                       si.get('step_value', -1))
                if key not in step_to_path:
                    step_to_path[key] = path.path_id
                    step_to_path_type[key] = path.path_type
                    # Store next 3 steps for cross-step completion lookup
                    step_to_next_steps[key] = path_steps[i+1:i+4]

        # Generate actions per unique step (NOT per path)
        all_actions = []
        action_counter = 0
        seen_steps = set()

        # Process steps in path order
        for path in paths:
            for step_si in path.steps:
                step_key = (step_si.get('station', ''),
                           step_si.get('thread_idx', -1),
                           step_si.get('step_value', -1))

                if step_key in seen_steps:
                    continue
                seen_steps.add(step_key)

                if step_si.get('step_value', -1) == 0:
                    continue

                path_id = step_to_path.get(step_key, path.path_id)
                path_type = step_to_path_type.get(step_key, path.path_type)
                next_steps = step_to_next_steps.get(step_key, [])

                step_actions = self._plan_step_actions(
                    step_si, path_id, path_type, action_counter, next_steps)
                all_actions.extend(step_actions)
                action_counter += len(step_actions)

        return all_actions

    def _plan_step_actions(self, step_si: dict, path_id: str,
                           path_type: str, counter_start: int,
                           next_steps: list = None) -> list:
        """为单个Step生成工程动作（只调用一次）。"""
        actions = []
        seq = counter_start
        next_steps = next_steps or []

        # Get events for this step
        step_commands = self._get_step_commands(step_si)
        step_waits = self._get_step_waits(step_si)

        # Filter non-timing commands
        timing_cmds = [c for c in step_commands
                       if self._is_timing_command(c)]

        # Group commands
        groups = self._group_commands(timing_cmds)

        # Build actions for each group
        for group in groups:
            completion = self._find_completion(group, step_si, next_steps, 0)
            action = self._build_action(
                group, step_si, completion, path_id, path_type, seq)
            actions.append(action)
            seq += 1

        # Build actions for waits (only cross-station waits with independent timing)
        for wait in step_waits:
            action = self._build_wait_action(wait, step_si, path_id, path_type, seq)
            actions.append(action)
            seq += 1

        # If no timing commands and no waits, create a transition-only action
        if not groups and not step_waits:
            transition = self._get_step_transition(step_si)
            if transition:
                action = self._build_transition_action(
                    step_si, transition, path_id, path_type, seq)
                actions.append(action)
                seq += 1

        return actions

    # ============================================================
    # Index builders
    # ============================================================

    def _build_feedback_index(self):
        """构建反馈变量索引: variable → [step_identity]"""
        self.feedback_index = defaultdict(list)
        for comp in self.eg.get('completions', []):
            v = comp.get('variable', '')
            if v:
                self.feedback_index[v].append(comp.get('step_identity', {}))
        # Also index from transition booleans
        for t in self.eg.get('transitions', []):
            be = t.get('boolean_expr', {})
            for v in self._extract_vars(be):
                si = t.get('step_identity', {})
                self.feedback_index[v].append(si)

    def _build_step_lookup(self):
        """构建步骤查找: (station, thread, step) → slice_data"""
        self.step_lookup = {}
        for s in self.slices:
            si = s.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('step_value', -1))
            self.step_lookup[key] = s

    def _build_comment_index(self):
        """构建注释索引: (station, thread, step) → comment"""
        self.comment_index = {}
        for s in self.slices:
            si = s.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('step_value', -1))
            comment = s.get('comment', '')
            self.comment_index[key] = comment

    def _build_fact_index(self):
        """构建语义事实索引: (station, thread, step) → [facts]"""
        self.fact_index = {}
        self.fact_by_var = {}
        for s in self.steps_described:
            si = s.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('step_value', -1))
            facts = s.get('action_semantic_facts', [])
            self.fact_index[key] = facts
            for f in facts:
                raw = f.get('raw_action', '')
                if raw:
                    self.fact_by_var[(key, raw)] = f

    def _extract_vars(self, be: dict) -> set:
        """从 BooleanExpr 提取所有变量。"""
        result = set()
        if not be:
            return result
        et = be.get('expr_type', '')
        if et == 'atom':
            v = be.get('variable', '')
            if v:
                result.add(v)
            comp = be.get('comparison')
            if comp:
                for o in comp.get('operands', []):
                    if o and not str(o).isdigit():
                        result.add(str(o))
        elif et in ('and', 'or'):
            for c in be.get('children', []):
                result.update(self._extract_vars(c))
        elif et == 'not':
            inner = be.get('operand', {})
            result.update(self._extract_vars(inner))
        return result

    # ============================================================
    # Path-level planning
    # ============================================================

    def _plan_path(self, path: PathSegment, counter_start: int) -> list:
        """规划一条路径上的工程动作。"""
        actions = []
        seq = counter_start

        # Build path step sequence for look-ahead
        path_steps = path.steps

        for i, step_si in enumerate(path_steps):
            step_key = (step_si.get('station', ''),
                        step_si.get('thread_idx', -1),
                        step_si.get('step_value', -1))

            # Skip step 0 (idle)
            if step_si.get('step_value', -1) == 0:
                continue

            # Get events for this step
            step_commands = self._get_step_commands(step_si)
            step_waits = self._get_step_waits(step_si)

            # Filter non-timing commands
            timing_cmds = [c for c in step_commands
                           if self._is_timing_command(c)]

            # Group commands
            groups = self._group_commands(timing_cmds)

            # Build actions for each group
            for group in groups:
                completion = self._find_completion(group, step_si, path_steps, i)
                action = self._build_action(
                    group, step_si, completion, path, seq)
                actions.append(action)
                seq += 1

            # Build actions for waits
            for wait in step_waits:
                action = self._build_wait_action(wait, step_si, path, seq)
                actions.append(action)
                seq += 1

            # If no timing commands and no waits, create a transition-only action
            if not groups and not step_waits:
                transition = self._get_step_transition(step_si)
                if transition:
                    action = self._build_transition_action(
                        step_si, transition, path, seq)
                    actions.append(action)
                    seq += 1

        return actions

    # ============================================================
    # Event accessors
    # ============================================================

    def _get_step_commands(self, step_si: dict) -> list:
        """获取步骤的命令列表。"""
        key = (step_si.get('station', ''),
               step_si.get('thread_idx', -1),
               step_si.get('step_value', -1))
        return [c for c in self.eg.get('commands', [])
                if self._step_match(c.get('step_identity', {}), step_si)]

    def _get_step_waits(self, step_si: dict) -> list:
        """获取步骤的等待事件列表。"""
        return [w for w in self.eg.get('waits', [])
                if self._step_match(w.get('step_identity', {}), step_si)]

    def _get_step_transition(self, step_si: dict) -> dict:
        """获取步骤的主要转移（优先非零目标）。"""
        transitions = [t for t in self.eg.get('transitions', [])
                       if self._step_match(t.get('step_identity', {}), step_si)]
        if not transitions:
            return None
        # Prefer non-zero target (normal flow over reset)
        for t in transitions:
            target = t.get('target_step', {})
            if target.get('value', -1) > 0:
                return t
        return transitions[0]

    def _step_match(self, si1: dict, si2: dict) -> bool:
        return (si1.get('station', '') == si2.get('station', '') and
                si1.get('thread_idx', -1) == si2.get('thread_idx', -1) and
                si1.get('step_value', -1) == si2.get('step_value', -1))

    # ============================================================
    # Command filtering and grouping
    # ============================================================

    def _is_timing_command(self, cmd: dict) -> bool:
        """判断命令是否具有独立计时意义（基于interface_spec配置）。"""
        var = cmd.get('variable', '')
        if not var:
            return False

        # Filter non-timing patterns
        for pat in self._non_timing_patterns:
            if pat.search(var):
                return False

        # Check if variable matches any command pattern
        for pat in self._cmd_patterns:
            if pat.search(var):
                return True

        return False

    def _group_commands(self, commands: list) -> list:
        """将命令分组为协同动作组（基于interface_spec的grouping_rules）。"""
        if not commands:
            return []

        groups = []
        grouped = [False] * len(commands)
        grouping_rules = self.interface_spec.get('grouping_rules', {})

        # Group by actuator type keywords
        for group_name, keywords in grouping_rules.items():
            group_cmds = []
            for i, cmd in enumerate(commands):
                if grouped[i]:
                    continue
                actuator = cmd.get('actuator_path', '')
                for kw in keywords:
                    if kw in actuator:
                        group_cmds.append(cmd)
                        grouped[i] = True
                        break
            if group_cmds:
                groups.append(group_cmds)

        # Remaining commands — each gets its own group
        for i, cmd in enumerate(commands):
            if not grouped[i]:
                groups.append([cmd])

        return groups

    # ============================================================
    # Completion matching (with cross-step look-ahead)
    # ============================================================

    def _find_completion(self, group: list, step_si: dict,
                         path_steps: list, current_idx: int) -> str:
        """查找命令组的完成信号（支持跨步查找）。

        策略:
          1. 对每个命令推导期望的反馈变量，按优先级排序 (ToPos > Location > Cylinder)
          2. 优先跨步查找机器人/主运动命令的完成信号
          3. 其次查找当前步骤的完成信号
          4. 最后在全局反馈索引中查找
        """
        station = step_si.get('station', '')
        thread = step_si.get('thread_idx', -1)
        step_val = step_si.get('step_value', -1)

        # Derive expected feedbacks for each command, ordered by priority
        priority_feedbacks = []
        for cmd in group:
            fb = self._derive_feedback(cmd)
            if fb:
                cmd_type = cmd.get('command_type', '')
                if cmd_type == 'ToPos':
                    priority_feedbacks.insert(0, fb)
                else:
                    priority_feedbacks.append(fb)

        if not priority_feedbacks:
            return ''

        # Strategy 1: Look ahead in path for each feedback (cross-step)
        # path_steps contains the NEXT steps (not including current)
        for fb in priority_feedbacks:
            for ahead in range(0, min(3, len(path_steps))):
                next_si = path_steps[ahead]
                if next_si.get('step_value', -1) == 0:
                    continue
                next_key = (next_si.get('station', ''),
                            next_si.get('thread_idx', -1),
                            next_si.get('step_value', -1))
                next_slice = self.step_lookup.get(next_key, {})
                for t in next_slice.get('transitions', []):
                    be = t.get('boolean_expr', {})
                    trans_vars = self._extract_vars(be)
                    if fb in trans_vars:
                        return fb

        # Strategy 1b: If no path context, search next steps globally
        if not path_steps:
            # Build next-step candidates from adjacency
            for fb in priority_feedbacks:
                # Find all steps that could follow this one
                step_key = (station, thread, step_val)
                step_slice = self.step_lookup.get(step_key, {})
                next_steps = []
                for t in step_slice.get('transitions', []):
                    ts = t.get('target_step', {})
                    tv = ts.get('value', -1)
                    tt = ts.get('thread', thread)
                    if tv > 0 and tt == thread:
                        next_steps.append((station, thread, tv))

                for ns_key in next_steps:
                    ns_slice = self.step_lookup.get(ns_key, {})
                    for t in ns_slice.get('transitions', []):
                        be = t.get('boolean_expr', {})
                        trans_vars = self._extract_vars(be)
                        if fb in trans_vars:
                            return fb

        # Strategy 2: Check current step's transition boolean
        step_key = (station, thread, step_val)
        slice_data = self.step_lookup.get(step_key, {})
        for fb in priority_feedbacks:
            for t in slice_data.get('transitions', []):
                be = t.get('boolean_expr', {})
                trans_vars = self._extract_vars(be)
                if fb in trans_vars:
                    return fb

        # Strategy 3: Global feedback index
        for fb in priority_feedbacks:
            if fb in self.feedback_index:
                return fb

        return ''

    def _derive_feedback(self, cmd: dict) -> str:
        """从命令变量推导期望的反馈变量（基于interface_spec配置）。

        通用规则: 通过 feedback_derivation 配置规则匹配命令变量,
        生成对应的反馈变量表达式。
        """
        var = cmd.get('variable', '')
        actuator = cmd.get('actuator_path', '')
        if not var or not actuator:
            return ''

        for rule in self.interface_spec.get('feedback_derivation', []):
            pattern_str, group_idx, template = rule
            m = re.search(pattern_str, var)
            if m:
                index = m.group(group_idx) if group_idx > 0 and m.lastindex and group_idx <= m.lastindex else ''
                return template.format(actuator=actuator, index=index)

        return ''

    # ============================================================
    # Action builders
    # ============================================================

    def _build_action(self, group: list, step_si: dict,
                      completion: str, path_id: str, path_type: str,
                      seq: int) -> TimingAction:
        """构建一个工程动作行。"""
        step_key = (step_si.get('station', ''),
                    step_si.get('thread_idx', -1),
                    step_si.get('step_value', -1))

        start_label = self._format_step_label(step_si)

        # Determine end label
        if completion:
            end_label = completion
            end_source = 'completion_cross_step'  # Default to cross-step
            # Check if completion is in current step
            slice_data = self.step_lookup.get(step_key, {})
            for t in slice_data.get('transitions', []):
                be = t.get('boolean_expr', {})
                if completion in self._extract_vars(be):
                    end_source = 'completion_direct'
                    break
        else:
            # Fallback: use transition target
            transition = self._get_step_transition(step_si)
            if transition:
                target = transition.get('target_step', {})
                tv = target.get('value', -1)
                if tv >= 0:
                    end_label = self._format_step_label(
                        {'station': step_si.get('station', ''),
                         'thread_idx': target.get('thread', step_si.get('thread_idx', -1)),
                         'state_variable': step_si.get('state_variable', ''),
                         'step_value': tv})
                    end_source = 'transition'
                else:
                    end_label = 'NOT (%s)' % start_label
                    end_source = 'step_exit'
            else:
                end_label = 'NOT (%s)' % start_label
                end_source = 'step_exit'

        # Determine action type
        action_type = self._classify_action_type(group)

        # Generate description
        description = self._describe_action(group, completion, step_si)

        # Collect command and completion variables
        cmd_vars = [c.get('variable', '') for c in group]

        # Get comment
        comment = self.comment_index.get(step_key, '')

        # Determine evidence level
        if completion:
            ev_level = 'source_explicit'
        else:
            ev_level = 'control_flow_verified'

        # Branch info
        branch_info = ''
        if path_type == 'branch':
            branch_info = '分支路径 %s' % path_id

        return TimingAction(
            action_id='act_%d' % seq,
            path_id=path_id,
            path_type=path_type,
            step_identity=dict(step_si),
            event_seq=seq,
            action_type=action_type,
            description=description,
            start_label=start_label,
            end_label=end_label,
            end_label_source=end_source,
            guard='',
            commands=cmd_vars,
            completions=[completion] if completion else [],
            evidence_level=ev_level,
            comment=comment,
            branch_info=branch_info,
        )

    def _build_wait_action(self, wait: dict, step_si: dict,
                           path_id: str, path_type: str, seq: int) -> TimingAction:
        """构建等待动作行。"""
        start_label = self._format_step_label(step_si)
        end_label = wait.get('variable', '')
        var = wait.get('variable', '')
        source_station = wait.get('source_station', '')

        # Generate description
        parts = var.split('.')
        signal_name = parts[-1] if parts else var
        if '[' in signal_name:
            signal_name = signal_name.split('[')[0]
        if source_station:
            description = '等待%s%s' % (source_station, signal_name)
        else:
            description = '等待%s' % signal_name

        step_key = (step_si.get('station', ''),
                    step_si.get('thread_idx', -1),
                    step_si.get('step_value', -1))
        comment = self.comment_index.get(step_key, '')

        return TimingAction(
            action_id='act_%d' % seq,
            path_id=path_id,
            path_type=path_type,
            step_identity=dict(step_si),
            event_seq=seq,
            action_type='wait',
            description=description,
            start_label=start_label,
            end_label=end_label,
            end_label_source='wait_signal',
            guard='',
            commands=[],
            completions=[],
            evidence_level='control_flow_verified',
            comment=comment,
            branch_info='',
        )

    def _build_transition_action(self, step_si: dict, transition: dict,
                                 path_id: str, path_type: str, seq: int) -> TimingAction:
        """构建纯转移动作行（无命令无等待的步骤）。"""
        start_label = self._format_step_label(step_si)
        target = transition.get('target_step', {})
        tv = target.get('value', -1)
        if tv >= 0:
            end_label = self._format_step_label(
                {'station': step_si.get('station', ''),
                 'thread_idx': target.get('thread', step_si.get('thread_idx', -1)),
                 'state_variable': step_si.get('state_variable', ''),
                 'step_value': tv})
        else:
            end_label = 'NOT (%s)' % start_label

        step_key = (step_si.get('station', ''),
                    step_si.get('thread_idx', -1),
                    step_si.get('step_value', -1))
        comment = self.comment_index.get(step_key, '')

        # Description from comment
        description = self._clean_comment(comment)
        if not description:
            description = '步序转移'

        return TimingAction(
            action_id='act_%d' % seq,
            path_id=path_id,
            path_type=path_type,
            step_identity=dict(step_si),
            event_seq=seq,
            action_type='transition',
            description=description,
            start_label=start_label,
            end_label=end_label,
            end_label_source='transition',
            guard='',
            commands=[],
            completions=[],
            evidence_level='inferred',
            comment=comment,
            branch_info='',
        )

    # ============================================================
    # Description generation
    # ============================================================

    def _describe_action(self, group: list, completion: str,
                         step_si: dict) -> str:
        """生成工程动作中文描述。

        策略:
          1. 优先使用PLC注释（清理后）
          2. 回退到语义事实
          3. 回退到变量结构解析
        """
        step_key = (step_si.get('station', ''),
                    step_si.get('thread_idx', -1),
                    step_si.get('step_value', -1))

        # Strategy 1: Use cleaned comment if it describes the action
        comment = self.comment_index.get(step_key, '')
        cleaned = self._clean_comment(comment)
        if cleaned and not self._is_meta_comment(cleaned):
            return cleaned

        # Strategy 2: Use semantic facts
        parts = []
        for cmd in group:
            var = cmd.get('variable', '')
            fact = self.fact_by_var.get((step_key, var))
            if fact:
                actor = self._extract_fact_value(fact, 'actor')
                operation = self._extract_fact_value(fact, 'operation')
                pos_idx = self._extract_fact_value(fact, 'position_index')
                if actor and actor != 'unknown':
                    parts.append(actor)
                if operation and operation != 'unknown' and operation != '到位反馈信号':
                    parts.append(operation)
                elif cmd.get('command_type') == 'ToPos':
                    parts.append('移动至位置')
                elif cmd.get('command_type') == 'Location_Auto':
                    parts.append('定位至')
                if pos_idx and pos_idx != 'None':
                    parts.append(str(pos_idx))
                break  # Use first command's fact

        if parts:
            return ''.join(parts)

        # Strategy 3: Parse from variable structure
        return self._describe_from_vars(group)

    def _clean_comment(self, comment: str) -> str:
        """清理PLC注释（去除步号前缀）。"""
        if not comment:
            return ''
        text = comment.strip()
        text = re.sub(r'^步?\d+[-.\s：:]*', '', text)
        return text.strip()

    def _is_meta_comment(self, text: str) -> bool:
        """判断注释是否是元信息（非动作描述）。"""
        meta_keywords = ['网络注释', '料盘参数', '参数', '标志', '记忆', '传递']
        for kw in meta_keywords:
            if kw in text:
                return True
        return False

    def _extract_fact_value(self, fact: dict, field_name: str) -> str:
        """从 action_semantic_fact 提取字段值。"""
        field = fact.get(field_name, {})
        if isinstance(field, dict):
            val = field.get('value', '')
            if val and val != 'unknown' and val != 'None':
                return str(val)
        return ''

    def _describe_from_vars(self, group: list) -> str:
        """从变量结构生成描述（回退方案）。"""
        if not group:
            return '(未描述)'

        cmd = group[0]
        actuator = cmd.get('actuator_path', '').split('.')[-1]
        cmd_type = cmd.get('command_type', '')
        index = cmd.get('index', '')

        op_map = {
            'ToPos': '移动至位置',
            'Location_Auto': '定位至',
            'Auto_V': '动作',
            'Auto_R': '打开',
            'ToHome': '回原点',
            'AbsEx': '绝对定位',
        }
        op = op_map.get(cmd_type, cmd_type)

        if actuator and index:
            return '%s%s%s' % (actuator, op, index)
        elif actuator:
            return '%s%s' % (actuator, op)
        return op

    def _classify_action_type(self, group: list) -> str:
        """分类动作类型。"""
        if not group:
            return 'unknown'
        cmd = group[0]
        actuator = cmd.get('actuator_path', '')
        cmd_type = cmd.get('command_type', '')

        if 'Robot' in actuator and cmd_type == 'ToPos':
            return 'motion'
        elif 'Axis' in actuator and cmd_type in ('Location_Auto', 'Location'):
            return 'motion'
        elif 'Cy_' in actuator or 'Cy_Clamp' in actuator:
            return 'clamp'
        elif 'Va_' in actuator:
            return 'vacuum'
        return 'action'

    def _format_step_label(self, si: dict) -> str:
        """格式化步序标签。"""
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
