#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/step_graph.py — Step Graph Schema

定义 Step / Transition / StepGraph 数据结构。

本轮只定义 Schema 和 serializer，不重写 Step Discovery 算法。
StepGraph 可以从现有 steps_raw.json 转换而来。

Step:
  - identity: (state_variable, station, thread_idx, step_value)
  - domain: state domain (e.g. production_run, initialization)
  - station / thread: 归属工站和线程
  - guards: 条件变量列表
  - actions: 输出赋值变量列表
  - calls: 函数/FB 调用列表
  - timers: 定时器列表
  - source_evidence: 源码证据 (source_file, source_type, expr)
  - comment: PLC 注释
  - st_code: 内嵌 ST 代码片段
  - outgoing_transitions: outgoing Transition 列表

Transition:
  - target: 目标 Step identity
  - condition: 转移条件 (Guard 表达式)
  - branch: 分支类型 (normal, conditional, parallel)
  - evidence: 转移证据来源
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepIdentity:
    """Step 唯一标识 — (state_variable, station, thread_idx, step_value)"""
    state_variable: str = ''
    station: str = ''
    thread_idx: int = 0
    step_value: int = 0

    def to_dict(self) -> dict:
        return {
            'state_variable': self.state_variable,
            'station': self.station,
            'thread_idx': self.thread_idx,
            'step_value': self.step_value,
        }

    def key(self) -> str:
        return f'{self.state_variable}|{self.station}|{self.thread_idx}|{self.step_value}'


@dataclass
class Transition:
    """状态转移 — 从一个 Step 到另一个 Step

    一个逻辑 Edge 可以拥有多条 Evidence。
    condition 为 None 表示无证据支持的转移条件（未解析，不猜测）。
    """
    target: StepIdentity = None
    condition: Any = None     # None = unresolved; str = guard expression
    branch: str = 'normal'       # normal, conditional, parallel
    evidence: dict = field(default_factory=dict)  # {source, detail, confidence}
    evidence_list: list = field(default_factory=list)  # Multiple evidence items
    target_thread: int = -1      # Legacy: target thread index
    target_value: int = 0        # Legacy: target step value
    source: str = ''             # Transition source (move_block, st_code, etc.)

    def to_dict(self) -> dict:
        return {
            'target': self.target.to_dict() if self.target else None,
            'condition': self.condition,
            'branch': self.branch,
            'evidence': self.evidence,
            'evidence_list': self.evidence_list,
            'target_thread': self.target_thread,
            'target_value': self.target_value,
            'source': self.source,
        }


@dataclass
class Step:
    """Step Graph 节点 — 一个步序状态"""
    identity: StepIdentity = None
    domain: str = ''             # State domain (production_run, initialization, etc.)
    station: str = ''
    thread_idx: int = 0
    step_value: int = 0
    state_variable: str = ''

    # Program content
    guards: list = field(default_factory=list)        # [str] — guard variable expressions
    actions: list = field(default_factory=list)        # [str] — action variable names
    calls: list = field(default_factory=list)          # [{name}] — FB/function calls
    timers: list = field(default_factory=list)         # [{name, pt}] — timers
    common_enables: list = field(default_factory=list) # [str] — common enable variables
    st_code: list = field(default_factory=list)        # [str] — embedded ST code snippets
    comment: str = ''

    # Source evidence
    source_file: str = ''
    source_type: str = ''         # graph_comparison, st_code, comment_only
    source_evidence: dict = field(default_factory=dict)

    # Outgoing transitions
    outgoing_transitions: list = field(default_factory=list)  # [Transition]

    def to_dict(self) -> dict:
        return {
            'identity': self.identity.to_dict() if self.identity else None,
            'domain': self.domain,
            'station': self.station,
            'thread_idx': self.thread_idx,
            'step_value': self.step_value,
            'state_variable': self.state_variable,
            'guards': self.guards,
            'actions': self.actions,
            'calls': self.calls,
            'timers': self.timers,
            'common_enables': self.common_enables,
            'st_code': self.st_code,
            'comment': self.comment,
            'source_file': self.source_file,
            'source_type': self.source_type,
            'source_evidence': self.source_evidence,
            'outgoing_transitions': [t.to_dict() for t in self.outgoing_transitions],
        }


@dataclass
class StepGraph:
    """Step Graph — 所有 Step + Transition 的集合

    提供图查询能力:
      - successors/predecessors: 邻接查询
      - reachable_from: 可达性查询
      - path: 路径查找 (BFS)
      - cycles: 环检测
      - dangling_targets: 悬空目标
      - disconnected_components: 连通分量
    """
    steps: list = field(default_factory=list)          # [Step]
    transitions: list = field(default_factory=list)     # [Transition] (global list)
    metadata: dict = field(default_factory=dict)        # {project, format, primitive, ...}

    def to_dict(self) -> dict:
        return {
            'steps': [s.to_dict() for s in self.steps],
            'transitions': [t.to_dict() for t in self.transitions],
            'metadata': self.metadata,
        }

    def save(self, path: str):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def step_count(self) -> int:
        return len(self.steps)

    def transition_count(self) -> int:
        return len(self.transitions)

    def steps_by_domain(self) -> dict:
        domains = {}
        for s in self.steps:
            d = s.domain or 'unknown'
            domains[d] = domains.get(d, 0) + 1
        return domains

    def steps_by_station(self) -> dict:
        stations = {}
        for s in self.steps:
            st = s.station or 'unknown'
            stations[st] = stations.get(st, 0) + 1
        return stations

    # ============================================================
    # Graph query methods
    # ============================================================

    def _step_key(self, step: Step) -> str:
        """Get step identity key."""
        return step.identity.key() if step.identity else f'{step.state_variable}|{step.station}|{step.thread_idx}|{step.step_value}'

    def _build_adjacency(self) -> dict:
        """Build adjacency map: {step_key: {target_key: Transition}}.

        Deduplicates edges: multiple evidence items for the same
        (source, target) pair are merged into one Transition with
        multiple evidence entries.
        """
        adj = {}
        for step in self.steps:
            sk = self._step_key(step)
            if sk not in adj:
                adj[sk] = {}
            for t in step.outgoing_transitions:
                tk = t.target.key() if t.target else f'||{t.target_thread}|{t.target_value}'
                if tk in adj[sk]:
                    # Merge evidence into existing transition
                    existing = adj[sk][tk]
                    if t.source and t.source not in (existing.source, ''):
                        existing.evidence_list.append({
                            'source': t.source,
                            'detail': f'duplicate edge',
                        })
                else:
                    adj[sk][tk] = t
        return adj

    def successors(self, step: Step) -> list:
        """Return list of successor Step objects."""
        adj = self._build_adjacency()
        sk = self._step_key(step)
        step_map = {self._step_key(s): s for s in self.steps}
        result = []
        for tk in adj.get(sk, {}):
            if tk in step_map:
                result.append(step_map[tk])
        return result

    def predecessors(self, step: Step) -> list:
        """Return list of predecessor Step objects."""
        adj = self._build_adjacency()
        tk = self._step_key(step)
        step_map = {self._step_key(s): s for s in self.steps}
        result = []
        for sk, targets in adj.items():
            if tk in targets and sk in step_map:
                result.append(step_map[sk])
        return result

    def reachable_from(self, step: Step) -> set:
        """Return set of step keys reachable from given step (BFS)."""
        adj = self._build_adjacency()
        start = self._step_key(step)
        visited = set()
        queue = [start]
        while queue:
            sk = queue.pop(0)
            if sk in visited:
                continue
            visited.add(sk)
            for tk in adj.get(sk, {}):
                if tk not in visited:
                    queue.append(tk)
        visited.discard(start)
        return visited

    def path(self, step_a: Step, step_b: Step) -> list:
        """Find shortest path from step_a to step_b (BFS).

        Returns list of step keys, or empty list if no path.
        """
        adj = self._build_adjacency()
        start = self._step_key(step_a)
        end = self._step_key(step_b)
        if start == end:
            return [start]
        visited = {start}
        queue = [(start, [start])]
        while queue:
            sk, path = queue.pop(0)
            for tk in adj.get(sk, {}):
                if tk == end:
                    return path + [tk]
                if tk not in visited:
                    visited.add(tk)
                    queue.append((tk, path + [tk]))
        return []

    def cycles(self) -> list:
        """Detect self-loops and simple cycles.

        Returns list of cycle representations. For large graphs,
        only self-loops and 2-cycles are detected (practical limitation).
        """
        adj = self._build_adjacency()
        cycles = []
        # Self-loops
        for sk, targets in adj.items():
            if sk in targets:
                cycles.append([sk])
        # 2-cycles
        for sk, targets in adj.items():
            for tk in targets:
                if tk != sk and tk in adj and sk in adj[tk]:
                    cycle = sorted([sk, tk])
                    if cycle not in cycles:
                        cycles.append(cycle)
        return cycles

    def dangling_targets(self) -> list:
        """Return list of target keys that don't correspond to any Step."""
        adj = self._build_adjacency()
        step_keys = {self._step_key(s) for s in self.steps}
        dangling = []
        for sk, targets in adj.items():
            for tk in targets:
                if tk not in step_keys:
                    dangling.append({'from': sk, 'to': tk})
        return dangling

    def disconnected_components(self) -> list:
        """Return list of connected components (as lists of step keys).

        Uses undirected connectivity (predecessor + successor edges).
        """
        adj = self._build_adjacency()
        # Build undirected adjacency
        undirected = {}
        for sk, targets in adj.items():
            if sk not in undirected:
                undirected[sk] = set()
            for tk in targets:
                undirected[sk].add(tk)
                if tk not in undirected:
                    undirected[tk] = set()
                undirected[tk].add(sk)
        # Ensure all steps are in undirected
        for s in self.steps:
            sk = self._step_key(s)
            if sk not in undirected:
                undirected[sk] = set()

        visited = set()
        components = []
        for sk in undirected:
            if sk in visited:
                continue
            # BFS
            component = []
            queue = [sk]
            while queue:
                node = queue.pop(0)
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                for neighbor in undirected.get(node, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)
            components.append(component)
        return components

    def unique_edge_count(self) -> int:
        """Count unique (source, target) edges (deduplicating evidence)."""
        adj = self._build_adjacency()
        return sum(len(targets) for targets in adj.values())

    def total_evidence_count(self) -> int:
        """Count total transition evidence (sum of evidence_list lengths).

        After edge deduplication, each Transition may have multiple
        evidence items in its evidence_list. This method counts the
        total evidence across all transitions.
        """
        count = 0
        for s in self.steps:
            for t in s.outgoing_transitions:
                if t.evidence_list:
                    count += len(t.evidence_list)
                else:
                    count += 1  # Legacy: each transition is one evidence
        return count


def steps_raw_to_step_graph(steps_raw: list, config: dict = None) -> StepGraph:
    """从现有 steps_raw.json 转换为 StepGraph。

    本函数是 adapter — 不重写 Step Discovery，只做格式转换。

    改进:
      - Transition.condition 默认 None (不猜测，无证据时保持未知)
      - Edge 去重: 同一 (source, target) 对的多条 evidence 合并为一个 Transition
    """
    config = config or {}
    state_domains = config.get('state_domains', [])
    domain_map = {}
    for d in state_domains:
        for sv in d.get('state_variables', []):
            domain_map[sv] = d.get('domain_id', 'unknown')

    graph = StepGraph()
    graph.metadata = {
        'source': 'steps_raw.json',
        'conversion': 'steps_raw_to_step_graph',
        'condition_policy': 'none_when_no_evidence',
        'edge_deduplication': True,
    }

    for s in steps_raw:
        sv = s.get('state_variable', '')
        station = s.get('station', '')
        t = s.get('thread_idx', 0)
        v = s.get('step_value', 0)

        identity = StepIdentity(
            state_variable=sv, station=station,
            thread_idx=t, step_value=v)

        step = Step(
            identity=identity,
            domain=domain_map.get(sv, 'unknown'),
            station=station,
            thread_idx=t,
            step_value=v,
            state_variable=sv,
            guards=s.get('guards', []),
            actions=s.get('actions', []),
            calls=s.get('calls', []),
            timers=s.get('timers', []),
            common_enables=s.get('common_enables', []),
            st_code=s.get('st_code', []),
            comment=s.get('comment', ''),
            source_file=s.get('source_file', ''),
            source_type=s.get('source_type', ''),
            source_evidence=s.get('evidence', {}),
        )

        # Convert transitions with edge deduplication
        # Include the written target state-variable family in edge identity.
        # The source state family is not necessarily the target (cross-thread
        # or cross-state-machine synchronization is common in Mylar).
        edge_map = {}  # (target_state_variable, target_thread, target_value) → Transition
        for t_info in s.get('transitions', []):
            target_thread = t_info.get('target_thread', -1)
            target_value = t_info.get('target_value', 0)
            target_state_variable = t_info.get('target_state_variable') or sv
            edge_key = (target_state_variable, target_thread, target_value)

            if edge_key in edge_map:
                # Merge evidence into existing transition
                existing = edge_map[edge_key]
                src = t_info.get('source', '')
                if src and src not in existing.evidence_list:
                    existing.evidence_list.append({
                        'source': src,
                        'detail': f'transition to thread={target_thread} value={target_value}',
                    })
            else:
                target = StepIdentity(
                    state_variable=target_state_variable,
                    station=station,
                    thread_idx=target_thread,
                    step_value=target_value,
                )
                # Use condition from steps_raw if available
                condition = t_info.get('condition')
                transition = Transition(
                    target=target,
                    condition=condition,
                    branch='normal',
                    target_thread=target_thread,
                    target_value=target_value,
                    source=t_info.get('source', ''),
                )
                # Record evidence
                src = t_info.get('source', '')
                if src:
                    transition.evidence_list.append({
                        'source': src,
                        'detail': f'transition to thread={target_thread} value={target_value}',
                    })
                edge_map[edge_key] = transition

        for transition in edge_map.values():
            step.outgoing_transitions.append(transition)
            graph.transitions.append(transition)

        graph.steps.append(step)

    return graph
