#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/boolean_expr.py — Minimal Boolean Expression IR

表达 PLC Step Guard 和 Transition Condition 的最小布尔表达式 IR。

类型:
  - Atom(variable/comparison) — 原子项 (变量引用或比较)
  - Not(expr)                 — 逻辑非
  - And(children)             — 逻辑与
  - Or(children)              — 逻辑或
  - Unknown(raw evidence)     — 无法确定的结构

设计原则:
  1. 格式无关 — 不读取 source_format
  2. 语义来自图结构 — serial→AND, parallel→OR, negated→NOT, unresolved→Unknown
  3. 保留 source_elements — 每个 BooleanExpr 可追溯到源码元素
  4. 不从文本猜 — 只从 topology 恢复

使用方式:
  StepLogicSlicer 从 topology path 构建 BooleanExpr:
    - Serial chain 中的 VariableRef → And candidate
    - Parallel branch 中的 paths → Or candidate
    - Negated contact → Not
    - 结构无法确定 → Unknown
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ============================================================
# Boolean Expression IR
# ============================================================

@dataclass
class BoolAtom:
    """原子布尔项 — 一个变量引用或比较条件。"""
    expr_type: str = 'atom'
    variable: str = ''          # Variable name
    negated: bool = False       # For negated contacts
    comparison: dict = None     # For comparison conditions: {operator, operands}
    source_element_ids: list = field(default_factory=list)  # Element IDs
    source_connections: list = field(default_factory=list)  # Connection IDs

    def to_dict(self) -> dict:
        return {
            'expr_type': 'atom',
            'variable': self.variable,
            'negated': self.negated,
            'comparison': self.comparison,
            'source_element_ids': self.source_element_ids,
            'source_connections': self.source_connections,
        }

    def to_string(self) -> str:
        if self.comparison:
            ops = self.comparison.get('operands', [])
            op = self.comparison.get('operator', '')
            return f'{op}({", ".join(str(o) for o in ops)})'
        label = ('NOT ' + self.variable) if self.negated else self.variable
        return label


@dataclass
class BoolNot:
    """逻辑非。"""
    expr_type: str = 'not'
    operand: Any = None  # BoolAtom or nested BoolExpr

    def to_dict(self) -> dict:
        return {
            'expr_type': 'not',
            'operand': self.operand.to_dict() if hasattr(self.operand, 'to_dict') else self.operand,
        }

    def to_string(self) -> str:
        inner = self.operand.to_string() if hasattr(self.operand, 'to_string') else str(self.operand)
        return f'NOT ({inner})'


@dataclass
class BoolAnd:
    """逻辑与 — serial chain 中的多个条件。"""
    expr_type: str = 'and'
    children: list = field(default_factory=list)  # [BoolAtom | BoolNot | BoolOr | ...]
    source_element_ids: list = field(default_factory=list)
    source_connections: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'expr_type': 'and',
            'children': [c.to_dict() if hasattr(c, 'to_dict') else c for c in self.children],
            'source_element_ids': self.source_element_ids,
            'source_connections': self.source_connections,
        }

    def to_string(self) -> str:
        parts = [c.to_string() if hasattr(c, 'to_string') else str(c) for c in self.children]
        return ' AND '.join(parts)


@dataclass
class BoolOr:
    """逻辑或 — parallel branch 中的多个路径。"""
    expr_type: str = 'or'
    children: list = field(default_factory=list)
    source_element_ids: list = field(default_factory=list)
    source_connections: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'expr_type': 'or',
            'children': [c.to_dict() if hasattr(c, 'to_dict') else c for c in self.children],
            'source_element_ids': self.source_element_ids,
            'source_connections': self.source_connections,
        }

    def to_string(self) -> str:
        parts = [c.to_string() if hasattr(c, 'to_string') else str(c) for c in self.children]
        return ' OR '.join(parts)


@dataclass
class BoolUnknown:
    """无法确定的布尔结构 — 保留原始证据。"""
    expr_type: str = 'unknown'
    raw_evidence: str = ''
    source_element_ids: list = field(default_factory=list)
    reason: str = ''  # Why it's unknown

    def to_dict(self) -> dict:
        return {
            'expr_type': 'unknown',
            'raw_evidence': self.raw_evidence,
            'source_element_ids': self.source_element_ids,
            'reason': self.reason,
        }

    def to_string(self) -> str:
        return f'Unknown({self.raw_evidence})'


# ============================================================
# Branch Topology (separate from Boolean semantics)
# ============================================================

@dataclass
class BranchTopology:
    """Branch topology — describes the physical structure, not the logic.

    Separated from boolean_expression because:
    - branch_topology describes HOW elements are connected (linear/split/merge/parallel)
    - boolean_expression describes WHAT logic they represent (AND/OR/NOT)

    A parallel_paths topology might be AND (all paths required) or OR
    (any path sufficient) — the topology alone doesn't determine this.
    """
    topology_type: str = 'linear'  # linear, split, merge, parallel_paths, unresolved
    paths: list = field(default_factory=list)  # List of paths (each path is list of element_ids)
    merge_point: str = ''  # Element ID where branches merge (if any)
    description: str = ''

    def to_dict(self) -> dict:
        return {
            'topology_type': self.topology_type,
            'paths': self.paths,
            'merge_point': self.merge_point,
            'description': self.description,
        }


# ============================================================
# Boolean Expression Builder (from topology)
# ============================================================

class BooleanExprBuilder:
    """从 topology path 构建 BooleanExpr。

    规则:
      - Serial chain (same Y, consecutive X) → And candidate
      - Parallel branch (different Y at same X) → Or candidate
      - Negated contact → Not
      - Structure cannot be determined → Unknown

    不从文本猜 — 只从 topology 结构恢复。
    """

    def __init__(self):
        pass

    def build_from_path(self, element_ids: list, elem_by_id: dict,
                        connections: list, state_var_re=None) -> Any:
        """Build BooleanExpr from a topology path.

        Args:
            element_ids: List of element IDs in the path (from anchor to target)
            elem_by_id: {element_id: CanonicalElement}
            connections: List of ProgramConnection in the network
            state_var_re: Regex for state variables (to exclude from guards)

        Returns:
            BooleanExpr (BoolAtom, BoolAnd, BoolOr, BoolNot, or BoolUnknown)
        """
        if not element_ids:
            return BoolUnknown(reason='empty path')

        # Collect guard VariableRefs in the path (excluding anchor and target)
        guards = []
        for eid in element_ids[1:-1]:  # Skip anchor and target
            elem = elem_by_id.get(eid)
            if elem is None:
                continue
            if not hasattr(elem, 'element_type'):
                continue

            if elem.element_type == 'variable_ref':
                var = elem.name or elem.base_name
                if not var:
                    continue
                if state_var_re and state_var_re.search(var):
                    continue
                guards.append(BoolAtom(
                    variable=var,
                    negated=getattr(elem, 'negated', False),
                    source_element_ids=[eid],
                ))
            elif elem.element_type == 'condition':
                # A condition in the path — could be part of interlock
                ops = []
                for o in getattr(elem, 'operands', []):
                    if hasattr(o, 'name'):
                        ops.append(o.name)
                    elif hasattr(o, 'value'):
                        ops.append(str(o.value))
                guards.append(BoolAtom(
                    comparison={'operator': getattr(elem, 'operator', ''),
                               'operands': ops},
                    source_element_ids=[eid],
                ))

        if not guards:
            return BoolUnknown(reason='no guards in path')

        if len(guards) == 1:
            g = guards[0]
            if g.negated:
                return BoolNot(operand=g)
            return g

        # Multiple guards — check if they're serial or parallel
        # For now, default to And (serial chain)
        # Parallel detection would require analyzing connection types
        return BoolAnd(
            children=guards,
            source_element_ids=element_ids,
        )

    def build_from_branches(self, branch_paths: list, elem_by_id: dict,
                            connections: list, state_var_re=None) -> Any:
        """Build BooleanExpr from multiple branch paths.

        Args:
            branch_paths: List of paths (each path is list of element_ids)
            elem_by_id: Element lookup
            connections: Connections in the network
            state_var_re: State variable regex

        Returns:
            BooleanExpr — Or if parallel branches, And if serial, etc.
        """
        if not branch_paths:
            return BoolUnknown(reason='no branch paths')

        if len(branch_paths) == 1:
            return self.build_from_path(branch_paths[0], elem_by_id,
                                        connections, state_var_re)

        # Multiple paths — determine if they're parallel (OR) or serial (AND)
        # Check connection types between paths
        path_exprs = []
        for path in branch_paths:
            expr = self.build_from_path(path, elem_by_id, connections, state_var_re)
            path_exprs.append(expr)

        # If paths share a merge point, they're parallel (OR)
        # If paths are sequential, they're serial (AND)
        # Default: parallel branches = OR
        return BoolOr(
            children=path_exprs,
            source_element_ids=[eid for path in branch_paths for eid in path],
        )

    def build_branch_topology(self, forward_paths: dict, anchor_id: str,
                              connections: list) -> BranchTopology:
        """Build BranchTopology from forward BFS paths.

        Args:
            forward_paths: {target_element_id: path_from_anchor}
            anchor_id: Starting element ID
            connections: ProgramConnection list

        Returns:
            BranchTopology describing the physical structure
        """
        if not forward_paths:
            return BranchTopology(topology_type='linear', description='no forward paths')

        # Group paths by direct successor (first element after anchor)
        by_successor = {}
        for eid, path in forward_paths.items():
            if len(path) >= 2 and path[0] == anchor_id:
                succ = path[1]
                if succ not in by_successor:
                    by_successor[succ] = []
                by_successor[succ].append(path)

        if len(by_successor) <= 1:
            return BranchTopology(
                topology_type='linear',
                paths=list(forward_paths.values()),
                description='single forward chain',
            )

        # Multiple successors — check if they merge
        all_endpoints = set()
        for paths in by_successor.values():
            for p in paths:
                all_endpoints.add(p[-1])

        if len(all_endpoints) < len(forward_paths):
            # Some paths share endpoints — merge
            return BranchTopology(
                topology_type='parallel_paths',
                paths=list(forward_paths.values()),
                merge_point=list(all_endpoints)[0] if len(all_endpoints) == 1 else '',
                description='parallel branches that merge',
            )
        else:
            return BranchTopology(
                topology_type='split',
                paths=list(forward_paths.values()),
                description='branches that do not merge',
            )


def bool_expr_to_string(expr) -> str:
    """Convert any BooleanExpr to string."""
    if expr is None:
        return ''
    if hasattr(expr, 'to_string'):
        return expr.to_string()
    return str(expr)


def bool_expr_to_dict(expr) -> dict:
    """Convert any BooleanExpr to dict."""
    if expr is None:
        return None
    if hasattr(expr, 'to_dict'):
        return expr.to_dict()
    return {'expr_type': 'unknown', 'raw': str(expr)}
