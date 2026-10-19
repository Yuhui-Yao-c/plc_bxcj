#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/step_logic_slicer.py — Step Logic Slicer (Program Primitive)

格式无关的程序图切片器。从 Step Anchor (Condition) 出发，沿 Topology Edge
(ProgramConnection) 和 VariableFlowEdge (数据流边) 做 backward/forward BFS，
恢复属于该 Step 的 Guard / Action / Timer / Call / Transition / Branch。

输入:
  - DecodedProgram (Canonical IR with topology)
  - VariableFlowEdges (from DefUse analyzer)
  - Step anchors (from Step Discovery)
  - Config (state variable patterns, common enables)

输出:
  - StepLogicSlice per step

关键原则:
  1. 沿 Topology Edge + VariableFlowEdge 遍历 — 两种边类型分别记录
  2. network_id 边界 — element_id 可能只在 network 内唯一 (SMC2 Ix)
  3. Ghost 节点透传 — 只做路径中间点，不记录为 endpoint
  4. Transition condition 从 topology path 恢复为 BooleanExpr
  5. branch_topology 与 boolean_expression 分开
  6. Evidence path 完整记录 — 每个 Logic Fact 可追溯到 anchor + edge types

不含:
  - source_format 判断
  - PLCopen/SMC2 分支
  - 项目命名规则
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .canonical_ir import (
    DecodedProgram, ProgramUnit, VariableDeclaration,
    VariableRef, Literal, Condition, Assignment, Call, Timer,
    Comment, Branch, RawFragment, SourceLocation, ProgramConnection,
    VariableFlowEdge,
)
from .boolean_expr import (
    BoolAtom, BoolNot, BoolAnd, BoolOr, BoolUnknown,
    BranchTopology, BooleanExprBuilder,
    bool_expr_to_string, bool_expr_to_dict,
)


# ============================================================
# StepLogicSlice Schema
# ============================================================

@dataclass
class LogicFact:
    """一条逻辑事实 — 带有 evidence path (含 edge types)。

    Weakest-link provenance: 如果 evidence path 经过任何 ambiguous
    VariableFlowEdge，则整个 LogicFact 的 provenance 标记为 'ambiguous'。
    """
    fact_type: str           # guard, common_enable, action, call, timer,
                             # transition, branch, unresolved
    variable: str = ''
    negated: bool = False
    callee: str = ''
    timer_type: str = ''
    preset: str = ''
    target_step: dict = None
    boolean_expr: dict = None    # BooleanExpr (for guards/transitions)
    element_id: str = ''
    network_id: str = ''
    unit_name: str = ''
    evidence_path: list = field(default_factory=list)
    # Each entry: {'element_id': str, 'edge_type': str}
    # edge_type: 'start', 'topology:wire', 'topology:series_chain',
    #            'variable_flow:assignment_target_to_...', etc.
    source_location: dict = None
    provenance: str = 'topology_only'  # topology_only / validated_dataflow / ambiguous
    # Weakest-link: 'ambiguous' if any variable_flow edge is ambiguous,
    #               'validated_dataflow' if all variable_flow edges are resolved,
    #               'topology_only' if no variable_flow edges
    # v10.36: FB call parameters (inputs/outputs) for semantic description
    inputs: list = field(default_factory=list)   # [{name, value_str}]
    outputs: list = field(default_factory=list)  # [{name, value_str}]

    def to_dict(self) -> dict:
        return {
            'fact_type': self.fact_type,
            'variable': self.variable,
            'negated': self.negated,
            'callee': self.callee,
            'timer_type': self.timer_type,
            'preset': self.preset,
            'target_step': self.target_step,
            'boolean_expr': self.boolean_expr,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'unit_name': self.unit_name,
            'evidence_path': self.evidence_path,
            'source_location': self.source_location,
            'provenance': self.provenance,
            'inputs': self.inputs,
            'outputs': self.outputs,
        }


@dataclass
class StepLogicSlice:
    """一个 Step 的完整逻辑切片。"""
    step_identity: dict = field(default_factory=dict)

    anchor_element_id: str = ''
    anchor_network_id: str = ''
    anchor_unit_name: str = ''
    anchor_condition: dict = None

    # Backward slice
    common_enables: list = field(default_factory=list)
    guards: list = field(default_factory=list)
    unresolved_upstream: list = field(default_factory=list)

    # Forward slice
    actions: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    timers: list = field(default_factory=list)
    transitions: list = field(default_factory=list)

    # Topology vs Boolean (separated)
    branch_topology: dict = None    # BranchTopology.to_dict()
    boolean_expression: dict = None # BooleanExpr for guard condition

    # Source
    source_file: str = ''
    source_unit: str = ''
    comment: str = ''

    # Unresolved
    unresolved_logic: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'step_identity': self.step_identity,
            'anchor_element_id': self.anchor_element_id,
            'anchor_network_id': self.anchor_network_id,
            'anchor_unit_name': self.anchor_unit_name,
            'anchor_condition': self.anchor_condition,
            'common_enables': [f.to_dict() for f in self.common_enables],
            'guards': [f.to_dict() for f in self.guards],
            'unresolved_upstream': [f.to_dict() for f in self.unresolved_upstream],
            'actions': [f.to_dict() for f in self.actions],
            'calls': [f.to_dict() for f in self.calls],
            'timers': [f.to_dict() for f in self.timers],
            'transitions': [f.to_dict() for f in self.transitions],
            'branch_topology': self.branch_topology,
            'boolean_expression': self.boolean_expression,
            'source_file': self.source_file,
            'source_unit': self.source_unit,
            'comment': self.comment,
            'unresolved_logic': self.unresolved_logic,
        }


# ============================================================
# Step Logic Slicer
# ============================================================

class StepLogicSlicer:
    """格式无关的 Step Logic 程序图切片器。

    从 Step Anchor (Condition element) 出发：
      - Backward BFS (topology + variable_flow) → guards, common_enables
      - Forward BFS (topology + variable_flow) → actions, calls, timers, transitions

    遵守 network_id 边界：element_id 可能只在 network 内唯一。
    VariableFlowEdge 可以跨 network，但必须通过 variable identity 匹配。
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.common_enable_vars = config.get('common_enable_vars', [])
        self.state_var_names = []
        for sv in config.get('state_variables', []):
            self.state_var_names.append(sv.get('name', ''))
        if not self.state_var_names:
            single = config.get('step_var_name', '')
            if single:
                self.state_var_names = [single]

        self.state_var_re = re.compile(
            '|'.join(re.escape(v) for v in self.state_var_names)) if self.state_var_names else None

        self.step_var_extract_re = re.compile(
            config.get('step_var_extract_pattern',
                       r'([A-Z]{2}\d{2})V?\.(\w+)\[(\d+)\]'))

        self.bool_builder = BooleanExprBuilder()

    def slice(self, decoded_programs: list, step_anchors: list,
              variable_flow_edges: list = None) -> list:
        """Slice program logic for each step anchor.

        Args:
            decoded_programs: list of DecodedProgram
            step_anchors: list of step anchor dicts
            variable_flow_edges: list of VariableFlowEdge (from DefUse analyzer)

        Returns:
            list of StepLogicSlice
        """
        variable_flow_edges = variable_flow_edges or []

        # Build lookup: (source_file, unit_name) → ProgramUnit
        unit_lookup = {}
        # Also build a global element lookup: (file, unit, element_id) → element
        # for cross-unit variable_flow traversal
        global_elem_lookup = {}

        for dp in decoded_programs:
            source_file = dp.source_file or ''
            file_name = source_file.split('/')[-1].split('\\')[-1]
            for unit in dp.program_units:
                key = (file_name, unit.name)
                unit_lookup[key] = unit
                for elem in unit.elements:
                    eid = elem.element_id
                    if eid:
                        gkey = (file_name, unit.name, eid)
                        global_elem_lookup[gkey] = elem

        # Build variable_flow adjacency: element_id → [(target_element_id, edge)]
        # Keyed by (file, unit) for scope resolution
        vf_forward = defaultdict(list)  # (file, unit, element_id) → [(file, unit, element_id, edge)]
        vf_reverse = defaultdict(list)  # (file, unit, element_id) → [(file, unit, element_id, edge)]

        for edge in variable_flow_edges:
            src_key = (edge.source_file, edge.source_unit_name, edge.source_element_id)
            tgt_key = (edge.target_file, edge.target_unit_name, edge.target_element_id)
            vf_forward[src_key].append((tgt_key, edge))
            vf_reverse[tgt_key].append((src_key, edge))

        slices = []

        for anchor in step_anchors:
            source_file = anchor.get('source_file', '')
            unit_name = anchor.get('unit_name', '')
            unit = unit_lookup.get((source_file, unit_name))

            if unit is None:
                # Fallback: try matching by unit_name only
                for (sf, un), u in unit_lookup.items():
                    if un == unit_name:
                        unit = u
                        break

            if unit is None:
                slices.append(StepLogicSlice(
                    step_identity={
                        'state_variable': anchor.get('state_variable', ''),
                        'station': anchor.get('station', ''),
                        'thread_idx': anchor.get('thread_idx', 0),
                        'step_value': anchor.get('step_value', 0),
                    },
                    unresolved_logic=[f'Unit not found: {unit_name}'],
                ))
                continue

            slice_result = self._slice_step(
                unit, anchor, source_file,
                global_elem_lookup, vf_forward, vf_reverse,
                unit_lookup)
            slices.append(slice_result)

        return slices

    def _slice_step(self, unit: ProgramUnit, anchor: dict,
                    source_file: str, global_elem_lookup: dict,
                    vf_forward: dict, vf_reverse: dict,
                    unit_lookup: dict) -> StepLogicSlice:
        """Slice logic for a single step anchor."""
        anchor_id = anchor.get('element_id', '')
        anchor_nid = anchor.get('network_id', '')

        # Build per-network topology adjacency
        topo_forward, topo_reverse = self._build_topology_adjacency(unit, anchor_nid)

        # Build element lookup by element_id within this network
        elem_by_id = {}
        for elem in unit.elements:
            if not anchor_nid or (elem.network_id == anchor_nid):
                if elem.element_id:
                    elem_by_id[elem.element_id] = elem

        # Also get anchor element
        anchor_elem = elem_by_id.get(anchor_id)

        # Backward slice: topology + variable_flow
        backward_results = self._bfs_dual(
            anchor_id, anchor_nid, source_file, unit.name,
            topo_reverse, vf_reverse,
            elem_by_id, global_elem_lookup, unit_lookup,
            direction='backward')

        common_enables, guards, unresolved_up = self._classify_backward(
            backward_results, elem_by_id, global_elem_lookup, unit_lookup)

        # Forward slice: topology + variable_flow
        forward_results = self._bfs_dual(
            anchor_id, anchor_nid, source_file, unit.name,
            topo_forward, vf_forward,
            elem_by_id, global_elem_lookup, unit_lookup,
            direction='forward')

        actions, calls, timers, transitions = self._classify_forward(
            forward_results, elem_by_id, global_elem_lookup, unit_lookup, anchor)

        # Build branch topology (separate from boolean expression)
        branch_topology = self.bool_builder.build_branch_topology(
            {r['element_id']: r['path_ids'] for r in forward_results
             if r['edge_type'].startswith('topology')},
            anchor_id, unit.connections)

        # Build guard boolean expression via topology path enumeration.
        # This replaces the BFS-based _build_boolean_from_guards which
        # defaulted to BoolAnd for all multi-guard cases. Path enumeration
        # correctly distinguishes serial (AND) from parallel (OR) branches.
        backward_paths = self._enumerate_backward_paths(
            unit, anchor_id, anchor_nid)
        bool_expr = self._build_bool_from_topology_paths(
            backward_paths, elem_by_id)

        # Override transition boolean expressions with topology path
        # enumeration. Each transition may have multiple topology paths
        # from anchor to the MOVE assignment — multiple paths means OR
        # (alternative conditions), within each path means AND (serial).
        for t in transitions:
            t_paths = self._enumerate_topology_paths(
                unit, anchor_id, t.element_id, anchor_nid)
            if t_paths:
                t_bool = self._build_bool_from_topology_paths(
                    t_paths, elem_by_id)
                t.boolean_expr = bool_expr_to_dict(t_bool)
            # If no topology paths found (e.g., cross-network via variable_flow),
            # keep the BFS-based expression from _classify_forward as fallback

        # Find comment — match by step number in comment text, then by network_id
        # FIX v10.36: Filter by state_variable family to prevent cross-family
        # comment contamination (e.g., m_nNextAutoStep[3]=110 getting the comment
        # of m_nNextAutoStep_load_Up[3]=110).
        comment = ''
        step_val_str = str(anchor.get('step_value', ''))
        anchor_sv = anchor.get('state_variable', '')
        # First: try to find a comment whose text starts with the step value
        # AND is in the same network as the anchor (most reliable)
        for elem in unit.elements:
            if isinstance(elem, Comment) and elem.text:
                text = elem.text.strip()
                if re.match(r'^' + re.escape(step_val_str) + r'[：:\.\s]', text):
                    # Prefer comment from the same network as anchor
                    if elem.network_id == anchor_nid:
                        comment = text
                        break
                    # If not same network, only accept if no better match found yet
                    if not comment:
                        comment = text
        # Second: fall back to network_id match (any text)
        if not comment:
            for elem in unit.elements:
                if isinstance(elem, Comment) and elem.network_id == anchor_nid and elem.text:
                    comment = elem.text
                    break
        # Third: for PLCopen FBD (no network_id), try to find a non-trivial comment
        # (not "网络注释") that mentions the step value
        if not comment and not anchor_nid:
            for elem in unit.elements:
                if isinstance(elem, Comment) and elem.text:
                    text = elem.text.strip()
                    if text and text != '网络注释' and step_val_str in text:
                        comment = text
                        break

        # Build anchor condition dict
        anchor_cond = None
        if isinstance(anchor_elem, Condition):
            ops = []
            for o in anchor_elem.operands:
                if isinstance(o, VariableRef):
                    ops.append({'type': 'variable_ref', 'name': o.name})
                elif isinstance(o, Literal):
                    ops.append({'type': 'literal', 'value': o.value, 'raw_text': o.raw_text})
            anchor_cond = {'operator': anchor_elem.operator, 'operands': ops}

        return StepLogicSlice(
            step_identity={
                'state_variable': anchor.get('state_variable', ''),
                'station': anchor.get('station', ''),
                'thread_idx': anchor.get('thread_idx', 0),
                'step_value': anchor.get('step_value', 0),
            },
            anchor_element_id=anchor_id,
            anchor_network_id=anchor_nid,
            anchor_unit_name=unit.name,
            anchor_condition=anchor_cond,
            common_enables=common_enables,
            guards=guards,
            unresolved_upstream=unresolved_up,
            actions=actions,
            calls=calls,
            timers=timers,
            transitions=transitions,
            branch_topology=branch_topology.to_dict(),
            boolean_expression=bool_expr_to_dict(bool_expr),
            source_file=anchor.get('source_file', ''),
            source_unit=unit.name,
            comment=comment,
        )

    # ============================================================
    # Topology Path Enumeration (replaces BFS for boolean expr)
    # ============================================================

    def _enumerate_topology_paths(self, unit: ProgramUnit, anchor_id: str,
                                   target_id: str, network_id: str,
                                   max_paths: int = 100,
                                   max_depth: int = 25) -> list:
        """Find all simple topology paths from anchor to target via DFS.

        Topology connections only (series_chain, parallel_branch,
        branch_merge, power_rail_in/out). No variable_flow edges.

        Returns list of paths, each path is a list of element_id strings
        ordered from anchor to target.
        """
        forward_adj = defaultdict(list)
        for conn in unit.connections:
            if network_id and conn.network_id and conn.network_id != network_id:
                continue
            forward_adj[conn.source_element_id].append(conn.target_element_id)

        paths = []
        visited = {anchor_id}

        def _dfs(node, path):
            if len(paths) >= max_paths:
                return
            if node == target_id:
                paths.append(list(path))
                return
            if len(path) >= max_depth:
                return
            for nxt in forward_adj.get(node, []):
                if nxt in visited:
                    continue
                visited.add(nxt)
                path.append(nxt)
                _dfs(nxt, path)
                path.pop()
                visited.remove(nxt)

        _dfs(anchor_id, [anchor_id])
        return paths

    def _enumerate_backward_paths(self, unit: ProgramUnit, anchor_id: str,
                                   network_id: str,
                                   max_paths: int = 100,
                                   max_depth: int = 25) -> list:
        """Find all topology paths from leaf/entry nodes to anchor (backward).

        Uses reverse adjacency. Stops at power_rail_in sources or nodes
        with no further predecessors.

        Returns list of paths, each path is a list of element_id strings
        ordered from leaf (first) to anchor (last).
        """
        reverse_adj = defaultdict(list)
        for conn in unit.connections:
            if network_id and conn.network_id and conn.network_id != network_id:
                continue
            reverse_adj[conn.target_element_id].append(conn.source_element_id)

        paths = []
        visited = {anchor_id}

        def _dfs(node, path):
            if len(paths) >= max_paths:
                return
            preds = reverse_adj.get(node, [])
            # Filter out pseudo-elements (LRI/RRI)
            real_preds = [p for p in preds
                          if not p.startswith('LRI:') and not p.startswith('RRI:')]
            if not real_preds:
                # Leaf node or only power-rail predecessor — path complete
                paths.append(list(path))
                return
            if len(path) >= max_depth:
                paths.append(list(path))
                return
            for pred in real_preds:
                if pred in visited:
                    continue
                visited.add(pred)
                path.insert(0, pred)
                _dfs(pred, path)
                path.pop(0)
                visited.remove(pred)

        _dfs(anchor_id, [anchor_id])
        return paths

    def _extract_guard_atoms(self, path_ids: list,
                              elem_by_id: dict) -> list:
        """Extract guard BoolAtoms from a topology path.

        Skips first (anchor) and last (target) elements.
        Skips elements not in elem_by_id (HL ghost nodes, comments).
        Skips state variable references.
        """
        atoms = []
        for eid in path_ids[1:-1]:
            elem = elem_by_id.get(eid)
            if elem is None:
                continue
            if isinstance(elem, VariableRef):
                var = elem.name or elem.base_name
                if not var:
                    continue
                if self.state_var_re and self.state_var_re.search(var):
                    continue
                atoms.append(BoolAtom(
                    variable=var,
                    negated=elem.negated,
                    source_element_ids=[eid],
                ))
            elif isinstance(elem, Condition):
                ops = []
                for o in getattr(elem, 'operands', []):
                    if hasattr(o, 'name'):
                        ops.append(o.name)
                    elif hasattr(o, 'value'):
                        ops.append(str(o.value))
                atoms.append(BoolAtom(
                    comparison={'operator': getattr(elem, 'operator', ''),
                               'operands': ops},
                    source_element_ids=[eid],
                ))
        return atoms

    def _atoms_to_bool(self, atoms: list):
        """Convert a list of BoolAtoms to a BooleanExpr (single=atom, multi=And)."""
        if not atoms:
            return None
        if len(atoms) == 1:
            return atoms[0]
        return BoolAnd(children=atoms,
                       source_element_ids=[a.source_element_ids[0] for a in atoms
                                            if a.source_element_ids])

    def _build_bool_from_topology_paths(self, paths: list,
                                         elem_by_id: dict):
        """Build BooleanExpr from multiple topology paths.

        Each path -> AND of guard atoms (excluding anchor, target, state vars)
        Multiple paths -> OR of ANDs
        No guards on any path -> BoolUnknown
        No paths -> BoolUnknown

        This replaces the BFS-based _build_boolean_from_guards and
        _build_transition_condition which defaulted to BoolAnd for
        all multi-guard cases, incorrectly treating parallel branches
        as AND instead of OR.
        """
        if not paths:
            return BoolUnknown(reason='no topology paths')

        path_exprs = []
        for path_ids in paths:
            atoms = self._extract_guard_atoms(path_ids, elem_by_id)
            if not atoms:
                continue
            expr = self._atoms_to_bool(atoms)
            if expr is not None:
                path_exprs.append(expr)

        if not path_exprs:
            return BoolUnknown(reason='no guards found in any topology path')

        if len(path_exprs) == 1:
            return path_exprs[0]

        return BoolOr(children=path_exprs,
                      source_element_ids=[eid for p in paths for eid in p])

    def _build_topology_adjacency(self, unit: ProgramUnit, network_id: str):
        """Build forward and reverse topology adjacency, scoped to network_id."""
        forward = defaultdict(list)
        reverse = defaultdict(list)

        for conn in unit.connections:
            if network_id and conn.network_id and conn.network_id != network_id:
                continue
            src = conn.source_element_id
            tgt = conn.target_element_id
            forward[src].append((tgt, conn.connection_type))
            reverse[tgt].append((src, conn.connection_type))

        return forward, reverse

    def _bfs_dual(self, start_id: str, start_nid: str, source_file: str,
                  unit_name: str, topo_adj: dict, vf_adj: dict,
                  elem_by_id: dict, global_elem_lookup: dict,
                  unit_lookup: dict, direction: str = 'forward') -> list:
        """BFS traversing both topology edges and variable_flow edges.

        Strategy:
          - Topology edges: BFS within network (depth limit 10)
          - Variable flow edges: Only from start node and 1 hop (depth limit 2)
            This prevents explosion in dense data flow graphs.

        Returns list of results:
            [{element_id, path, path_ids, edge_type, ...}]
        """
        visited = set()
        start_key = (source_file, unit_name, start_id)

        # Phase 1: Topology BFS (within network)
        topo_results = []
        topo_queue = [(start_key, [{'element_id': start_id, 'edge_type': 'start'}], 0)]
        topo_max_depth = 10
        topo_max_results = 200

        while topo_queue and len(topo_results) < topo_max_results:
            cur_key, path, depth = topo_queue.pop(0)
            if depth >= topo_max_depth:
                continue

            cur_file, cur_unit, cur_id = cur_key
            if cur_key in visited:
                continue
            visited.add(cur_key)

            for nxt_id, conn_type in topo_adj.get(cur_id, []):
                nxt_key = (cur_file, cur_unit, nxt_id)
                if nxt_key in visited:
                    continue
                edge_label = f'topology:{conn_type}'
                new_path = path + [{'element_id': nxt_id, 'edge_type': edge_label}]
                # Topology traversal stays within one network, so use the
                # network-scoped elem_by_id first (element_id is unique per
                # network but NOT per unit — SMC2 Ix repeats across rungs).
                # Fall back to global lookup only for cross-network cases.
                nxt_elem = elem_by_id.get(nxt_id)
                if nxt_elem is None:
                    nxt_elem = self._lookup_element(
                        nxt_key, global_elem_lookup, unit_lookup)
                if nxt_elem is not None:
                    topo_results.append({
                        'element_id': nxt_id,
                        'path': new_path,
                        'path_ids': [p['element_id'] for p in new_path],
                        'edge_type': edge_label,
                        'is_cross_network': False,
                        'is_cross_unit': False,
                        'target_elem': nxt_elem,
                        'target_key': nxt_key,
                    })
                topo_queue.append((nxt_key, new_path, depth + 1))

        # Phase 2: Variable flow BFS (only from start, depth 2)
        vf_results = []
        vf_visited = set()
        vf_queue = [(start_key, [{'element_id': start_id, 'edge_type': 'start'}], 0)]
        vf_max_depth = 3
        vf_max_results = 50

        while vf_queue and len(vf_results) < vf_max_results:
            cur_key, path, depth = vf_queue.pop(0)
            if depth >= vf_max_depth:
                continue

            if cur_key in vf_visited:
                continue
            vf_visited.add(cur_key)

            for nxt_key, vf_edge in vf_adj.get(cur_key, []):
                if nxt_key in vf_visited:
                    continue
                edge_label = f'variable_flow:{vf_edge.flow_type}'
                new_path = path + [{
                    'element_id': nxt_key[2],
                    'edge_type': edge_label,
                    'variable': vf_edge.variable_expression,
                    'resolution_status': vf_edge.resolution_status,
                }]
                nxt_elem = self._lookup_element(nxt_key, global_elem_lookup, unit_lookup)
                is_cross_network = (vf_edge.source_network_id != vf_edge.target_network_id)
                is_cross_unit = (vf_edge.source_unit_name != vf_edge.target_unit_name)

                if nxt_elem is not None:
                    vf_results.append({
                        'element_id': nxt_key[2],
                        'path': new_path,
                        'path_ids': [p['element_id'] for p in new_path],
                        'edge_type': edge_label,
                        'is_cross_network': is_cross_network,
                        'is_cross_unit': is_cross_unit,
                        'target_elem': nxt_elem,
                        'target_key': nxt_key,
                        'variable_flow_edge': vf_edge,
                    })
                vf_queue.append((nxt_key, new_path, depth + 1))

        return topo_results + vf_results

    def _lookup_element(self, key: tuple, global_elem_lookup: dict,
                        unit_lookup: dict):
        """Look up an element by (file, unit, element_id)."""
        # Try direct lookup
        elem = global_elem_lookup.get(key)
        if elem is not None:
            return elem

        # Try finding in the unit
        file_name, unit_name, eid = key
        unit = unit_lookup.get((file_name, unit_name))
        if unit:
            for e in unit.elements:
                if e.element_id == eid:
                    return e
        return None

    def _check_provenance(self, evidence_path: list) -> str:
        """Check weakest-link provenance from evidence path.

        Returns:
          - 'topology_only' — only topology edges, high confidence
          - 'validated_dataflow' — variable_flow edges present, all resolved
          - 'ambiguous' — at least one ambiguous variable_flow edge
        """
        has_variable_flow = False
        has_ambiguous = False
        for step in evidence_path:
            edge_type = step.get('edge_type', '')
            if 'variable_flow' in edge_type:
                has_variable_flow = True
                status = step.get('resolution_status', 'ambiguous')
                if status == 'ambiguous':
                    has_ambiguous = True
                    break
        if has_ambiguous:
            return 'ambiguous'
        if has_variable_flow:
            return 'validated_dataflow'
        return 'topology_only'

    def _classify_backward(self, results: list, elem_by_id: dict,
                           global_elem_lookup: dict, unit_lookup: dict):
        """Classify backward-sliced elements into guards and enables."""
        common_enables = []
        guards = []
        unresolved = []

        seen_keys = set()

        for r in results:
            elem = r.get('target_elem')
            if elem is None:
                continue

            rkey = r['target_key']
            if rkey in seen_keys:
                continue
            seen_keys.add(rkey)

            if isinstance(elem, VariableRef):
                var = elem.name or elem.base_name
                if not var:
                    continue
                if self.state_var_re and self.state_var_re.search(var):
                    continue
                # Build boolean expr for this guard
                bool_expr = self.bool_builder.build_from_path(
                    r['path_ids'], self._build_local_elem_map(r, global_elem_lookup, unit_lookup),
                    [], self.state_var_re)

                fact = LogicFact(
                    fact_type='guard',
                    variable=var,
                    negated=elem.negated,
                    boolean_expr=bool_expr_to_dict(bool_expr),
                    element_id=r['element_id'],
                    network_id=getattr(elem, 'network_id', ''),
                    unit_name=r['target_key'][1],
                    evidence_path=r['path'],
                    source_location=elem.source_location.to_dict() if elem.source_location else None,
                    provenance=self._check_provenance(r['path']),
                )
                if self._is_common_enable(var):
                    fact.fact_type = 'common_enable'
                    common_enables.append(fact)
                else:
                    guards.append(fact)
            elif isinstance(elem, Condition):
                unresolved.append(LogicFact(
                    fact_type='unresolved',
                    element_id=r['element_id'],
                    network_id=getattr(elem, 'network_id', ''),
                    unit_name=r['target_key'][1],
                    evidence_path=r['path'],
                ))
            elif not isinstance(elem, (Comment,)):
                unresolved.append(LogicFact(
                    fact_type='unresolved',
                    element_id=r['element_id'],
                    network_id=getattr(elem, 'network_id', ''),
                    unit_name=r['target_key'][1],
                    evidence_path=r['path'],
                ))

        return common_enables, guards, unresolved

    def _classify_forward(self, results: list, elem_by_id: dict,
                          global_elem_lookup: dict, unit_lookup: dict,
                          anchor: dict):
        """Classify forward-sliced elements into actions, calls, timers, transitions.

        FIX v10.36:
          1. Prefer same-network topology transitions over cross-network variable_flow
             transitions. Only use variable_flow transitions if no topology transitions
             are found.
          2. Preserve FB call inputs/outputs in LogicFact for semantic description.
        """
        actions = []
        calls = []
        timers = []
        transitions = []
        transitions_vf = []  # variable_flow transitions (lower priority)

        seen_keys = set()
        anchor_step_value = anchor.get('step_value', 0)
        anchor_nid = anchor.get('network_id', '')

        for r in results:
            elem = r.get('target_elem')
            if elem is None:
                continue

            rkey = r['target_key']
            if rkey in seen_keys:
                continue
            seen_keys.add(rkey)

            if isinstance(elem, Assignment):
                target_name = elem.target.name if elem.target else ''
                if not target_name:
                    continue

                # Check if it's a transition (writes to state variable)
                if self.state_var_re and self.state_var_re.search(target_name):
                    target_thread = -1
                    sv_m = re.search(r'\[(\d+)\]', target_name)
                    if sv_m:
                        target_thread = int(sv_m.group(1))

                    target_value = None
                    if isinstance(elem.source, Literal):
                        target_value = elem.source.value
                    elif isinstance(elem.source, VariableRef):
                        target_value = None

                    if target_value is not None and isinstance(target_value, int):
                        # Build boolean expr for transition condition
                        bool_expr = self._build_transition_condition(
                            r, elem, global_elem_lookup, unit_lookup)

                        t_fact = LogicFact(
                            fact_type='transition',
                            target_step={'thread': target_thread, 'value': target_value},
                            boolean_expr=bool_expr_to_dict(bool_expr),
                            element_id=r['element_id'],
                            network_id=getattr(elem, 'network_id', ''),
                            unit_name=r['target_key'][1],
                            evidence_path=r['path'],
                            source_location=elem.source_location.to_dict() if elem.source_location else None,
                            provenance=self._check_provenance(r['path']),
                        )

                        # v10.36: Separate topology transitions from variable_flow
                        is_topo = all('topology' in p.get('edge_type', '')
                                      for p in r['path'] if p.get('edge_type') != 'start')
                        if is_topo:
                            transitions.append(t_fact)
                        else:
                            transitions_vf.append(t_fact)
                else:
                    actions.append(LogicFact(
                        fact_type='action',
                        variable=target_name,
                        element_id=r['element_id'],
                        network_id=getattr(elem, 'network_id', ''),
                        unit_name=r['target_key'][1],
                        evidence_path=r['path'],
                        source_location=elem.source_location.to_dict() if elem.source_location else None,
                        provenance=self._check_provenance(r['path']),
                    ))

            elif isinstance(elem, Call):
                # v10.36: Preserve FB inputs/outputs for semantic description
                call_inputs = []
                call_outputs = []
                for inp in getattr(elem, 'inputs', []):
                    name = inp.get('name', '')
                    val = inp.get('value', None)
                    val_str = ''
                    if hasattr(val, 'name'):
                        val_str = val.name or ''
                    elif hasattr(val, 'value'):
                        val_str = str(val.value)
                    elif hasattr(val, 'raw_text'):
                        val_str = val.raw_text or ''
                    if name:
                        call_inputs.append({'name': name, 'value': val_str})
                for out in getattr(elem, 'outputs', []):
                    name = out.get('name', '')
                    val = out.get('value', None)
                    val_str = ''
                    if hasattr(val, 'name'):
                        val_str = val.name or ''
                    if name:
                        call_outputs.append({'name': name, 'value': val_str})

                calls.append(LogicFact(
                    fact_type='call',
                    callee=elem.callee,
                    element_id=r['element_id'],
                    network_id=getattr(elem, 'network_id', ''),
                    unit_name=r['target_key'][1],
                    evidence_path=r['path'],
                    source_location=elem.source_location.to_dict() if elem.source_location else None,
                    inputs=call_inputs,
                    outputs=call_outputs,
                ))

            elif isinstance(elem, Timer):
                timers.append(LogicFact(
                    fact_type='timer',
                    timer_type=elem.timer_type,
                    preset=elem.preset,
                    element_id=r['element_id'],
                    network_id=getattr(elem, 'network_id', ''),
                    unit_name=r['target_key'][1],
                    evidence_path=r['path'],
                    source_location=elem.source_location.to_dict() if elem.source_location else None,
                ))

        # v10.36: Prefer topology transitions over variable_flow transitions.
        # Topology transitions are from the same network as the anchor and are
        # reliably the MOVE assignment for this step. Variable_flow transitions
        # may come from completely different networks and cause contamination.
        if transitions:
            # Use only topology transitions
            pass
        elif transitions_vf:
            # No topology transitions found — use variable_flow as fallback
            transitions = transitions_vf

        return actions, calls, timers, transitions

    def _build_transition_condition(self, result: dict, transition_elem: Assignment,
                                     global_elem_lookup: dict, unit_lookup: dict):
        """Build BooleanExpr for transition condition from evidence path.

        The path goes: anchor → ... → transition (Assignment).
        Guards in the path between anchor and transition form the condition.
        """
        path_ids = result['path_ids']
        path = result['path']

        # Collect guard elements in the path
        guards_in_path = []
        for i, pid in enumerate(path_ids[1:-1], 1):  # Skip anchor and transition
            # Look up element
            step = path[i]
            file_name = result['target_key'][0]
            # Try to find element in the path's unit context
            # For topology edges, element is in same unit
            # For variable_flow edges, element may be in different unit
            unit_name = self._infer_unit_from_path(path, i, result['target_key'][1])
            elem = global_elem_lookup.get((file_name, unit_name, pid))

            if elem is None:
                # Try elem_by_id (same network)
                elem = self._find_element_in_units(file_name, pid, unit_lookup)

            if elem is None:
                continue

            if isinstance(elem, VariableRef):
                var = elem.name or elem.base_name
                if not var:
                    continue
                if self.state_var_re and self.state_var_re.search(var):
                    continue
                guards_in_path.append(BoolAtom(
                    variable=var,
                    negated=elem.negated,
                    source_element_ids=[pid],
                ))
            elif isinstance(elem, Condition):
                ops = []
                for o in getattr(elem, 'operands', []):
                    if hasattr(o, 'name'):
                        ops.append(o.name)
                    elif hasattr(o, 'value'):
                        ops.append(str(o.value))
                guards_in_path.append(BoolAtom(
                    comparison={'operator': getattr(elem, 'operator', ''),
                               'operands': ops},
                    source_element_ids=[pid],
                ))

        if not guards_in_path:
            return BoolUnknown(reason='no guards in transition path')

        if len(guards_in_path) == 1:
            g = guards_in_path[0]
            if g.negated:
                return BoolNot(operand=g)
            return g

        return BoolAnd(
            children=guards_in_path,
            source_element_ids=path_ids,
        )

    def _build_boolean_from_guards(self, guard_results: list, elem_by_id: dict,
                                    global_elem_lookup: dict, unit_lookup: dict):
        """Build BooleanExpr from guard backward results."""
        if not guard_results:
            return BoolUnknown(reason='no guards')

        atoms = []
        for r in guard_results:
            elem = r.get('target_elem')
            if isinstance(elem, VariableRef):
                var = elem.name or elem.base_name
                if not var:
                    continue
                if self.state_var_re and self.state_var_re.search(var):
                    continue
                atoms.append(BoolAtom(
                    variable=var,
                    negated=elem.negated,
                    source_element_ids=[r['element_id']],
                ))

        if not atoms:
            return BoolUnknown(reason='no guard atoms')

        if len(atoms) == 1:
            if atoms[0].negated:
                return BoolNot(operand=atoms[0])
            return atoms[0]

        return BoolAnd(children=atoms)

    def _build_local_elem_map(self, result: dict, global_elem_lookup: dict,
                              unit_lookup: dict) -> dict:
        """Build a local element map from a result's path."""
        m = {}
        path = result.get('path', [])
        file_name = result['target_key'][0]
        for step in path:
            eid = step.get('element_id', '')
            if not eid:
                continue
            # Try to find in global lookup with different unit contexts
            for (f, u, e), elem in global_elem_lookup.items():
                if e == eid and f == file_name:
                    m[eid] = elem
                    break
        return m

    def _infer_unit_from_path(self, path: list, idx: int, default_unit: str) -> str:
        """Infer the unit name for a path step."""
        # For topology edges, unit is the same as the anchor's unit
        # For variable_flow edges, unit may differ
        # For now, use the default unit (anchor's unit)
        return default_unit

    def _find_element_in_units(self, file_name: str, element_id: str,
                                unit_lookup: dict):
        """Find an element by ID across all units in a file."""
        for (f, u), unit in unit_lookup.items():
            if f != file_name:
                continue
            for e in unit.elements:
                if e.element_id == element_id:
                    return e
        return None

    def _is_common_enable(self, var: str) -> bool:
        """Check if variable is a common enable."""
        if not var or not self.common_enable_vars:
            return False
        for pattern in self.common_enable_vars:
            if var == pattern or var.startswith(pattern + '[') or var.startswith(pattern + '.'):
                return True
        return False


# ============================================================
# Helper: Extract step anchors from DecodedProgram
# ============================================================

def find_step_anchors(decoded_programs: list, config: dict) -> list:
    """Find step anchor Condition elements in DecodedPrograms.

    A step anchor is a Condition (EQ) where one operand is a state variable
    and another is a literal step value.
    """
    state_vars_config = config.get('state_variables', [])
    state_var_names = [sv.get('name', '') for sv in state_vars_config]
    if not state_var_names:
        single = config.get('step_var_name', '')
        state_var_names = [single] if single else []

    if not state_var_names:
        return []

    eq_operator = config.get('eq_block_type', 'EQ')
    step_var_re = re.compile('|'.join(re.escape(v) for v in state_var_names))

    step_var_extract_re = re.compile(
        config.get('step_var_extract_pattern',
                   r'([A-Z]{2}\d{2})V?\.(\w+)\[(\d+)\]'))

    filename_station_pattern = config.get('filename_station_pattern')
    file_station_map = config.get('file_station_map', {})

    anchors = []

    for dp in decoded_programs:
        source_file = dp.source_file or ''
        file_name = source_file.split('/')[-1].split('\\')[-1]

        station = ''
        if file_name in file_station_map:
            station = file_station_map[file_name]
        elif filename_station_pattern:
            m = re.search(filename_station_pattern, file_name)
            if m:
                station = m.group(1)

        for unit in dp.program_units:
            for elem in unit.elements:
                if not isinstance(elem, Condition):
                    continue
                if elem.operator != eq_operator:
                    continue

                step_var_input = None
                value_input = None
                for operand in elem.operands:
                    if isinstance(operand, VariableRef):
                        name = operand.name or operand.base_name
                        if step_var_re.search(name) and re.search(r'\w+\[\d+\]', name):
                            step_var_input = name
                    elif isinstance(operand, Literal):
                        if operand.value is not None:
                            value_input = str(operand.value)

                if step_var_input and value_input:
                    try:
                        val = int(value_input)
                    except (ValueError, TypeError):
                        continue
                    if val <= 0:
                        continue

                    sv_m = step_var_extract_re.search(step_var_input)
                    if sv_m:
                        if sv_m.lastindex and sv_m.lastindex >= 3:
                            ext_station = sv_m.group(1)
                            ext_var = sv_m.group(2)
                            ext_thread = int(sv_m.group(3))
                        elif sv_m.lastindex and sv_m.lastindex >= 2:
                            ext_station = station
                            ext_var = sv_m.group(1)
                            ext_thread = int(sv_m.group(2))
                        else:
                            ext_station = station
                            ext_var = sv_m.group(1) if sv_m.lastindex else ''
                            ext_thread = -1
                    else:
                        sv_m2 = re.search(r'(\w+)\[(\d+)\]', step_var_input)
                        ext_station = station
                        ext_var = sv_m2.group(1) if sv_m2 else ''
                        ext_thread = int(sv_m2.group(2)) if sv_m2 else -1

                    anchors.append({
                        'element_id': elem.element_id,
                        'network_id': elem.network_id,
                        'unit_name': unit.name,
                        'source_file': file_name,
                        'state_variable': ext_var,
                        'station': ext_station or station,
                        'thread_idx': ext_thread,
                        'step_value': val,
                        'operator': elem.operator,
                        'operands': step_var_input,
                    })

    return anchors
