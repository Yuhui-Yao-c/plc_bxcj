#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dlib/process_model.py — ProcessModel 数据模型与构建器（v10.38 P3）

将 EventGraph + StepLogicSlices + ProgramStructure + 原生注释 + 工站/模块结构
组织为具有工程意义的工序模型。

ProcessModel 描述一个工序的：
  - 入口（entry）
  - 工艺目的（purpose）
  - 主要执行机构（mechanisms）
  - 正常执行路径（normal_path）
  - 条件支路（branches）
  - 同步关系（syncs）
  - 工序终点（endpoint）
  - 重要异常路径（anomaly_paths）

OpenCode 负责理解并生成初始 ProcessModel，Primitive 验证程序事实。
不直接让 LLM 凭注释编造不存在的工序关系。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from collections import defaultdict


@dataclass
class ProcessEntry:
    """工序入口。"""
    process_id: str
    station: str
    thread_idx: int
    state_variable: str
    entry_step: int
    process_purpose: str = ""
    mechanisms: list = field(default_factory=list)
    source_provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcessPath:
    """一段工序路径。"""
    path_id: str
    process_id: str
    path_type: str
    steps: list = field(default_factory=list)
    branch_from: str = ""
    branch_step: dict = None
    guard: str = ""
    source_provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcessSync:
    """工序内的同步关系。"""
    sync_id: str
    process_id: str
    threads: list = field(default_factory=list)
    sync_type: str = "cross_thread_wait"
    sync_condition: str = ""
    sync_variables: list = field(default_factory=list)
    source_provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcessModel:
    """一个完整工序的模型。

    对应工程师视角的一道工序（如 "机台上料"、"转接片焊接"、"贴胶"）。
    一个项目通常包含多个 ProcessModel。
    """
    project: str
    process_id: str
    entry: ProcessEntry
    purpose: str = ""
    mechanisms: list = field(default_factory=list)
    normal_path: ProcessPath = None
    branches: list = field(default_factory=list)
    syncs: list = field(default_factory=list)
    endpoint_step: int = -1
    endpoint_type: str = "next_process"
    anomaly_paths: list = field(default_factory=list)
    source_provenance: dict = field(default_factory=dict)
    validation_status: str = "pending"

    def to_dict(self) -> dict:
        r = {
            'project': self.project,
            'process_id': self.process_id,
            'entry': self.entry.to_dict() if self.entry else None,
            'purpose': self.purpose,
            'mechanisms': self.mechanisms,
            'normal_path': self.normal_path.to_dict() if self.normal_path else None,
            'branches': [b.to_dict() for b in self.branches],
            'syncs': [s.to_dict() for s in self.syncs],
            'endpoint_step': self.endpoint_step,
            'endpoint_type': self.endpoint_type,
            'anomaly_paths': [a.to_dict() for a in self.anomaly_paths],
            'source_provenance': self.source_provenance,
            'validation_status': self.validation_status,
        }
        return r


class ProcessModelBuilder:
    """从 EventGraph + StepLogicSlices 构建 ProcessModel 列表。

    通用算法：
      1. 按 (station, thread, state_variable) 分组
      2. 在每组内用 transition邻接表找入口点
      3. DFS 枚举正常路径与分支
      4. 检测跨 thread condition（同步关系）
      5. 从注释/描述推断工艺目的与机构
      6. 标注 endpoint（step=0 或下一工序入口）
    """

    def __init__(self, project: str, cfg: dict = None):
        self.project = project
        self.cfg = cfg or {}
        self.max_depth = self.cfg.get('process_model', {}).get('max_depth', 40)
        self.max_paths = self.cfg.get('process_model', {}).get('max_paths', 200)

    def build(self, event_graph: dict, step_logic_slices: list,
              steps_described: list = None,
              thread_to_station_map: dict = None) -> list:
        """构建 ProcessModel 列表。"""
        steps_described = steps_described or []
        thread_to_station_map = thread_to_station_map or {}

        steps = event_graph.get('steps', [])
        transitions = event_graph.get('transitions', [])
        conditions = event_graph.get('conditions', [])
        waits = event_graph.get('waits', [])

        groups = self._groupSteps(steps)
        transition_adj = self._buildTransitionAdjacency(transitions)
        syncs = self._detectSyncs(conditions, waits, groups)
        desc_lookup = self._buildDescriptionLookup(steps_described)

        models = []
        process_counter = 0

        for (station, thread, sv), group_steps in groups.items():
            if thread < 0:
                continue
            # v10.39 修复：Mylar SMC2 解码器不提取工站名，用 thread 作为 fallback
            original_station = station
            if not station and thread_to_station_map:
                station = thread_to_station_map.get(thread, f'T{thread}')
            if not station:
                # SMC2 项目（Mylar）：用 T{thread} 作为虚拟工站名
                if sv.startswith('m_nNextAutoStep'):
                    station = f'T{thread}'
                else:
                    continue
            if sv in ('Init_Steps', 'm_nNextPowerUpStep',
                      'm_nNextAutoStep_Up', 'm_nNextAutoStep_load_Up'):
                continue

            # v10.39 修复：transition_adj 可能用空 station 作 key（原始 step_identity）
            # 需要同时尝试新 station 和原始 station
            adj = transition_adj.get((station, thread, sv), {})
            if not adj and original_station != station:
                adj = transition_adj.get((original_station, thread, sv), {})
            if not adj:
                continue

            entries = self._findEntryPoints(adj)
            if not entries:
                entries = [min(adj.keys())] if adj else []

            for entry_step in sorted(entries):
                pm = self._buildSingleProcess(
                    station, thread, sv, entry_step, adj,
                    group_steps, desc_lookup, syncs, process_counter)
                if pm:
                    models.append(pm)
                    process_counter += 1

        return models

    def _groupSteps(self, steps: list) -> dict:
        groups = defaultdict(list)
        for s in steps:
            key = (s.get('station', ''), s.get('thread_idx', -1),
                   s.get('state_variable', ''))
            groups[key].append(s)
        return dict(groups)

    def _buildTransitionAdjacency(self, transitions: list) -> dict:
        adj = defaultdict(lambda: defaultdict(list))
        for t in transitions:
            si = t.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('state_variable', ''))
            sv = si.get('step_value', -1)
            target = t.get('target_step')
            # v10.39 修复：target_step 可能是 dict {'thread': N, 'value': V} 或纯 int
            if isinstance(target, dict):
                target_thread = target.get('thread', si.get('thread_idx', -1))
                target_value = target.get('value', -1)
            else:
                target_thread = si.get('thread_idx', -1)
                target_value = target
            guard = t.get('boolean_expr', '')
            # guard 可能是 dict (AST) 或 string，统一转为可序列化
            if isinstance(guard, dict):
                guard = json.dumps(guard, ensure_ascii=False)
            elif not isinstance(guard, str):
                guard = str(guard) if guard else ''
            branches = t.get('branches', [])
            adj[key][sv].append({
                'target': target_value,
                'target_thread': target_thread,
                'guard': guard,
                'branches': branches,
                'element_id': t.get('element_id', ''),
                'provenance': t.get('provenance', ''),
            })
        return dict(adj)

    def _detectSyncs(self, conditions: list, waits: list,
                     groups: dict) -> list:
        syncs = []
        for w in waits:
            si = w.get('step_identity', {})
            syncs.append({
                'sync_type': 'cross_thread_wait',
                'station': si.get('station', ''),
                'thread': si.get('thread_idx', -1),
                'source_station': w.get('source_station', ''),
                'variable': w.get('variable', ''),
                'element_id': w.get('element_id', ''),
            })
        cross_thread_conds = self._detectCrossThreadConditions(conditions)
        syncs.extend(cross_thread_conds)
        return syncs

    def _detectCrossThreadConditions(self, conditions: list) -> list:
        syncs = []
        for c in conditions:
            expr = c.get('expression', '') or ''
            operands = c.get('operands', []) or []
            referenced_threads = set()
            for op in operands:
                if isinstance(op, dict):
                    name = op.get('name', '')
                    if 'm_nNextAutoStep[' in name or 'AutoStep[' in name:
                        import re
                        m = re.search(r'\[(\d+)\]', name)
                        if m:
                            referenced_threads.add(int(m.group(1)))
            si = c.get('step_identity', {})
            own_thread = si.get('thread_idx', -1)
            other_threads = referenced_threads - {own_thread}
            if other_threads and len(referenced_threads) >= 2:
                syncs.append({
                    'sync_type': 'compound_condition',
                    'station': si.get('station', ''),
                    'thread': own_thread,
                    'other_threads': sorted(other_threads),
                    'condition_expr': expr,
                    'element_id': c.get('element_id', ''),
                })
        return syncs

    def _buildDescriptionLookup(self, steps_described: list) -> dict:
        lookup = {}
        for s in steps_described:
            si = s.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('step_value', -1))
            lookup[key] = s
        return lookup

    def _findEntryPoints(self, adj: dict) -> list:
        all_targets = set()
        for step_val, targets in adj.items():
            for t in targets:
                if t.get('target') is not None:
                    all_targets.add(t['target'])
        entries = [sv for sv in adj.keys() if sv not in all_targets and sv > 0]
        return entries

    def _buildSingleProcess(self, station: str, thread: int, sv: str,
                            entry_step: int, adj: dict,
                            group_steps: list, desc_lookup: dict,
                            all_syncs: list, process_counter: int) -> ProcessModel:
        process_id = f"P{process_counter:03d}_{station}_T{thread}_{sv}_{entry_step}"

        normal_steps, branch_paths = self._dfsPath(
            entry_step, adj, station, thread, sv, process_id)

        if not normal_steps:
            return None

        entry_desc = desc_lookup.get((station, thread, entry_step), {})
        purpose = self._inferPurpose(entry_desc, station, sv)
        mechanisms = self._inferMechanisms(entry_desc, normal_steps, desc_lookup, station, thread)

        relevant_syncs = [s for s in all_syncs
                         if s.get('station') == station and s.get('thread') == thread]

        endpoint_step = normal_steps[-1].get('step_value', -1) if normal_steps else -1
        endpoint_type = "reset" if endpoint_step == 0 else "next_process"

        entry = ProcessEntry(
            process_id=process_id,
            station=station,
            thread_idx=thread,
            state_variable=sv,
            entry_step=entry_step,
            process_purpose=purpose,
            mechanisms=mechanisms,
            source_provenance={'unit_name': entry_desc.get('unit_name', ''),
                              'comment': entry_desc.get('comment', '')}
        )

        normal_path = ProcessPath(
            path_id=f"{process_id}_main",
            process_id=process_id,
            path_type="main",
            steps=normal_steps,
            source_provenance={'entry_step': entry_step}
        )

        branches = []
        for i, bp in enumerate(branch_paths):
            branches.append(ProcessPath(
                path_id=f"{process_id}_branch{i}",
                process_id=process_id,
                path_type="branch",
                steps=bp['steps'],
                branch_from=f"{process_id}_main",
                branch_step=bp.get('branch_step'),
                guard=bp.get('guard', ''),
                source_provenance={'branch_step_value': bp.get('branch_step', {}).get('step_value', -1)}
            ))

        sync_models = []
        for i, s in enumerate(relevant_syncs):
            sync_models.append(ProcessSync(
                sync_id=f"{process_id}_sync{i}",
                process_id=process_id,
                threads=[s.get('thread', thread)] + s.get('other_threads', []),
                sync_type=s.get('sync_type', 'cross_thread_wait'),
                sync_condition=s.get('condition_expr', s.get('variable', '')),
                sync_variables=[s.get('variable', ''), s.get('source_station', '')],
                source_provenance={'element_id': s.get('element_id', '')}
            ))

        return ProcessModel(
            project=self.project,
            process_id=process_id,
            entry=entry,
            purpose=purpose,
            mechanisms=mechanisms,
            normal_path=normal_path,
            branches=branches,
            syncs=sync_models,
            endpoint_step=endpoint_step,
            endpoint_type=endpoint_type,
            anomaly_paths=[],
            source_provenance={
                'builder': 'ProcessModelBuilder v1.0',
                'slice_count': len(group_steps),
                'transition_count': sum(len(t) for t in adj.values()),
            },
            validation_status="pending"
        )

    def _dfsPath(self, entry: int, adj: dict, station: str,
                 thread: int, sv: str, process_id: str):
        normal_steps = []
        branch_paths = []
        visited = set()

        def _dfs(step_val, path_steps, depth):
            if depth >= self.max_depth or step_val in visited:
                return
            if step_val == 0:
                return
            visited.add(step_val)
            si = {'station': station, 'thread_idx': thread,
                  'state_variable': sv, 'step_value': step_val}
            path_steps.append(si)

            targets = adj.get(step_val, [])
            unique_targets = {}
            for t in targets:
                tv = t.get('target')
                if tv is not None and tv not in unique_targets:
                    unique_targets[tv] = t

            if not unique_targets:
                return

            sorted_targets = sorted(unique_targets.items())
            for i, (tv, t) in enumerate(sorted_targets):
                if i == 0:
                    _dfs(tv, path_steps, depth + 1)
                else:
                    branch_steps = []
                    branch_visited = set(visited)
                    _dfsBranch(tv, adj, branch_steps, branch_visited, depth + 1,
                              {'station': station, 'thread_idx': thread,
                               'state_variable': sv, 'step_value': step_val},
                              t.get('guard', ''))
                    if branch_steps:
                        branch_paths.append({
                            'steps': branch_steps,
                            'branch_step': {'station': station, 'thread_idx': thread,
                                          'state_variable': sv, 'step_value': step_val},
                            'guard': t.get('guard', '')
                        })

        def _dfsBranch(step_val, adj, path_steps, visited, depth, branch_step, guard):
            if depth >= self.max_depth or step_val in visited:
                return
            if step_val == 0:
                return
            visited.add(step_val)
            si = {'station': station, 'thread_idx': thread,
                  'state_variable': sv, 'step_value': step_val}
            path_steps.append(si)
            targets = adj.get(step_val, [])
            unique_targets = {}
            for t in targets:
                tv = t.get('target')
                if tv is not None and tv not in unique_targets:
                    unique_targets[tv] = t
            if not unique_targets:
                return
            for tv, t in sorted(unique_targets.items()):
                _dfsBranch(tv, adj, path_steps, visited, depth + 1, branch_step, guard)

        _dfs(entry, normal_steps, 0)
        return normal_steps, branch_paths

    def _inferPurpose(self, entry_desc: dict, station: str, sv: str) -> str:
        comment = entry_desc.get('comment', '') or ''
        unit_name = entry_desc.get('unit_name', '') or ''
        if comment:
            return comment[:80]
        if unit_name:
            return unit_name
        return f"{station}_{sv}"

    def _inferMechanisms(self, entry_desc: dict, steps: list,
                         desc_lookup: dict, station: str, thread: int) -> list:
        mechanisms = set()
        for s in steps:
            sv = s.get('step_value', -1)
            d = desc_lookup.get((station, thread, sv), {})
            desc = d.get('description', '') or ''
            for kw in ['气缸', '电机', '机械手', '机器人', '夹爪', '真空', '吸盘',
                       '焊接', '压板', '贴胶', '飞拍', 'CCD', 'Z轴', 'X轴', 'R轴']:
                if kw in desc:
                    mechanisms.add(kw)
        return sorted(mechanisms)


def saveProcessModels(models: list, output_path: Path):
    """保存 ProcessModel 列表到 JSON。"""
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump([m.to_dict() for m in models], f, ensure_ascii=False, indent=2)


def loadProcessModels(path: Path) -> list:
    """从 JSON 加载 ProcessModel 列表（字典形式）。"""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)
