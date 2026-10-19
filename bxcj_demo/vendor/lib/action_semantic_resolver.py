#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/action_semantic_resolver.py — Action 语义解析器 (v3)

设计原则 (来自 opencode 指令):
  - opencode 负责理解/取证/配置/语义判断; Python 负责确定性解析与验证
  - 禁止把 root 解析成功称为 Action 语义解析成功
  - position_index 与 position_name 分离
  - 完整 nested datatype/member traversal, 每层产生可追溯 EvidenceRelation
  - Documentation/HMI Label 不能直接整体作为 actor
  - 每个字段必须独立记录 Evidence
  - 不确定就保留不确定

解析层级 (独立标志, 非互斥):
  expression_parsed       — 表达式可解析为 root/path/index
  root_resolved           — 根变量在 EvidenceStore 中找到
  object_resolved         — 第一个 path member (actor/object) 找到
  member_resolved         — 中间 member (如 In/Out 接口) 找到
  command_resolved        — 最终 command member (如 Auto_R/Location_Auto) 找到
  physical_semantic_resolved — command member 有 documentation (物理语义)
  position_semantic_resolved — position_name 有真实证据 (非 index)

支持的 traversal:
  - DUT/STRUCT (datatype → member)
  - nested STRUCT (member → sub-datatype → sub-member)
  - FB instance (instance_of → datatype)
  - array (index 处理)
  - member of array element
  - input/output structure (In/Out member → interface datatype)
  - alias/reference (external type → candidate alias resolution)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .evidence_levels import (
    FieldEvidenceV2, EXACT_SOURCE_FACT, VALIDATED_STRUCTURAL_SEMANTIC,
    PLAUSIBLE_STRUCTURAL_HYPOTHESIS, CONTEXTUAL_SEMANTIC, UNRESOLVED,
    EVIDENCE_LEVEL_ORDER, weakest_level,
)


# ============================================================
# Expression Parsing — import authoritative version from symbol_resolver
# ============================================================
# parse_expression and SymbolExpression are defined in symbol_resolver.py
# as the single authoritative implementation. ParsedExpression is kept
# as a backward-compatible alias.

from .symbol_resolver import SymbolExpression as ParsedExpression, parse_expression


# ============================================================
# Traversal Chain — 每层产生可追溯记录
# ============================================================

@dataclass
class TraversalStep:
    """类型系统遍历链中的一步"""
    step_type: str  # root_variable | datatype | member_variable | external_datatype | alias_candidate | unresolved_member
    name: str
    entity_id: str = ''
    entity_type: str = ''
    display_name: str = ''
    scope: str = ''
    documentation: str = ''
    derived_type: str = ''
    evidence: list = field(default_factory=list)  # 该步的证据
    is_external: bool = False  # 是否为外部库类型 (0 members)
    alias_target: str = ''  # 别名解析目标 (如果有)
    alias_confidence: str = ''  # exact | structural | candidate
    path_index: int = -1  # 在 expr.path 中的位置 (-1 = root/datatype)


@dataclass
class TraversalChain:
    """完整的类型系统遍历链"""
    steps: list = field(default_factory=list)

    @property
    def root_step(self) -> Optional[TraversalStep]:
        for s in self.steps:
            if s.step_type == 'root_variable':
                return s
        return None

    @property
    def datatype_steps(self) -> list:
        return [s for s in self.steps if s.step_type == 'datatype']

    @property
    def member_steps(self) -> list:
        return [s for s in self.steps if s.step_type == 'member_variable']

    @property
    def external_steps(self) -> list:
        return [s for s in self.steps if s.step_type == 'external_datatype']

    @property
    def alias_steps(self) -> list:
        return [s for s in self.steps if s.step_type == 'alias_candidate']

    @property
    def unresolved_steps(self) -> list:
        return [s for s in self.steps if s.step_type == 'unresolved_member']


# ============================================================
# Field Evidence — 每个字段独立记录证据 (v2 with evidence_level)
# ============================================================
# Using FieldEvidenceV2 from evidence_levels.py
FieldEvidence = FieldEvidenceV2  # alias for backward compat


# ============================================================
# ActionSemanticFact — v4 完整 schema (with evidence levels)
# ============================================================

@dataclass
class ActionSemanticFact:
    """Action 语义事实 — 每个字段独立记录证据 + 证据等级

    Evidence levels per field:
      exact_source_fact          — 直接来自源码/HMI事实
      validated_structural_semantic — 结构规律全局验证 (project convention)
      contextual_semantic        — 上下文/命名约定推断
      unresolved                 — 证据不足
    """
    raw_action: str = ''

    # 解析层级标志 (独立, 非互斥)
    expression_parsed: bool = False
    root_resolved: bool = False
    object_resolved: bool = False
    member_resolved: bool = False
    command_resolved: bool = False
    physical_semantic_resolved: bool = False
    position_semantic_resolved: bool = False

    # 语义字段 (各自独立证据 + evidence_level)
    station: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)
    actor: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)
    actor_type: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)
    operation: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)
    direction_state: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)
    target: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)
    position_index: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)
    position_name: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)
    command_member: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)

    # Role classification (physical_actuator_command / process_command / internal_logic / status_flag / alarm / routing_control / unknown)
    action_role: FieldEvidenceV2 = field(default_factory=FieldEvidenceV2)

    # 复合字段
    parameters: list = field(default_factory=list)
    io_addresses: list = field(default_factory=list)

    # 元信息
    traversal_chain: list = field(default_factory=list)
    evidence_by_field: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    unresolved_fields: list = field(default_factory=list)
    external_type_encountered: bool = False  # 是否遇到外部类型

    def to_dict(self) -> dict:
        return {
            'raw_action': self.raw_action,
            'resolution_flags': {
                'expression_parsed': self.expression_parsed,
                'root_resolved': self.root_resolved,
                'object_resolved': self.object_resolved,
                'member_resolved': self.member_resolved,
                'command_resolved': self.command_resolved,
                'physical_semantic_resolved': self.physical_semantic_resolved,
                'position_semantic_resolved': self.position_semantic_resolved,
            },
            'station': self.station.to_dict(),
            'actor': self.actor.to_dict(),
            'actor_type': self.actor_type.to_dict(),
            'operation': self.operation.to_dict(),
            'direction_state': self.direction_state.to_dict(),
            'target': self.target.to_dict(),
            'position_index': self.position_index.to_dict(),
            'position_name': self.position_name.to_dict(),
            'command_member': self.command_member.to_dict(),
            'action_role': self.action_role.to_dict(),
            'parameters': self.parameters,
            'io_addresses': self.io_addresses,
            'traversal_chain': self.traversal_chain,
            'evidence_by_field': self.evidence_by_field,
            'conflicts': self.conflicts,
            'unresolved_fields': self.unresolved_fields,
            'external_type_encountered': self.external_type_encountered,
        }


# ============================================================
# ActionSemanticResolver — 核心解析器
# ============================================================

class ActionSemanticResolver:
    """Action 语义解析器 v4

    Spec-driven + evidence-level-aware.
    - datatype families read from project_semantic_spec (not hardcoded)
    - command naming conventions read from spec (not hardcoded)
    - alias resolution uses validated DatatypeAliasHypothesis only
    - every field records evidence_level

    用法:
        resolver = ActionSemanticResolver(store, spec=spec, doc_decomposer=decomposer,
                                          command_hypotheses=hyps, validated_aliases=aliases)
        fact = resolver.resolve('EM06V.Axis_Z1.In.Location_Auto[1]')
    """

    def __init__(self, store, doc_decomposer=None, command_hypotheses=None,
                 spec=None, validated_aliases=None):
        """
        Args:
            store: EvidenceStore 实例
            doc_decomposer: 可选的 documentation 分解器
            command_hypotheses: CommandSemanticHypothesis 查找表
            spec: project_semantic_spec dict (datatype_families, command_naming_conventions, etc.)
            validated_aliases: dict of validated alias hypotheses {source_type: target_type}
        """
        self.store = store
        self.doc_decomposer = doc_decomposer
        self.command_hypotheses = command_hypotheses or {}
        self.spec = spec or {}
        self.validated_aliases = validated_aliases or {}

        # 预构建索引: datatype_name -> members list
        self._dt_members_index = self._build_datatype_member_index()
        # 预构建索引: member_name -> [datatype_names that contain it]
        self._member_to_dts_index = self._build_member_to_datatypes_index()
        # 预构建索引: external types (0 members but referenced)
        self._external_types = self._identify_external_types()
        # 预构建关系索引 (避免线性扫描)
        self._relations_from_index = self._build_relations_from_index()
        self._relations_to_index = self._build_relations_to_index()
        # 预构建 documentation 索引: entity_id -> doc_text
        self._doc_cache = self._build_doc_cache()
        # 预构建 used_by (datatype) 索引: entity_id -> datatype_name
        self._used_dt_cache = self._build_used_dt_cache()

        # Build datatype family lookup from spec
        self._datatype_family_lookup = self._build_datatype_family_lookup()
        # Build command convention lookup from spec
        self._command_convention_lookup = self._build_command_convention_lookup()
        # Build role classification lookup from spec
        self._role_classification_lookup = self._build_role_classification_lookup()

    def resolve(self, expr_str: str) -> ActionSemanticFact:
        """解析一个 action 表达式为 ActionSemanticFact"""
        fact = ActionSemanticFact(raw_action=expr_str)

        # Step 1: Parse expression
        expr = parse_expression(expr_str)
        fact.expression_parsed = True

        # Step 2: Traverse type chain
        chain = self._traverse(expr)
        fact.traversal_chain = [self._step_to_dict(s) for s in chain.steps]

        # Step 3: Extract semantic fields from traversal chain
        self._extract_fields(fact, chain, expr)

        # Step 4: Compute resolution flags
        self._compute_resolution_flags(fact, chain, expr)

        # Step 5: Compute conflicts and unresolved
        self._compute_conflicts_and_unresolved(fact)

        return fact

    def resolve_batch(self, expressions: list) -> dict:
        """批量解析"""
        results = []
        flag_counts = {
            'expression_parsed': 0,
            'root_resolved': 0,
            'object_resolved': 0,
            'member_resolved': 0,
            'command_resolved': 0,
            'physical_semantic_resolved': 0,
            'position_semantic_resolved': 0,
        }
        for expr in expressions:
            fact = self.resolve(expr)
            results.append(fact)
            for flag in flag_counts:
                if getattr(fact, flag):
                    flag_counts[flag] += 1
        return {
            'total': len(expressions),
            'resolution_flags': flag_counts,
            'facts': results,
        }

    # ============================================================
    # Traversal
    # ============================================================

    def _traverse(self, expr: ParsedExpression) -> TraversalChain:
        """完整 nested datatype/member traversal"""
        chain = TraversalChain()

        # Step 1: Find root variable
        root_entities = self.store.find_by_key('plc_variable', expr.root)
        if not root_entities:
            chain.steps.append(TraversalStep(
                step_type='unresolved_member',
                name=expr.root,
                path_index=-1,
            ))
            return chain

        root_entity = root_entities[0]
        root_step = self._make_step('root_variable', root_entity, path_index=-1)
        root_step.evidence = self._collect_evidence(root_entity)
        root_step.documentation = self._get_documentation(root_entity)
        chain.steps.append(root_step)

        # Step 2: Find root's datatype
        root_dt = self._find_used_datatype(root_entity)
        if not root_dt:
            return chain
        dt_step = self._make_step('datatype', root_dt, path_index=-1)
        chain.steps.append(dt_step)

        # Step 3: Traverse path members
        current_dt = root_dt
        for i, member_name in enumerate(expr.path):
            member_entity = self._find_member_in_datatype(current_dt, member_name)

            if member_entity is None:
                # Try alias resolution on current_dt
                alias_dt = self._try_alias_resolution(current_dt, member_name)
                if alias_dt:
                    alias_step = TraversalStep(
                        step_type='alias_candidate',
                        name=alias_dt.key,
                        entity_id=alias_dt.entity_id,
                        entity_type=alias_dt.entity_type,
                        display_name=alias_dt.display_name,
                        alias_target=alias_dt.key,
                        alias_confidence='structural',
                        path_index=i,
                    )
                    chain.steps.append(alias_step)
                    # Try finding member in alias target
                    member_entity = self._find_member_in_datatype(alias_dt, member_name)
                    current_dt = alias_dt

                if member_entity is None:
                    chain.steps.append(TraversalStep(
                        step_type='unresolved_member',
                        name=member_name,
                        path_index=i,
                    ))
                    break

            member_step = self._make_step('member_variable', member_entity, path_index=i)
            member_step.evidence = self._collect_evidence(member_entity)
            member_step.documentation = self._get_documentation(member_entity)
            member_step.derived_type = self._get_derived_type(member_entity)
            chain.steps.append(member_step)

            # Find member's datatype for next iteration
            member_dt = self._find_used_datatype(member_entity)
            if member_dt:
                if self._is_external_type(member_dt):
                    ext_step = self._make_step('external_datatype', member_dt, path_index=i)
                    ext_step.is_external = True
                    chain.steps.append(ext_step)
                    # Try alias resolution for the external type
                    remaining_path = expr.path[i + 1:]
                    if remaining_path:
                        alias_dt = self._try_alias_resolution(member_dt, remaining_path[0])
                        if alias_dt:
                            alias_step = TraversalStep(
                                step_type='alias_candidate',
                                name=alias_dt.key,
                                entity_id=alias_dt.entity_id,
                                entity_type=alias_dt.entity_type,
                                display_name=alias_dt.display_name,
                                alias_target=alias_dt.key,
                                alias_confidence='candidate',
                                path_index=i,
                            )
                            chain.steps.append(alias_step)
                            current_dt = alias_dt
                        else:
                            current_dt = member_dt  # continue with external type (will likely fail)
                    else:
                        current_dt = member_dt
                else:
                    dt_step2 = self._make_step('datatype', member_dt, path_index=i)
                    chain.steps.append(dt_step2)
                    current_dt = member_dt
            else:
                break  # no sub-datatype, stop

        return chain

    # ============================================================
    # Field Extraction
    # ============================================================

    def _extract_fields(self, fact: ActionSemanticFact, chain: TraversalChain, expr: ParsedExpression):
        """从 traversal chain 提取语义字段 — with evidence levels"""

        # --- station (from root documentation) ---
        root_step = chain.root_step
        if root_step and root_step.documentation:
            # Root variable has documentation → exact_source_fact
            fact.station.set_exact(
                value=root_step.documentation,
                evidence=[{
                    'type': 'documentation',
                    'value': root_step.documentation,
                    'source': 'root_variable_documentation',
                }],
                method='root_variable_documentation'
            )
        elif root_step and root_step.entity_id:
            # Try belongs_to relation
            for r in self._relations_from_index.get((root_step.entity_id, 'belongs_to'), []):
                tgt = self.store.get_entity(r.target_id)
                if tgt and tgt.entity_type == 'station_thread':
                    fact.station.set_contextual(
                        value=tgt.display_name or tgt.key,
                        evidence=[{
                            'type': 'station',
                            'value': tgt.display_name or tgt.key,
                            'source': 'belongs_to',
                        }],
                        method='belongs_to_relation'
                    )
                    break
            if not fact.station.resolved:
                # Try pou name pattern
                for r in self._relations_from_index.get((root_step.entity_id, 'belongs_to'), []):
                    tgt = self.store.get_entity(r.target_id)
                    if tgt and tgt.entity_type == 'pou':
                        station_code = self._extract_station_code(tgt.key)
                        if station_code:
                            fact.station.set_contextual(
                                value=station_code,
                                evidence=[{
                                    'type': 'pou_name',
                                    'value': station_code,
                                    'source': 'pou_name_pattern',
                                }],
                                method='pou_name_pattern'
                            )
                            break

        # --- actor (from first path member documentation) ---
        member_steps = chain.member_steps
        if member_steps:
            first_member = member_steps[0]
            if first_member.documentation:
                # Decompose documentation if decomposer is available
                if self.doc_decomposer:
                    decomposed = self.doc_decomposer.decompose(first_member.documentation)
                    fact.actor.set_exact(
                        value=decomposed.get('actor_name', first_member.documentation),
                        evidence=[{
                            'type': 'documentation',
                            'value': first_member.documentation,
                            'source': 'member_variable_documentation',
                            'decomposed': True,
                            'decomposition_pattern': decomposed.get('pattern_type', ''),
                        }],
                        method='documentation_decomposed',
                        validator_result={'decomposed': True, 'pattern': decomposed.get('pattern_type', '')}
                    )
                    # Extract io_addresses from decomposed doc
                    for addr in decomposed.get('feedback_addresses', []):
                        fact.io_addresses.append({
                            'address': addr, 'type': 'feedback', 'role': 'documentation_extracted',
                        })
                    for addr in decomposed.get('output_addresses', []):
                        fact.io_addresses.append({
                            'address': addr, 'type': 'output', 'role': 'documentation_extracted',
                        })
                    # Extract direction_state from decomposed doc
                    commands = decomposed.get('command_states', [])
                    if commands:
                        fact.direction_state.set_exact(
                            value=commands,
                            evidence=[{
                                'type': 'documentation',
                                'value': commands,
                                'source': 'member_variable_documentation',
                                'decomposed': True,
                            }],
                            method='documentation_decomposed',
                            validator_result={'decomposed': True}
                        )
                else:
                    # No decomposer — use raw documentation
                    fact.actor.set_exact(
                        value=first_member.documentation,
                        evidence=[{
                            'type': 'documentation',
                            'value': first_member.documentation,
                            'source': 'member_variable_documentation',
                            'decomposed': False,
                        }],
                        method='documentation_raw'
                    )
            elif first_member.entity_id:
                # No documentation — member name only, contextual
                fact.actor.set_contextual(
                    value=first_member.name,
                    evidence=[{
                        'type': 'member_name',
                        'value': first_member.name,
                        'source': 'datatype_member',
                    }],
                    method='member_name'
                )

            # --- actor_type (from datatype family spec) ---
            actor_dt = first_member.derived_type
            if actor_dt:
                actor_type = self._infer_actor_type(actor_dt, first_member.name)
                if actor_type and actor_type != 'unknown':
                    # actor_type from spec = validated_structural_semantic (project convention)
                    fact.actor_type.set_validated(
                        value=actor_type,
                        evidence=[{
                            'type': 'datatype_family_spec',
                            'value': actor_type,
                            'source': f'spec:datatype_families, derived_type={actor_dt}',
                            'convention_type': 'project_specific',
                        }],
                        method='datatype_family_spec',
                        validator_result={'spec_source': 'project_semantic_spec'}
                    )
                elif actor_type == 'unknown':
                    fact.actor_type.set_contextual(
                        value='unknown',
                        evidence=[{
                            'type': 'datatype_family',
                            'value': 'unknown',
                            'source': f'derived_type={actor_dt} (no spec match)',
                        }],
                        method='no_spec_match'
                    )

        # --- command_member (last path member) ---
        if len(expr.path) >= 1:
            last_member_name = expr.path[-1]
            fact.command_member.value = last_member_name
            last_member_steps = [s for s in member_steps if s.path_index == len(expr.path) - 1]
            if last_member_steps:
                cmd_step = last_member_steps[0]
                # command_member resolved through type chain
                # If resolved through alias, it's validated_structural (alias is validated)
                # If resolved directly, it's exact (source fact)
                alias_used = any(s.step_type == 'alias_candidate' for s in chain.steps
                                 if s.path_index == len(expr.path) - 2)

                if alias_used:
                    # Get alias evidence level
                    # Find the external type that was aliased
                    external_steps = [s for s in chain.steps if s.step_type == 'external_datatype']
                    alias_level = PLAUSIBLE_STRUCTURAL_HYPOTHESIS  # default
                    if external_steps:
                        alias_level = self._get_alias_evidence_level(external_steps[-1].name)

                    if alias_level == VALIDATED_STRUCTURAL_SEMANTIC:
                        fact.command_member.set_validated(
                            value=last_member_name,
                            evidence=[{
                                'type': 'datatype_member_via_alias',
                                'value': last_member_name,
                                'source': f'datatype:{cmd_step.scope} (via validated alias)',
                                'alias_evidence_level': 'validated',
                            }],
                            method='datatype_member_via_validated_alias',
                            validator_result={'alias_evidence_level': 'validated'}
                        )
                    else:
                        fact.command_member.set_plausible(
                            value=last_member_name,
                            evidence=[{
                                'type': 'datatype_member_via_alias',
                                'value': last_member_name,
                                'source': f'datatype:{cmd_step.scope} (via plausible alias)',
                                'alias_evidence_level': 'plausible',
                            }],
                            method='datatype_member_via_plausible_alias',
                            validator_result={'alias_evidence_level': 'plausible'}
                        )
                else:
                    fact.command_member.set_exact(
                        value=last_member_name,
                        evidence=[{
                            'type': 'datatype_member',
                            'value': last_member_name,
                            'source': f'datatype:{cmd_step.scope}',
                        }],
                        method='datatype_member'
                    )

                # If command member has documentation → operation = exact_source_fact
                if cmd_step.documentation:
                    fact.operation.set_exact(
                        value=cmd_step.documentation,
                        evidence=[{
                            'type': 'documentation',
                            'value': cmd_step.documentation,
                            'source': 'command_member_documentation',
                        }],
                        method='command_member_documentation'
                    )
                    fact.physical_semantic_resolved = True
            else:
                fact.command_member.set_unresolved()

        # --- operation (from command hypothesis if not from doc) ---
        if not fact.operation.resolved and fact.command_member.value:
            cmd_hyp = self._lookup_command_hypothesis(chain, fact.command_member.value)
            if cmd_hyp:
                hyp_level = cmd_hyp.get('evidence_level', 'plausible')
                # operation evidence level = hypothesis evidence level
                if hyp_level == 'validated':
                    fact.operation.set_validated(
                        value=cmd_hyp.get('operation', ''),
                        evidence=[{
                            'type': 'command_hypothesis',
                            'value': cmd_hyp.get('operation', ''),
                            'source': cmd_hyp.get('hypothesis_key', ''),
                            'evidence_level': 'validated',
                            'consistency_rate': cmd_hyp.get('consistency_rate', 0),
                            'holdout_support': cmd_hyp.get('holdout_support', 0),
                        }],
                        method='command_hypothesis_validated',
                        validator_result={
                            'hypothesis_key': cmd_hyp.get('hypothesis_key', ''),
                            'consistency_rate': cmd_hyp.get('consistency_rate', 0),
                            'holdout_support': cmd_hyp.get('holdout_support', 0),
                        }
                    )
                else:
                    # plausible hypothesis
                    fact.operation.set_plausible(
                        value=cmd_hyp.get('operation', ''),
                        evidence=[{
                            'type': 'command_hypothesis',
                            'value': cmd_hyp.get('operation', ''),
                            'source': cmd_hyp.get('hypothesis_key', ''),
                            'evidence_level': 'plausible',
                            'consistency_rate': cmd_hyp.get('consistency_rate', 0),
                        }],
                        method='command_hypothesis_plausible',
                        validator_result={
                            'hypothesis_key': cmd_hyp.get('hypothesis_key', ''),
                            'evidence_level': 'plausible',
                        }
                    )

                # === Weakest-link rule ===
                # If operation depends on command_member which was resolved via plausible alias,
                # operation cannot be stronger than plausible
                if fact.command_member.evidence_level == PLAUSIBLE_STRUCTURAL_HYPOTHESIS:
                    fact.operation.apply_weakest_link(
                        PLAUSIBLE_STRUCTURAL_HYPOTHESIS,
                        'command_member resolved via plausible alias'
                    )

        # --- direction_state (from command name convention, if not from doc) ---
        if not fact.direction_state.resolved and fact.command_member.value:
            direction = self._infer_direction_from_command(fact.command_member.value)
            if direction:
                # direction from naming convention = contextual_semantic
                fact.direction_state.set_contextual(
                    value=direction,
                    evidence=[{
                        'type': 'command_naming_convention',
                        'value': direction,
                        'source': f'spec:command_naming_conventions, command_member={fact.command_member.value}',
                        'convention_type': 'project_specific',
                    }],
                    method='command_naming_convention_spec'
                )

        # --- position_index (from array index) ---
        if expr.index is not None:
            if isinstance(expr.index, int):
                fact.position_index.set_exact(
                    value=expr.index,
                    evidence=[{
                        'type': 'array_index',
                        'value': expr.index,
                        'source': 'expression',
                    }],
                    method='index_parse'
                )
            elif isinstance(expr.index, tuple):
                fact.position_index.set_exact(
                    value=list(expr.index),
                    evidence=[{
                        'type': 'array_index',
                        'value': list(expr.index),
                        'source': 'expression',
                    }],
                    method='index_parse'
                )
        elif expr.index_var:
            fact.position_index.value = expr.index_var
            fact.position_index.resolved = False
            fact.position_index.evidence_level = CONTEXTUAL_SEMANTIC
            fact.position_index.resolution_method = 'variable_index'

        # --- position_name (needs real evidence) ---
        self._resolve_position_name(fact, chain, expr)

        # --- io_addresses (from maps_to relations) ---
        for step in member_steps:
            if step.entity_id:
                for r in self._relations_from_index.get((step.entity_id, 'maps_to'), []):
                    addr = self.store.get_entity(r.target_id)
                    if addr:
                        existing = [a for a in fact.io_addresses if a['address'] == addr.key]
                        if not existing:
                            fact.io_addresses.append({
                                'address': addr.key,
                                'type': 'output' if addr.key.startswith('Y') else 'feedback' if addr.key.startswith('X') else 'unknown',
                                'role': 'maps_to_relation',
                            })

        # --- action_role (from spec role classification) ---
        actor_type_val = fact.actor_type.value if fact.actor_type.resolved else ''
        role = self._classify_action_role(expr, actor_type_val)
        if role != 'unknown':
            fact.action_role.set_validated(
                value=role,
                evidence=[{
                    'type': 'role_classification_spec',
                    'value': role,
                    'source': 'spec:role_classification',
                    'convention_type': 'project_specific',
                }],
                method='role_classification_spec'
            )
        else:
            fact.action_role.set_contextual(
                value='unknown',
                evidence=[{
                    'type': 'role_classification',
                    'value': 'unknown',
                    'source': 'no spec match',
                }],
                method='no_spec_match'
            )

        # --- external_type_encountered ---
        if chain.external_steps:
            fact.external_type_encountered = True

        # Build evidence_by_field
        for field_name in ['station', 'actor', 'actor_type', 'operation', 'direction_state',
                           'target', 'position_index', 'position_name', 'command_member',
                           'action_role']:
            fe = getattr(fact, field_name)
            if fe.evidence:
                fact.evidence_by_field[field_name] = {
                    'evidence_level': fe.evidence_level,
                    'evidence': fe.evidence,
                    'method': fe.resolution_method,
                }

    def _resolve_position_name(self, fact: ActionSemanticFact, chain: TraversalChain, expr: ParsedExpression):
        """Resolve position_name — needs real evidence (not just index)"""
        if fact.position_index.value is None:
            return

        # Try to find position name from:
        # 1. Parameter datatype members (e.g., HomePosition, ClampingPosition)
        # 2. HMI labels related to this action
        # 3. Documentation containing position info

        # Check if the actor has a Parameter sub-structure with position definitions
        member_steps = chain.member_steps
        if not member_steps:
            return

        first_member = member_steps[0]
        if not first_member.entity_id:
            return

        # Try to find the actor's Parameter datatype
        actor_dt_name = first_member.derived_type
        if not actor_dt_name:
            return

        # Look for position-related members in the actor's datatype family
        # e.g., ST_BaseAxis_08AxisVar → look for ST_BaseAxis_05Parameter
        # This is structural inference, not project rule
        param_dt_name = self._find_parameter_datatype(actor_dt_name)
        if param_dt_name:
            param_members = self._dt_members_index.get(param_dt_name, [])
            # If we have a position index, try to find a position array member
            for pm_eid, pm in param_members:
                pm_doc = self._get_documentation_by_id(pm_eid)
                if pm_doc and ('位置' in pm_doc or '位' in pm_doc):
                    # This could be a position definition
                    if isinstance(fact.position_index.value, int):
                        # We have an index but no position name mapping
                        # Mark as potential position but unresolved
                        pass

        # Check HMI labels for position info
        for step in member_steps:
            if step.entity_id:
                for r in self._relations_from_index.get((step.entity_id, 'maps_to'), []):
                    addr = self.store.get_entity(r.target_id)
                    if addr:
                        for r2 in self._relations_from_index.get((addr.entity_id, 'labelled_as'), []):
                            label = self.store.get_entity(r2.target_id)
                            if label:
                                label_text = label.display_name or label.key
                                # Check if label contains position info
                                if '位' in label_text and fact.position_index.value is not None:
                                    # This is a potential position name
                                    fact.position_name.value = label_text
                                    fact.position_name.resolved = True
                                    fact.position_name.resolution_method = 'hmi_label'
                                    fact.position_name.evidence = [{
                                        'type': 'hmi_label',
                                        'value': label_text,
                                        'source': f'{addr.key} → {label_text}',
                                        'confidence': 'structural',
                                    }]
                                    fact.position_semantic_resolved = True
                                    return

    # ============================================================
    # Resolution Flags
    # ============================================================

    def _compute_resolution_flags(self, fact: ActionSemanticFact, chain: TraversalChain, expr: ParsedExpression):
        """Compute resolution flags from traversal chain"""
        root_step = chain.root_step
        if root_step and root_step.step_type == 'root_variable':
            fact.root_resolved = True

        member_steps = chain.member_steps
        if member_steps:
            fact.object_resolved = True  # at least first member resolved

        # member_resolved: intermediate members (In/Out interface) resolved
        if len(member_steps) >= 2:
            fact.member_resolved = True

        # command_resolved: last path member resolved
        if expr.path:
            last_idx = len(expr.path) - 1
            last_members = [s for s in member_steps if s.path_index == last_idx]
            if last_members:
                fact.command_resolved = True

        # physical_semantic_resolved: set in _extract_fields if command has documentation
        # (already set)

    def _compute_conflicts_and_unresolved(self, fact: ActionSemanticFact):
        """Compute conflicts and unresolved fields"""
        for field_name in ['station', 'actor', 'actor_type', 'operation',
                           'direction_state', 'target', 'position_index', 'position_name',
                           'command_member']:
            fe = getattr(fact, field_name)
            if not fe.resolved:
                fact.unresolved_fields.append(field_name)

    # ============================================================
    # Helper Methods — Type System Queries
    # ============================================================

    def _build_datatype_member_index(self) -> dict:
        """Build index: datatype_name -> [(entity_id, entity), ...]"""
        index = {}
        for eid, e in self.store.entities.items():
            if e.entity_type == 'plc_variable' and e.scope:
                if e.scope.startswith('datatype:'):
                    dt_name = e.scope.split('/')[0].replace('datatype:', '')
                    index.setdefault(dt_name, []).append((eid, e))
        return index

    def _build_member_to_datatypes_index(self) -> dict:
        """Build index: member_name -> [datatype_names that contain it]"""
        index = {}
        for dt_name, members in self._dt_members_index.items():
            for _, m in members:
                index.setdefault(m.key, []).append(dt_name)
        return index

    def _identify_external_types(self) -> set:
        """Identify datatypes that are referenced but have 0 members (external library types)"""
        external = set()
        # Find all datatype names that are used as derived types
        referenced_dts = set()
        for r in self.store.relations:
            if r.relation_type == 'used_by':
                dt_entity = self.store.get_entity(r.source_id)
                if dt_entity and dt_entity.entity_type == 'datatype':
                    referenced_dts.add(dt_entity.key)

        # External = referenced but has 0 members
        for dt_name in referenced_dts:
            if dt_name not in self._dt_members_index or len(self._dt_members_index[dt_name]) == 0:
                external.add(dt_name)
        return external

    def _build_relations_from_index(self) -> dict:
        """Build index: (entity_id, relation_type) -> [Relation, ...]"""
        index = {}
        for r in self.store.relations:
            key = (r.source_id, r.relation_type)
            index.setdefault(key, []).append(r)
        return index

    def _build_relations_to_index(self) -> dict:
        """Build index: (entity_id, relation_type) -> [Relation, ...]"""
        index = {}
        for r in self.store.relations:
            key = (r.target_id, r.relation_type)
            index.setdefault(key, []).append(r)
        return index

    def _build_doc_cache(self) -> dict:
        """Build cache: entity_id -> documentation_text"""
        cache = {}
        for r in self.store.relations:
            if r.relation_type == 'documented_as':
                tgt = self.store.get_entity(r.target_id)
                if tgt:
                    cache[r.source_id] = tgt.display_name or tgt.key
        return cache

    def _build_used_dt_cache(self) -> dict:
        """Build cache: entity_id -> datatype_entity (via used_by)"""
        cache = {}
        for r in self.store.relations:
            if r.relation_type == 'used_by':
                dt = self.store.get_entity(r.source_id)
                if dt and dt.entity_type == 'datatype':
                    cache[r.target_id] = dt
        return cache

    def _find_used_datatype(self, entity) -> Optional[Any]:
        """Find the datatype that an entity is an instance of (via used_by relation)"""
        return self._used_dt_cache.get(entity.entity_id)

    def _find_member_in_datatype(self, datatype_entity, member_name: str):
        """Find a member variable within a datatype's scope"""
        members = self._dt_members_index.get(datatype_entity.key, [])
        for _, m in members:
            if m.key == member_name:
                return m
        return None

    def _is_external_type(self, datatype_entity) -> bool:
        """Check if a datatype is an external library type (0 members)"""
        return datatype_entity.key in self._external_types

    def _try_alias_resolution(self, datatype_entity, expected_member_name: str):
        """Try to resolve an external/empty datatype using validated OR plausible aliases.

        Alias lookup format: {source_type: {target_type, evidence_level, confidence, member_overlap}}
        Only validated and plausible aliases are used.
        """
        alias_info = self.validated_aliases.get(datatype_entity.key)
        if alias_info:
            target_type = alias_info.get('target_type', '') if isinstance(alias_info, dict) else alias_info
            if target_type:
                dt_entities = self.store.find_by_key('datatype', target_type)
                if dt_entities:
                    return dt_entities[0]
        return None

    def _get_alias_evidence_level(self, datatype_name: str) -> str:
        """Get the evidence level of an alias used for this datatype."""
        alias_info = self.validated_aliases.get(datatype_name)
        if alias_info and isinstance(alias_info, dict):
            level = alias_info.get('evidence_level', '')
            if level == 'validated':
                return VALIDATED_STRUCTURAL_SEMANTIC
            elif level == 'plausible':
                return PLAUSIBLE_STRUCTURAL_HYPOTHESIS
        return UNRESOLVED

    def _name_similarity(self, name1: str, name2: str) -> float:
        """Compute name similarity score (0-1) for alias resolution.

        Uses token overlap and prefix/suffix matching.
        Not project-specific — general string similarity.
        """
        if name1 == name2:
            return 1.0

        # Extract tokens (split by _ and case boundaries)
        def tokenize(s):
            tokens = re.findall(r'[A-Z][a-z]*|[a-z]+|[A-Z]+|[0-9]+', s)
            return [t.lower() for t in tokens]

        tokens1 = set(tokenize(name1))
        tokens2 = set(tokenize(name2))

        if not tokens1 or not tokens2:
            return 0.0

        # Jaccard similarity
        intersection = tokens1 & tokens2
        union = tokens1 | tokens2
        jaccard = len(intersection) / len(union)

        # Boost if suffix matches (e.g., both end with "In")
        if name1.endswith('In') and name2.endswith('In'):
            jaccard += 0.2
        if name1.endswith('Out') and name2.endswith('Out'):
            jaccard += 0.2
        if name1.endswith('Var') and name2.endswith('Var'):
            jaccard += 0.1

        return min(jaccard, 1.0)

    def _find_parameter_datatype(self, actor_dt_name: str) -> Optional[str]:
        """Find the parameter datatype for a given actor datatype.

        e.g., ST_BaseAxis_08AxisVar → ST_BaseAxis_05Parameter

        This uses the naming convention of the datatype family (numeric prefix + role suffix),
        which is a structural pattern in the type system, not a project-specific rule.
        """
        # Extract the family prefix (e.g., "ST_BaseAxis" from "ST_BaseAxis_08AxisVar")
        m = re.match(r'^(ST_[A-Za-z]+)_\d+(\w+)$', actor_dt_name)
        if m:
            family_prefix = m.group(1)
            # Look for a Parameter type in the same family
            for dt_name in self._dt_members_index:
                if dt_name.startswith(family_prefix) and 'Parameter' in dt_name:
                    return dt_name
        return None

    def _infer_actor_type(self, derived_type: str, member_name: str = '') -> str:
        """Infer actor_type from datatype family — spec-driven.

        Reads datatype_families from project_semantic_spec.
        Returns 'unknown' if no spec match (does NOT fall back to hardcoded patterns).
        """
        # Check spec-driven family lookup
        for pattern, family in self._datatype_family_lookup.items():
            if pattern in derived_type:
                # More specific match: clamp is subset of cylinder
                if family == 'clamp' and 'CyClamp' in derived_type:
                    return 'clamp'
                if family == 'cylinder' and 'CyClamp' in derived_type:
                    continue  # skip, let clamp match
                if family == 'blow' and 'Blow' in derived_type:
                    return 'blow'
                return family

        # No spec match — return unknown (do NOT use hardcoded member name patterns)
        return 'unknown'

    def _infer_direction_from_command(self, command_member: str) -> str:
        """Infer direction/state from command member name — spec-driven.

        Reads command_naming_conventions from project_semantic_spec.
        Returns '' if no spec match (does NOT use hardcoded _R/_V patterns).
        """
        cmd_lower = command_member.lower()

        # Check spec-driven suffix conventions
        for suffix, direction in self._command_convention_lookup.get('suffixes', []):
            if cmd_lower.endswith(suffix.lower()):
                return direction

        # Check spec-driven keyword conventions
        for keyword, direction in self._command_convention_lookup.get('keywords', []):
            if keyword.lower() in cmd_lower:
                return direction

        return ''

    def _build_datatype_family_lookup(self) -> dict:
        """Build datatype family lookup from spec.

        Returns: {pattern: semantic_family}
        """
        lookup = {}
        for family in self.spec.get('datatype_families', []):
            pattern = family.get('pattern', '')
            semantic_family = family.get('semantic_family', '')
            if pattern and semantic_family:
                lookup[pattern] = semantic_family
        return lookup

    def _build_command_convention_lookup(self) -> dict:
        """Build command naming convention lookup from spec.

        Returns: {'suffixes': [(suffix, direction), ...], 'keywords': [(keyword, direction), ...]}
        """
        suffixes = []
        keywords = []
        for conv in self.spec.get('command_naming_conventions', []):
            suffix = conv.get('suffix', '')
            keyword = conv.get('member_keyword', '')
            direction = conv.get('direction', '')
            if suffix and direction:
                suffixes.append((suffix, direction))
            if keyword and direction:
                keywords.append((keyword, direction))
        return {'suffixes': suffixes, 'keywords': keywords}

    def _build_role_classification_lookup(self) -> dict:
        """Build role classification lookup from spec.

        Returns: {signal_pattern: role}
        """
        lookup = {}
        for role_spec in self.spec.get('role_classification', []):
            signal_pattern = role_spec.get('signal_pattern', '')
            role = role_spec.get('role', '')
            if signal_pattern and role:
                lookup[signal_pattern] = role
        return lookup

    def _classify_action_role(self, expr: ParsedExpression, actor_type: str = '') -> str:
        """Classify action role from spec.

        Returns: physical_actuator_command / process_command / internal_logic /
                 status_flag / alarm / routing_control / unknown
        """
        # If actor_type is a physical type, it's a physical_actuator_command
        physical_types = {'axis', 'cylinder', 'clamp', 'vacuum', 'motor', 'robot', 'sensor', 'blow'}
        if actor_type in physical_types:
            return 'physical_actuator_command'

        # Check command member against role classification patterns
        if expr.path:
            cmd = expr.path[-1].split('[')[0]
            for pattern, role in self._role_classification_lookup.items():
                # Support glob-style patterns
                if '*' in pattern:
                    # Convert glob to regex
                    regex_pattern = pattern.replace('*', '.*')
                    if re.match(regex_pattern, cmd):
                        return role
                elif pattern in cmd:
                    return role

        # Check root variable
        root = expr.root
        for pattern, role in self._role_classification_lookup.items():
            if '*' in pattern:
                regex_pattern = pattern.replace('*', '.*')
                if re.match(regex_pattern, root):
                    return role
            elif pattern in root:
                return role

        return 'unknown'

    def _lookup_command_hypothesis(self, chain: TraversalChain, command_member: str) -> dict:
        """Look up command semantic hypothesis for a command member.

        Key: (datatype_family, member_name)
        """
        # Determine datatype family from the chain
        member_steps = chain.member_steps
        if not member_steps:
            return {}

        first_member = member_steps[0]
        actor_dt = first_member.derived_type
        if not actor_dt:
            return {}

        actor_type = self._infer_actor_type(actor_dt, first_member.name)

        # Try lookup with various keys
        for key in [
            (actor_dt, command_member),
            (actor_type, command_member),
            (command_member, ''),
        ]:
            key_str = '{}::{}'.format(key[0], key[1]) if key[1] else key[0]
            if key_str in self.command_hypotheses:
                return self.command_hypotheses[key_str]

        return {}

    def _collect_evidence(self, entity) -> list:
        """Collect all evidence for an entity"""
        evidence = []
        eid = entity.entity_id
        # documentation
        for r in self._relations_from_index.get((eid, 'documented_as'), []):
            tgt = self.store.get_entity(r.target_id)
            if tgt:
                evidence.append({
                    'type': 'documentation',
                    'value': tgt.display_name or tgt.key,
                    'confidence': r.confidence,
                })
        # maps_to (address)
        for r in self._relations_from_index.get((eid, 'maps_to'), []):
            tgt = self.store.get_entity(r.target_id)
            if tgt:
                evidence.append({'type': 'address', 'value': tgt.key})
                for r2 in self._relations_from_index.get((tgt.entity_id, 'labelled_as'), []):
                    label = self.store.get_entity(r2.target_id)
                    if label:
                        evidence.append({
                            'type': 'hmi_label',
                            'value': label.display_name or label.key,
                        })
        # belongs_to (station)
        for r in self._relations_from_index.get((eid, 'belongs_to'), []):
            tgt = self.store.get_entity(r.target_id)
            if tgt:
                evidence.append({
                    'type': 'station',
                    'value': tgt.display_name or tgt.key,
                    'entity_type': tgt.entity_type,
                })
        # HMI binds_to
        for r in self._relations_to_index.get((eid, 'binds_to'), []):
            tgt = self.store.get_entity(r.source_id)
            if tgt:
                evidence.append({
                    'type': 'hmi_component',
                    'value': tgt.display_name or tgt.key,
                })
        return evidence

    def _get_documentation(self, entity) -> str:
        """Get documentation text for an entity"""
        return self._doc_cache.get(entity.entity_id, '')

    def _get_documentation_by_id(self, entity_id: str) -> str:
        """Get documentation text for an entity by ID"""
        return self._doc_cache.get(entity_id, '')

    def _get_derived_type(self, entity) -> str:
        """Get the derived type name for an entity"""
        dt = self._used_dt_cache.get(entity.entity_id)
        if dt:
            return dt.key
        return ''

    def _extract_station_code(self, pou_name: str) -> str:
        """Extract station code (e.g., EM06) from POU name"""
        m = re.match(r'([A-Z]{2}\d{2})', pou_name)
        return m.group(1) if m else ''

    def _make_step(self, step_type: str, entity, path_index: int = -1) -> TraversalStep:
        """Create a TraversalStep from an entity"""
        return TraversalStep(
            step_type=step_type,
            name=entity.key,
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            display_name=entity.display_name,
            scope=entity.scope,
            path_index=path_index,
        )

    def _step_to_dict(self, step: TraversalStep) -> dict:
        """Serialize a TraversalStep to dict"""
        return {
            'step_type': step.step_type,
            'name': step.name,
            'entity_id': step.entity_id,
            'entity_type': step.entity_type,
            'display_name': step.display_name,
            'scope': step.scope,
            'documentation': step.documentation,
            'derived_type': step.derived_type,
            'is_external': step.is_external,
            'alias_target': step.alias_target,
            'alias_confidence': step.alias_confidence,
            'path_index': step.path_index,
            'evidence': step.evidence,
        }
