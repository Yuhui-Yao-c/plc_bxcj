#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/event_graph.py — Event Graph: 事件级工程动作关系图

从 StepLogicSlices 提取事件级事实，构建 EventGraph：
  - CommandFact: 执行命令 (actions 中的 .In. 赋值或线圈输出)
  - CompletionEvent: 完成反馈 (transition boolean 中的 .Out. 变量)
  - WaitEvent: 等待/同步信号 (transition boolean 中的跨工站变量)
  - ConditionFact: 条件 (transition boolean 中的比较原子和其他变量)
  - TransitionEdge: 状态转移 (含完整 boolean 和分支结构)
  - EngineeringAction: 工程动作组 (命令 + 反馈 + 描述)

通用原则:
  1. 事件分类基于 PLC 接口约定 (.In. = 命令, .Out. = 反馈) — 非项目专用
  2. 命令-反馈匹配基于实例身份 (同根路径, 不同接口方向) — 非变量名相似度
  3. 分支分类基于内容类型组合 — 非特定变量名
  4. 所有事件保留 evidence path 和 provenance

不含:
  - 项目专用规则 (工站名/步号/动作名)
  - 人工表匹配
  - Excel 输出 (由 event_exporter.py 负责)
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# ============================================================
# Event Schema
# ============================================================

@dataclass
class CommandFact:
    event_id: str
    step_identity: dict
    variable: str
    actuator_path: str       # e.g. "EM01V.Robot1"
    interface_dir: str       # "In" / "Out" / "" (non-interface)
    command_type: str        # e.g. "ToPos", "Location_Auto", ""
    index: str               # e.g. "21", "11", ""
    element_id: str
    evidence_path: list = field(default_factory=list)
    provenance: str = ''     # topology_only / validated_dataflow / ambiguous
    is_interface_cmd: bool = False  # True if .In. pattern matched


@dataclass
class CompletionEvent:
    event_id: str
    step_identity: dict
    variable: str
    actuator_path: str
    feedback_type: str       # e.g. "PosDone", "InPlace", "Cmd_Busy"
    index: str
    element_id: str
    matched_command_id: str = ''
    match_confidence: str = ''  # exact_index / instance_only / unmatched
    source: str = ''         # transition_boolean / cross_step_scan
    evidence_path: list = field(default_factory=list)
    provenance: str = ''


@dataclass
class WaitEvent:
    event_id: str
    step_identity: dict
    variable: str
    source_station: str      # 工站前缀 (不同于当前步的工站)
    element_id: str
    evidence_path: list = field(default_factory=list)
    provenance: str = ''


@dataclass
class ConditionFact:
    event_id: str
    step_identity: dict
    expression: str          # e.g. "Cell_Info[2,0].Quality==0"
    condition_type: str      # comparison / variable / unknown
    variable: str            # extracted variable if any
    operator: str            # EQ/NE/GT etc. for comparisons
    operands: list           # operands for comparisons
    element_id: str
    evidence_path: list = field(default_factory=list)
    provenance: str = ''


@dataclass
class TransitionEdge:
    event_id: str
    step_identity: dict
    target_step: dict        # {thread, value}
    boolean_expr: dict       # full BooleanExpr
    branch_count: int        # number of OR branches
    branches: list           # [{branch_id, conditions: [ConditionFact/CompletionEvent/WaitEvent refs], branch_type}]
    element_id: str
    evidence_path: list = field(default_factory=list)
    provenance: str = ''


@dataclass
class EngineeringAction:
    action_id: str
    step_identity: dict
    event_seq: int           # display order within step
    event_type: str          # command / completion / wait / condition / transition
    command: CommandFact = None
    completion: CompletionEvent = None
    wait: WaitEvent = None
    condition: ConditionFact = None
    transition: TransitionEdge = None
    branch_id: str = ''      # which branch this event belongs to
    parallel_group: int = 0  # parallel action group
    description: str = ''    # engineer-style Chinese (filled by opencode)
    timing_start: str = ''   # start expression
    timing_end: str = ''     # end expression (completion or transition)
    evidence_level: str = '' # source_explicit / control_flow_verified / inferred


@dataclass
class EventGraph:
    steps: list = field(default_factory=list)          # list of step identities
    commands: list = field(default_factory=list)        # CommandFact
    completions: list = field(default_factory=list)     # CompletionEvent
    waits: list = field(default_factory=list)           # WaitEvent
    conditions: list = field(default_factory=list)      # ConditionFact
    transitions: list = field(default_factory=list)     # TransitionEdge
    actions: list = field(default_factory=list)         # EngineeringAction

    def to_dict(self) -> dict:
        return {
            'steps': self.steps,
            'commands': [c.__dict__ for c in self.commands],
            'completions': [c.__dict__ for c in self.completions],
            'waits': [w.__dict__ for w in self.waits],
            'conditions': [c.__dict__ for c in self.conditions],
            'transitions': [t.__dict__ for t in self.transitions],
            'actions': [
                {
                    'action_id': a.action_id,
                    'step_identity': a.step_identity,
                    'event_seq': a.event_seq,
                    'event_type': a.event_type,
                    'command': a.command.__dict__ if a.command else None,
                    'completion': a.completion.__dict__ if a.completion else None,
                    'wait': a.wait.__dict__ if a.wait else None,
                    'condition': a.condition.__dict__ if a.condition else None,
                    'transition': a.transition.__dict__ if a.transition else None,
                    'branch_id': a.branch_id,
                    'parallel_group': a.parallel_group,
                    'description': a.description,
                    'timing_start': a.timing_start,
                    'timing_end': a.timing_end,
                    'evidence_level': a.evidence_level,
                }
                for a in self.actions
            ],
        }


# ============================================================
# Event Graph Builder
# ============================================================

# General PLC interface patterns (not project-specific)
_IN_PATTERN = re.compile(r'\.In\.')
_OUT_PATTERN = re.compile(r'\.Out\.')
_IN_ARRAY_PATTERN = re.compile(r'\.In\.(\w+)\[([^\]]+)\]')
_OUT_ARRAY_PATTERN = re.compile(r'\.Out\.(\w+)\[([^\]]+)\]')
_STATION_PREFIX_RE = re.compile(r'^([A-Z]{2}\d+)V?\.')
_VAR_INDEX_RE = re.compile(r'\[([^\]]+)\]')


class EventGraphBuilder:
    """从 StepLogicSlices 构建事件级 EventGraph。

    通用原则:
      - .In. 模式 = 命令 (写入执行机构输入接口)
      - .Out. 模式 = 反馈 (读取执行机构输出接口)
      - 跨工站前缀 = 等待/同步信号
      - 比较原子 = 条件
      - 命令-反馈匹配 = 同实例路径 + 同索引
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.state_var_names = [sv.get('name', '')
                                for sv in config.get('state_variables', [])]
        if not self.state_var_names:
            single = config.get('step_var_name', '')
            if single:
                self.state_var_names = [single]
        self.state_var_re = (
            re.compile('|'.join(re.escape(v) for v in self.state_var_names))
            if self.state_var_names else None
        )

    def build(self, step_logic_slices: list,
              all_slices_for_scan: list = None) -> EventGraph:
        """构建 EventGraph。

        Args:
            step_logic_slices: 主 StepLogicSlices
            all_slices_for_scan: 所有 slices (用于跨步反馈搜索,
                                 默认与 step_logic_slices 相同)

        Returns:
            EventGraph
        """
        if all_slices_for_scan is None:
            all_slices_for_scan = step_logic_slices

        graph = EventGraph()
        event_counter = 0

        # Build global variable index for cross-step feedback scan
        global_var_index = self._build_global_var_index(all_slices_for_scan)

        for slice_data in step_logic_slices:
            si = slice_data.get('step_identity', {})
            station = si.get('station', '')
            thread = si.get('thread_idx', -1)
            step_val = si.get('step_value', -1)
            state_var = si.get('state_variable', '')

            if thread < 0 or step_val < 0:
                continue
            if state_var in ('Init_Steps', 'm_nNextPowerUpStep',
                             'm_nNextAutoStep_Up', 'm_nNextAutoStep_load_Up'):
                continue

            step_key = (station, thread, step_val)
            graph.steps.append(si)

            # === 1. Extract Commands ===
            commands = self._extract_commands(slice_data, si, event_counter)
            event_counter += len(commands)
            graph.commands.extend(commands)

            # === 2. Extract Transition Events ===
            transitions = self._extract_transitions(
                slice_data, si, event_counter, commands, global_var_index)
            event_counter += len(transitions)
            graph.transitions.extend(transitions)

            # Collect completion/wait/condition from transition booleans
            step_completions = []
            step_waits = []
            step_conditions = []
            for t in transitions:
                step_completions.extend(getattr(t, 'branches_completion', []))
                step_waits.extend(getattr(t, 'branches_waits', []))
                step_conditions.extend(getattr(t, 'branches_conditions', []))
                # Clean up temporary fields
                if hasattr(t, 'branches_completion'):
                    delattr(t, 'branches_completion')
                if hasattr(t, 'branches_waits'):
                    delattr(t, 'branches_waits')
                if hasattr(t, 'branches_conditions'):
                    delattr(t, 'branches_conditions')

            graph.completions.extend(step_completions)
            graph.waits.extend(step_waits)
            graph.conditions.extend(step_conditions)

            # === 3. Build Engineering Actions ===
            actions = self._build_engineering_actions(
                si, commands, transitions, step_completions,
                step_waits, step_conditions, event_counter)
            graph.actions.extend(actions)

        return graph

    # ============================================================
    # Command Extraction
    # ============================================================

    def _extract_commands(self, slice_data: dict, si: dict,
                          counter_start: int) -> list:
        """从 slice 的 actions 提取命令，去重。"""
        seen_vars = set()
        commands = []
        idx = counter_start

        for a in slice_data.get('actions', []):
            var = a.get('variable', '')
            if not var:
                continue
            if var in seen_vars:
                continue
            seen_vars.add(var)

            # Parse interface pattern
            actuator_path, interface_dir, cmd_type, index = (
                self._parse_interface_var(var))

            cmd = CommandFact(
                event_id=f'cmd_{idx}',
                step_identity=dict(si),
                variable=var,
                actuator_path=actuator_path,
                interface_dir=interface_dir,
                command_type=cmd_type,
                index=index,
                element_id=a.get('element_id', ''),
                evidence_path=a.get('evidence_path', []),
                provenance=a.get('provenance', ''),
                is_interface_cmd=(interface_dir == 'In'),
            )
            commands.append(cmd)
            idx += 1

        return commands

    def _parse_interface_var(self, var: str) -> tuple:
        """解析接口变量，返回 (actuator_path, interface_dir, type, index)。

        通用模式:
          EM01V.Robot1.In.ToPos[21]  → ("EM01V.Robot1", "In", "ToPos", "21")
          EM01V.Axis_X2.In.Location_Auto[11] → ("EM01V.Axis_X2", "In", "Location_Auto", "11")
          EM01V.Robot1ToX1 → ("EM01V", "", "Robot1ToX1", "")

        非项目专用：基于 .In. / .Out. 接口约定。
        """
        m = _IN_ARRAY_PATTERN.search(var)
        if m:
            prefix = var[:m.start()]
            return (prefix, 'In', m.group(1), m.group(2))

        m = _OUT_ARRAY_PATTERN.search(var)
        if m:
            prefix = var[:m.start()]
            return (prefix, 'Out', m.group(1), m.group(2))

        if _IN_PATTERN.search(var):
            prefix = var[:_IN_PATTERN.search(var).start()]
            suffix = var[_IN_PATTERN.search(var).end():]
            return (prefix, 'In', suffix, '')

        if _OUT_PATTERN.search(var):
            prefix = var[:_OUT_PATTERN.search(var).start()]
            suffix = var[_OUT_PATTERN.search(var).end():]
            return (prefix, 'Out', suffix, '')

        # Non-interface variable (e.g., internal flag)
        dot_pos = var.rfind('.')
        if dot_pos > 0:
            return (var[:dot_pos], '', var[dot_pos+1:], '')
        return (var, '', '', '')

    # ============================================================
    # Transition Extraction (with branch structure)
    # ============================================================

    def _extract_transitions(self, slice_data: dict, si: dict,
                             counter_start: int,
                             commands: list,
                             global_var_index: dict) -> list:
        """从 slice 的 transitions 提取转移边，解析分支结构。"""
        transitions = []
        idx = counter_start
        station = si.get('station', '')

        for t in slice_data.get('transitions', []):
            target = t.get('target_step', {})
            be = t.get('boolean_expr', {})
            elem_id = t.get('element_id', '')

            # Parse branches from boolean expression
            branches_raw = self._parse_branches(be)

            # Classify each branch and extract events
            branches = []
            all_completions = []
            all_waits = []
            all_conditions = []
            comp_counter = 0
            wait_counter = 0
            cond_counter = 0

            for bi, branch in enumerate(branches_raw):
                branch_events = self._classify_branch_events(
                    branch, si, station, elem_id,
                    t.get('evidence_path', []),
                    t.get('provenance', ''),
                    commands, global_var_index)

                branch_type = self._classify_branch_type(branch_events)

                branches.append({
                    'branch_id': f'b{bi}',
                    'branch_type': branch_type,
                    'conditions': [c.event_id for c in branch_events['conditions']],
                    'completions': [c.event_id for c in branch_events['completions']],
                    'waits': [w.event_id for w in branch_events['waits']],
                    'raw_atoms': branch,
                })

                all_completions.extend(branch_events['completions'])
                all_waits.extend(branch_events['waits'])
                all_conditions.extend(branch_events['conditions'])

            trans = TransitionEdge(
                event_id=f'trans_{idx}',
                step_identity=dict(si),
                target_step=target,
                boolean_expr=be,
                branch_count=len(branches_raw),
                branches=branches,
                element_id=elem_id,
                evidence_path=t.get('evidence_path', []),
                provenance=t.get('provenance', ''),
            )
            # Temporary fields for later collection
            trans.branches_completion = all_completions
            trans.branches_waits = all_waits
            trans.branches_conditions = all_conditions

            transitions.append(trans)
            idx += 1

        return transitions

    def _parse_branches(self, be: dict) -> list:
        """将 BooleanExpr 解析为分支列表 (OR of ANDs)。

        返回: list of list of atoms
          - OR → 多个分支
          - AND → 单分支多条件
          - atom → 单分支单条件
          - unknown → 空分支
        """
        if not be:
            return [[]]

        et = be.get('expr_type', '')
        if et == 'or':
            branches = []
            for child in be.get('children', []):
                branches.extend(self._parse_branches(child))
            return branches if branches else [[]]
        elif et == 'and':
            atoms = []
            for child in be.get('children', []):
                child_branches = self._parse_branches(child)
                if child_branches and child_branches[0]:
                    atoms.extend(child_branches[0])
                elif len(child_branches) > 1:
                    # AND of ORs — flatten to first branch for now
                    # (conservative: don't enumerate cross product)
                    atoms.extend(child_branches[0])
            return [atoms] if atoms else [[]]
        elif et == 'not':
            inner = be.get('operand', be.get('children', [{}])[0] if be.get('children') else {})
            inner_branches = self._parse_branches(inner)
            # Mark atoms as negated
            result = []
            for branch in inner_branches:
                negated_branch = [{**a, 'negated': True} for a in branch]
                result.append(negated_branch)
            return result if result else [[]]
        elif et == 'atom':
            atom = {
                'variable': be.get('variable', ''),
                'negated': be.get('negated', False),
                'comparison': be.get('comparison'),
                'source_element_ids': be.get('source_element_ids', []),
            }
            return [[atom]]
        else:
            return [[]]

    def _classify_branch_events(self, branch_atoms: list, si: dict,
                                station: str, elem_id: str,
                                evidence_path: list, provenance: str,
                                commands: list,
                                global_var_index: dict) -> dict:
        """分类分支中的原子为 completion/wait/condition。"""
        completions = []
        waits = []
        conditions = []
        comp_idx = 0
        wait_idx = 0
        cond_idx = 0

        for atom in branch_atoms:
            var = atom.get('variable', '')
            comp = atom.get('comparison')
            negated = atom.get('negated', False)

            if comp:
                # Comparison atom (e.g., Cell_Info[2,0].Quality == 0)
                expr_parts = []
                op = comp.get('operator', '')
                operands = comp.get('operands', [])
                expr_str = f'{operands[0] if operands else ""} {op} {operands[1] if len(operands) > 1 else ""}'
                cond_var = operands[0] if operands else ''

                cond = ConditionFact(
                    event_id=f'cond_{elem_id}_{cond_idx}',
                    step_identity=dict(si),
                    expression=expr_str.strip(),
                    condition_type='comparison',
                    variable=cond_var,
                    operator=op,
                    operands=operands,
                    element_id=atom.get('source_element_ids', [''])[0] if atom.get('source_element_ids') else elem_id,
                    evidence_path=evidence_path,
                    provenance=provenance,
                )
                conditions.append(cond)
                cond_idx += 1

            elif var:
                # Variable atom
                if self.state_var_re and self.state_var_re.search(var):
                    # State variable — skip (it's the step condition itself)
                    continue

                actuator_path, iface_dir, fb_type, index = (
                    self._parse_interface_var(var))

                if iface_dir == 'Out':
                    # Completion feedback
                    matched_cmd_id = self._match_command(
                        var, actuator_path, index, commands)

                    # Also scan other steps for this feedback variable
                    cross_step_found = ''
                    if not matched_cmd_id:
                        cross_step = self._scan_cross_step_feedback(
                            var, actuator_path, index, global_var_index)
                        if cross_step:
                            cross_step_found = cross_step

                    comp_event = CompletionEvent(
                        event_id=f'comp_{elem_id}_{comp_idx}',
                        step_identity=dict(si),
                        variable=var,
                        actuator_path=actuator_path,
                        feedback_type=fb_type,
                        index=index,
                        element_id=atom.get('source_element_ids', [''])[0] if atom.get('source_element_ids') else elem_id,
                        matched_command_id=matched_cmd_id,
                        match_confidence='exact_index' if matched_cmd_id else (
                            'cross_step_found' if cross_step_found else 'unmatched'),
                        source='transition_boolean' if matched_cmd_id else (
                            'cross_step_scan' if cross_step_found else 'transition_boolean'),
                        evidence_path=evidence_path,
                        provenance=provenance,
                    )
                    completions.append(comp_event)
                    comp_idx += 1

                else:
                    # Check if cross-station (wait event)
                    m = _STATION_PREFIX_RE.match(var)
                    var_station = m.group(1) if m else ''
                    if var_station and var_station != station:
                        wait = WaitEvent(
                            event_id=f'wait_{elem_id}_{wait_idx}',
                            step_identity=dict(si),
                            variable=var,
                            source_station=var_station,
                            element_id=atom.get('source_element_ids', [''])[0] if atom.get('source_element_ids') else elem_id,
                            evidence_path=evidence_path,
                            provenance=provenance,
                        )
                        waits.append(wait)
                        wait_idx += 1
                    else:
                        # Other variable condition
                        cond = ConditionFact(
                            event_id=f'cond_{elem_id}_{cond_idx}',
                            step_identity=dict(si),
                            expression=f'NOT {var}' if negated else var,
                            condition_type='variable',
                            variable=var,
                            operator='',
                            operands=[],
                            element_id=atom.get('source_element_ids', [''])[0] if atom.get('source_element_ids') else elem_id,
                            evidence_path=evidence_path,
                            provenance=provenance,
                        )
                        conditions.append(cond)
                        cond_idx += 1

        return {
            'completions': completions,
            'waits': waits,
            'conditions': conditions,
        }

    def _classify_branch_type(self, branch_events: dict) -> str:
        """分类分支类型 (基于内容类型组合，非特定变量名)。

        类型:
          - normal: 含完成反馈 (可能含等待和条件)
          - bypass: 条件少于其他分支 (快捷/屏蔽路径)
          - sync_only: 仅含等待信号
          - condition_only: 仅含条件 (无反馈)
          - empty: 无事件
        """
        n_comp = len(branch_events['completions'])
        n_wait = len(branch_events['waits'])
        n_cond = len(branch_events['conditions'])

        if n_comp == 0 and n_wait == 0 and n_cond == 0:
            return 'empty'
        if n_comp == 0 and n_wait == 0:
            return 'condition_only'
        if n_comp == 0 and n_wait > 0:
            return 'sync_only'
        # Has completions
        if n_cond > 0 and n_wait == 0 and n_comp > 0:
            # Could be bypass if fewer conditions than a normal branch
            return 'normal_with_condition'
        if n_wait > 0:
            return 'normal_with_sync'
        return 'normal'

    def _match_command(self, feedback_var: str, actuator_path: str,
                       index: str, commands: list) -> str:
        """匹配反馈变量到命令 (同实例 + 同索引)。

        通用规则:
          命令: EM01V.Robot1.In.ToPos[21]
          反馈: EM01V.Robot1.Out.PosDone[21]
          匹配: 同 actuator_path (EM01V.Robot1) + 同 index (21)

        无索引反馈 (如气缸 .Out.XR) 匹配同实例的无索引命令 (如 .In.Auto_V):
          命令: EM02V.Cy_Clamp1.In.Auto_V
          反馈: EM02V.Cy_Clamp1.Out.XR
          匹配: 同 actuator_path (EM02V.Cy_Clamp1)
        """
        if not actuator_path:
            return ''

        # Strategy 1: exact index match (for Axis/Robot FBs)
        if index:
            for cmd in commands:
                if (cmd.actuator_path == actuator_path
                        and cmd.index == index):
                    return cmd.event_id

        # Strategy 2: instance-only match (for Cylinder/Vacuum FBs
        # where command has no index and feedback has no index)
        if not index:
            for cmd in commands:
                if (cmd.actuator_path == actuator_path
                        and not cmd.index
                        and cmd.is_interface_cmd):
                    return cmd.event_id

        # Strategy 3: instance match with any index (fallback)
        for cmd in commands:
            if cmd.actuator_path == actuator_path:
                return cmd.event_id

        return ''

    def _scan_cross_step_feedback(self, feedback_var: str,
                                   actuator_path: str,
                                   index: str,
                                   global_var_index: dict) -> str:
        """扫描其他步骤的命令，查找匹配的反馈变量。

        如果反馈变量在当前步骤无匹配命令，但在其他步骤有同实例的命令，
        则标记为 cross_step_found。
        """
        if not actuator_path:
            return ''

        # Search for commands with same actuator_path in other steps
        cmd_vars = global_var_index.get('cmd_by_actuator', {}).get(actuator_path, [])
        if cmd_vars:
            return f'cross_step:{actuator_path}'

        return ''

    def _build_global_var_index(self, all_slices: list) -> dict:
        """构建全局变量索引 (用于跨步搜索)。"""
        index = {
            'cmd_by_actuator': defaultdict(list),
            'cmd_by_var': defaultdict(list),
            'fb_by_actuator': defaultdict(list),
        }

        for s in all_slices:
            si = s.get('step_identity', {})
            for a in s.get('actions', []):
                var = a.get('variable', '')
                if not var:
                    continue
                actuator_path, iface_dir, _, idx = self._parse_interface_var(var)
                if iface_dir == 'In':
                    index['cmd_by_actuator'][actuator_path].append({
                        'variable': var,
                        'step': si,
                        'index': idx,
                    })
                index['cmd_by_var'][var].append({'step': si})

            # Also index feedback vars from transition booleans
            for t in s.get('transitions', []):
                be = t.get('boolean_expr', {})
                fb_vars = self._extract_vars_from_bool(be)
                for v in fb_vars:
                    actuator_path, iface_dir, _, idx = self._parse_interface_var(v)
                    if iface_dir == 'Out':
                        index['fb_by_actuator'][actuator_path].append({
                            'variable': v,
                            'step': si,
                            'index': idx,
                        })

        return index

    def _extract_vars_from_bool(self, be: dict) -> set:
        """从 BooleanExpr 递归提取所有变量。"""
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
                    if o and not o.isdigit():
                        result.add(o)
        elif et in ('and', 'or'):
            for c in be.get('children', []):
                result.update(self._extract_vars_from_bool(c))
        elif et == 'not':
            inner = be.get('operand', {})
            result.update(self._extract_vars_from_bool(inner))
        return result

    # ============================================================
    # Engineering Action Builder
    # ============================================================

    def _build_engineering_actions(self, si: dict, commands: list,
                                    transitions: list,
                                    step_completions: list,
                                    step_waits: list,
                                    step_conditions: list,
                                    counter_start: int) -> list:
        """构建工程动作组 (一个 Step 可能有多个动作)。

        策略:
          1. 每个命令 → 一个 EngineeringAction (event_type=command)
          2. 如果命令有匹配的完成反馈 → 设置 timing_end
          3. 未匹配反馈的等待事件 → 独立 EngineeringAction (event_type=wait)
          4. 条件 → 独立 EngineeringAction (event_type=condition)
          5. 转移 → 独立 EngineeringAction (event_type=transition)
        """
        actions = []
        seq = 0
        parallel_group = 0

        # Build completion lookup from step completions
        comp_by_cmd = {}
        for comp in step_completions:
            if comp.matched_command_id:
                comp_by_cmd[comp.matched_command_id] = comp

        # Group parallel commands
        parallel_groups = self._group_parallel_commands(commands)

        # 1. Commands with matched completions
        for cmd in commands:
            comp = comp_by_cmd.get(cmd.event_id)
            timing_end = ''
            if comp:
                timing_end = comp.variable
            elif transitions:
                # Use transition target as fallback end
                t = transitions[0]
                target = t.target_step
                if target.get('value', -1) >= 0:
                    sv = si.get('state_variable', '')
                    st = si.get('station', '')
                    th = si.get('thread_idx', -1)
                    if sv == 'Process_steps':
                        timing_end = f'{st}V.{sv}[{th}]={target["value"]}'
                    elif sv == 'm_nNextAutoStep':
                        timing_end = f'{sv}[{th}]=INT#{target["value"]}'
                    elif sv:
                        timing_end = f'{st}V.{sv}[{th}]={target["value"]}'

            timing_start = self._format_step_expr(si)

            ev_level = 'control_flow_verified'
            if cmd.provenance == 'topology_only':
                ev_level = 'control_flow_verified'
            elif cmd.provenance == 'ambiguous':
                ev_level = 'inferred'

            action = EngineeringAction(
                action_id=f'act_{counter_start}_{seq}',
                step_identity=dict(si),
                event_seq=seq,
                event_type='command',
                command=cmd,
                completion=comp,
                parallel_group=parallel_groups.get(cmd.event_id, 0),
                timing_start=timing_start,
                timing_end=timing_end,
                evidence_level=ev_level,
            )
            actions.append(action)
            seq += 1

        # 2. Unmatched wait events
        for wait in step_waits:
            action = EngineeringAction(
                action_id=f'act_{counter_start}_{seq}',
                step_identity=dict(si),
                event_seq=seq,
                event_type='wait',
                wait=wait,
                timing_start=self._format_step_expr(si),
                timing_end=wait.variable,
                evidence_level='control_flow_verified',
            )
            actions.append(action)
            seq += 1

        # 3. Unmatched condition events (not part of a completion match)
        matched_cond_ids = set()
        for comp in step_completions:
            # Conditions in the same branch as a completion are already
            # represented by the completion event
            pass

        for cond in step_conditions:
            if cond.event_id in matched_cond_ids:
                continue
            action = EngineeringAction(
                action_id=f'act_{counter_start}_{seq}',
                step_identity=dict(si),
                event_seq=seq,
                event_type='condition',
                condition=cond,
                timing_start=self._format_step_expr(si),
                timing_end='',
                evidence_level='control_flow_verified',
            )
            actions.append(action)
            seq += 1

        # 4. Transitions
        for t in transitions:
            target = t.target_step
            timing_end = ''
            if target.get('value', -1) >= 0:
                sv = si.get('state_variable', '')
                st = si.get('station', '')
                th = si.get('thread_idx', -1)
                if sv == 'Process_steps':
                    timing_end = f'{st}V.{sv}[{th}]={target["value"]}'
                elif sv == 'm_nNextAutoStep':
                    timing_end = f'{sv}[{th}]=INT#{target["value"]}'
                elif sv:
                    timing_end = f'{st}V.{sv}[{th}]={target["value"]}'

            action = EngineeringAction(
                action_id=f'act_{counter_start}_{seq}',
                step_identity=dict(si),
                event_seq=seq,
                event_type='transition',
                transition=t,
                timing_start=self._format_step_expr(si),
                timing_end=timing_end,
                evidence_level='control_flow_verified',
            )
            actions.append(action)
            seq += 1

        return actions

    def _group_parallel_commands(self, commands: list) -> dict:
        """将命令分组为并行组。

        同一 Step 下的所有命令默认并行执行 (PLC 扫描周期内同时生效)。
        返回 {command_id: group_id}。
        """
        groups = {}
        for i, cmd in enumerate(commands):
            groups[cmd.event_id] = 1  # All in group 1 (parallel)
        return groups

    def _format_step_expr(self, si: dict) -> str:
        """格式化步序表达式。"""
        sv = si.get('state_variable', '')
        st = si.get('station', '')
        th = si.get('thread_idx', -1)
        sv_val = si.get('step_value', -1)
        if sv == 'Process_steps':
            return f'{st}V.{sv}[{th}]={sv_val}'
        elif sv == 'AutoStep':
            return f'{st}.{sv}[{th}]={sv_val}'
        elif sv == 'm_nNextAutoStep':
            return f'{sv}[{th}]=INT#{sv_val}'
        elif sv:
            if st:
                return f'{st}V.{sv}[{th}]={sv_val}'
            return f'{sv}[{th}]=INT#{sv_val}'
        return ''


# ============================================================
# CLI Entry Point
# ============================================================

def build_event_graph_from_slices(slices_path: str,
                                  config: dict = None) -> EventGraph:
    """从 step_logic_slices.json 构建 EventGraph。"""
    import json
    with open(slices_path, 'r', encoding='utf-8') as f:
        slices = json.load(f)

    builder = EventGraphBuilder(config)
    return builder.build(slices)
