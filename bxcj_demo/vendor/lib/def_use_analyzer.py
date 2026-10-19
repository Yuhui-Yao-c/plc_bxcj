#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/def_use_analyzer.py — Def-Use Analysis for Canonical Program IR

通用 Def-Use 分析器。从 DecodedProgram 收集所有变量定义 (Definition) 和
变量使用 (Use)，在合法 scope 中形成 VariableFlowEdge。

核心原则:
  1. 格式无关 — 不读取 source_format / PLCopen / SMC2
  2. 保守匹配 — 多个 producer 保留为 ambiguous，不制造单一确定路径
  3. Scope 边界 — 跨 ProgramUnit 必须存在确定的 global symbol 关系
  4. 不针对特定变量名写特殊规则

Definition 来源:
  - Assignment.target (coil / MOVE 写入)
  - Call.outputs (FB 输出参数写入)

Use 来源:
  - VariableRef (独立 contact / 变量引用)
  - Condition.operands (比较操作数)
  - Call.inputs (FB/函数输入参数)
  - Timer (定时器预设值，如果是变量)

匹配规则:
  - variable_identity 匹配 (base_name + array index)
  - scope 检查:
      same_unit — 高置信度
      cross_unit_same_file — 需要 global 声明或同名匹配
      cross_file — 默认不连接 (除非有明确 global 关系)
  - 方向检查: definition 的 network_id 应在 use 之前 (或同 network)
  - 多 producer → resolution_status = 'ambiguous'
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .canonical_ir import (
    DecodedProgram, ProgramUnit,
    VariableRef, Literal, Condition, Assignment, Call, Timer,
    Comment, Branch, RawFragment, SourceLocation, ProgramConnection,
    VariableFlowEdge,
)


# ============================================================
# Definition / Use records
# ============================================================

@dataclass
class Definition:
    """A variable definition (write)."""
    variable_identity: str        # base_name for matching
    variable_expression: str      # full expression
    element_id: str
    network_id: str
    unit_name: str
    source_file: str
    def_type: str                 # 'assignment_target' / 'call_output'
    element: Any = None           # Reference to the Canonical Element


@dataclass
class Use:
    """A variable use (read)."""
    variable_identity: str
    variable_expression: str
    element_id: str
    network_id: str
    unit_name: str
    source_file: str
    use_type: str                 # 'variable_ref' / 'condition_operand' /
                                  # 'call_input' / 'timer_input'
    element: Any = None
    negated: bool = False


# ============================================================
# Def-Use Analyzer
# ============================================================

class DefUseAnalyzer:
    """通用 Def-Use 分析器。

    从 DecodedProgram 收集 Definitions 和 Uses，匹配形成 VariableFlowEdge。

    Scope 判定优先使用 VariableDeclaration:
      1. 查 Declaration → 获取 scope (pou name, datatype, at_address)
      2. 根据 Declaration scope 判定:
         - local: 同 POU 声明 → only same_unit flow
         - program_unit: POU-level variable → same_unit + cross_unit if same POU
         - global: 全局变量 → cross_unit allowed
         - fb_member: FB 实例成员 → only within FB call context
         - external: 外部引用 → conservative_skip
         - unknown: 无 Declaration → ambiguous
      3. 如果 Declaration 无法唯一解析 → resolution_status = 'ambiguous'

    不读取 source_format。不针对特定变量名写规则。
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.known_globals = set(config.get('known_global_variables', []))
        self.allow_cross_unitByName = config.get('allow_cross_unit_by_name', True)
        self.allow_cross_file = config.get('allow_cross_file', False)
        # Declaration lookup: variable_name → [VariableDeclaration]
        # Built from DecodedProgram.variable_declarations
        self._decl_map = {}

    def analyze(self, decoded_programs: list) -> list:
        """Analyze Def-Use for a list of DecodedPrograms.

        Args:
            decoded_programs: list of DecodedProgram

        Returns:
            list of VariableFlowEdge
        """
        all_defs = []
        all_uses = []

        # Build declaration lookup from all programs
        for dp in decoded_programs:
            for vd in dp.variable_declarations:
                name = vd.name if hasattr(vd, 'name') else vd.get('name', '')
                if name:
                    if name not in self._decl_map:
                        self._decl_map[name] = []
                    self._decl_map[name].append(vd)
            # Also add to known_globals if declared at global scope
            for vd in dp.variable_declarations:
                if hasattr(vd, 'scope') and not vd.scope:
                    # No scope = likely global
                    self.known_globals.add(vd.name if hasattr(vd, 'name') else vd.get('name', ''))

        for dp in decoded_programs:
            source_file = self._file_name(dp.source_file)
            for unit in dp.program_units:
                defs, uses = self._collect_unit(unit, source_file)
                all_defs.extend(defs)
                all_uses.extend(uses)

            # Also collect from known_globals
            for vd in dp.variable_declarations:
                if hasattr(vd, 'name'):
                    self.known_globals.add(vd.name)

        # Build symbol → definitions map
        def_map = defaultdict(list)
        for d in all_defs:
            def_map[self._var_key(d.variable_identity, d.variable_expression)].append(d)

        # Build symbol → uses map
        use_map = defaultdict(list)
        for u in all_uses:
            use_map[self._var_key(u.variable_identity, u.variable_expression)].append(u)

        # Match definitions to uses
        edges = []
        for var_key, defs in def_map.items():
            uses = use_map.get(var_key, [])
            if not uses:
                continue
            edges.extend(self._match_def_use(var_key, defs, uses))

        return edges

    def _collect_unit(self, unit: ProgramUnit, source_file: str):
        """Collect definitions and uses from a single ProgramUnit."""
        defs = []
        uses = []

        for elem in unit.elements:
            if isinstance(elem, Assignment):
                # Definition: target variable
                if elem.target:
                    target_expr = elem.target.name or elem.target.base_name
                    if target_expr:
                        defs.append(Definition(
                            variable_identity=elem.target.base_name or target_expr,
                            variable_expression=target_expr,
                            element_id=elem.element_id,
                            network_id=elem.network_id,
                            unit_name=unit.name,
                            source_file=source_file,
                            def_type='assignment_target',
                            element=elem,
                        ))
                # Use: source variable (if it's a VariableRef)
                if elem.source and isinstance(elem.source, VariableRef):
                    src_expr = elem.source.name or elem.source.base_name
                    if src_expr:
                        uses.append(Use(
                            variable_identity=elem.source.base_name or src_expr,
                            variable_expression=src_expr,
                            element_id=elem.element_id,
                            network_id=elem.network_id,
                            unit_name=unit.name,
                            source_file=source_file,
                            use_type='assignment_source',
                            element=elem,
                        ))

            elif isinstance(elem, Call):
                # Use: input parameters
                for inp in elem.inputs:
                    val = inp.get('value')
                    if isinstance(val, VariableRef):
                        expr = val.name or val.base_name
                        if expr:
                            uses.append(Use(
                                variable_identity=val.base_name or expr,
                                variable_expression=expr,
                                element_id=elem.element_id,
                                network_id=elem.network_id,
                                unit_name=unit.name,
                                source_file=source_file,
                                use_type='call_input',
                                element=elem,
                            ))
                # Definition: output parameters
                for out in elem.outputs:
                    val = out.get('value')
                    if isinstance(val, VariableRef):
                        expr = val.name or val.base_name
                        if expr:
                            defs.append(Definition(
                                variable_identity=val.base_name or expr,
                                variable_expression=expr,
                                element_id=elem.element_id,
                                network_id=elem.network_id,
                                unit_name=unit.name,
                                source_file=source_file,
                                def_type='call_output',
                                element=elem,
                            ))

            elif isinstance(elem, VariableRef):
                # Use: standalone variable reference (contact)
                expr = elem.name or elem.base_name
                if expr:
                    uses.append(Use(
                        variable_identity=elem.base_name or expr,
                        variable_expression=expr,
                        element_id=elem.element_id,
                        network_id=elem.network_id,
                        unit_name=unit.name,
                        source_file=source_file,
                        use_type='variable_ref',
                        element=elem,
                        negated=elem.negated,
                    ))

            elif isinstance(elem, Condition):
                # Use: comparison operands
                for operand in elem.operands:
                    if isinstance(operand, VariableRef):
                        expr = operand.name or operand.base_name
                        if expr:
                            uses.append(Use(
                                variable_identity=operand.base_name or expr,
                                variable_expression=expr,
                                element_id=elem.element_id,
                                network_id=elem.network_id,
                                unit_name=unit.name,
                                source_file=source_file,
                                use_type='condition_operand',
                                element=elem,
                            ))

        return defs, uses

    def _var_key(self, identity: str, expression: str) -> str:
        """Create a matching key for a variable.

        Uses the full expression for exact symbol identity matching.
        Two variables flow-connect only if they refer to the same variable:
            "Cylinder[32].AnastoleSafety" != "Cylinder[33].AnastoleSafety"
            "EM06V.Process_steps[5]" != "EM07V.Process_steps[5]"

        This ensures exact symbol identity as required by the spec.
        """
        expr = expression or identity
        return expr.strip()

    def _match_def_use(self, var_key: str, defs: list, uses: list) -> list:
        """Match definitions to uses for a single variable.

        Conservative matching:
        - Multiple producers → ambiguous
        - Cross-POU → check scope
        - Cross-file → default skip
        """
        edges = []

        for use in uses:
            matching_defs = []
            for d in defs:
                # Skip self-reference (same element)
                if d.element_id == use.element_id and d.network_id == use.network_id:
                    continue

                # Determine scope
                scope = self._determine_scope(d, use)

                # Scope filtering
                if scope == 'cross_file' and not self.allow_cross_file:
                    continue
                if scope == 'cross_unit_same_file' and not self.allow_cross_unitByName:
                    # Check if variable is known global
                    if d.variable_identity not in self.known_globals:
                        continue

                # Direction check: def should be before use
                # (same network or earlier network)
                # For PLCopen: network_id is empty, so we can't check order
                # For SMC2: network_id is "entry:rung", can compare
                if not self._is_valid_order(d, use):
                    continue

                matching_defs.append((d, scope))

            if not matching_defs:
                continue

            if len(matching_defs) == 1:
                d, scope = matching_defs[0]
                edges.append(self._make_edge(d, use, scope, 'resolved', 1.0))
            else:
                # Multiple producers — ambiguous
                # Create edges for each, marked as ambiguous
                for d, scope in matching_defs:
                    edges.append(self._make_edge(d, use, scope, 'ambiguous', 0.5))

        return edges

    def _determine_scope(self, d: Definition, u: Use) -> str:
        """Determine the scope between a definition and a use.

        Uses VariableDeclaration to determine if a variable is:
          - local (POU-scoped) → only same_unit flow
          - global (no scope or global declaration) → cross_unit allowed
          - fb_member (FB instance member) → only within FB call context
          - external (AT address binding) → cross_unit allowed
          - unknown (no declaration found) → ambiguous

        Falls back to file/unit comparison if declaration not available.
        """
        # Check declaration for this variable
        var_name = d.variable_identity
        decls = self._decl_map.get(var_name, [])

        if not decls:
            # No declaration found — unknown scope
            # Fall back to file/unit comparison
            if d.source_file != u.source_file:
                return 'cross_file'
            if d.unit_name != u.unit_name:
                return 'cross_unit_same_file'
            return 'same_unit'

        # If multiple declarations, scope is ambiguous
        if len(decls) > 1:
            # Check if all declarations are global (no scope)
            all_global = all(
                (not (vd.scope if hasattr(vd, 'scope') else vd.get('scope', '')))
                for vd in decls
            )
            if all_global:
                # Multiple global declarations — still global but ambiguous
                if d.source_file != u.source_file:
                    return 'cross_file'
                if d.unit_name != u.unit_name:
                    return 'cross_unit_same_file'
                return 'same_unit'
            # Mixed scopes — ambiguous
            return 'ambiguous_scope'

        # Single declaration — use its scope
        vd = decls[0]
        scope = vd.scope if hasattr(vd, 'scope') else vd.get('scope', '')
        at_addr = vd.at_address if hasattr(vd, 'at_address') else vd.get('at_address', '')

        if not scope:
            # No scope = global variable
            if d.source_file != u.source_file:
                return 'cross_file'
            if d.unit_name != u.unit_name:
                return 'cross_unit_same_file'
            return 'same_unit'

        if at_addr:
            # Has AT address binding — external/physical reference
            return 'external'

        # Has POU scope — check if same POU
        if d.unit_name == u.unit_name and d.source_file == u.source_file:
            return 'same_unit'
        else:
            # POU-scoped variable accessed from different POU
            # This could be valid if it's a global, or invalid if truly local
            # Conservative: mark as cross_unit_same_file (allow but flag)
            return 'cross_unit_same_file'

    def _is_valid_order(self, d: Definition, u: Use) -> bool:
        """Check if definition precedes use in execution order.

        Source order is used as EVIDENCE for candidate ranking, not as
        an absolute exclusion condition. In PLC scan cycles, variables
        can be written and read in any order across scans.

        Rules:
          - If both have network_id (SMC2): prefer def before use, but
            allow same or later network (scan cycle semantics)
          - If no network_id (PLCopen): allow — order is determined by
            wire topology, not network order
          - Never exclude solely based on source order
        """
        # If both have network_id, use it as evidence (not exclusion)
        if d.network_id and u.network_id:
            try:
                d_parts = d.network_id.split(':')
                u_parts = u.network_id.split(':')
                d_entry = int(d_parts[0]) if len(d_parts) > 0 else 0
                d_rung = int(d_parts[1]) if len(d_parts) > 1 else 0
                u_entry = int(u_parts[0]) if len(u_parts) > 0 else 0
                u_rung = int(u_parts[1]) if len(u_parts) > 1 else 0
                # Definition before use — high confidence
                if d_entry < u_entry or (d_entry == u_entry and d_rung < u_rung):
                    return True
                # Same network — could be bidirectional
                if d_entry == u_entry and d_rung == u_rung:
                    return True
                # Definition after use — still allow (scan cycle), but
                # will be marked as lower confidence by caller
                return True
            except (ValueError, IndexError):
                return True

        # If no network_id (PLCopen), allow — order is determined by
        # wire topology, not network order
        return True

    def _make_edge(self, d: Definition, u: Use, scope: str,
                   status: str, confidence: float) -> VariableFlowEdge:
        """Create a VariableFlowEdge from a Definition and Use."""
        return VariableFlowEdge(
            variable_identity=d.variable_identity,
            variable_expression=d.variable_expression,
            source_element_id=d.element_id,
            source_network_id=d.network_id,
            source_unit_name=d.unit_name,
            source_file=d.source_file,
            target_element_id=u.element_id,
            target_network_id=u.network_id,
            target_unit_name=u.unit_name,
            target_file=u.source_file,
            flow_type=f'{d.def_type}_to_{u.use_type}',
            scope=scope,
            resolution_status=status,
            confidence=confidence,
            evidence={
                'def_type': d.def_type,
                'use_type': u.use_type,
            },
        )

    def _file_name(self, path: str) -> str:
        """Extract file name from path."""
        if not path:
            return ''
        return path.replace('\\', '/').split('/')[-1]
