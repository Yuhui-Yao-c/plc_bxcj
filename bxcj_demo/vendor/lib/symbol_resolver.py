#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/symbol_resolver.py — 表达式/符号解析器

将 PLC 变量路径表达式解析为结构化组件，并在 EvidenceStore 中查找证据。

支持的表达式形式:
    EM06V.Axis_Z1.In.Location_Auto[1]
    → {root: 'EM06V', path: ['Axis_Z1', 'In', 'Location_Auto'], index: 1}

    EM06V.Cy_Clamp1.In.Auto_R
    → {root: 'EM06V', path: ['Cy_Clamp1', 'In', 'Auto_R'], index: None}

    Cell_Info[6,2].ProtectCoverScanFinish
    → {root: 'Cell_Info', path: ['ProtectCoverScanFinish'], index: (6, 2)}

    H_Messages.General_Warning[15]
    → {root: 'H_Messages', path: ['General_Warning'], index: 15}

解析优先级:
    1. exact qualified identity — 直接在 EvidenceStore 中查找完整路径
    2. scope-aware structural resolution — 按 root/path 分解查找
    3. explicit reference relation — 通过 FB 参数关系查找
    4. validated alias relation — 通过别名查找
    5. candidate/fuzzy matching — 模糊匹配 (仅产生候选, 不是事实)
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SymbolExpression:
    """解析后的变量路径表达式"""
    raw: str                    # 原始表达式
    root: str                   # 根变量 (如 EM06V, Cell_Info, H_Messages)
    path: list = field(default_factory=list)  # 成员路径 (如 ['Axis_Z1', 'In', 'Location_Auto'])
    index: object = None        # 数组索引: int, tuple, 或 None
    index_var: str = ''         # 变量索引 (如 [EM06V.AxisMoveNumber])

    @property
    def qualified_name(self) -> str:
        """不含索引的限定名"""
        parts = [self.root] + self.path
        return '.'.join(parts)

    @property
    def full_name(self) -> str:
        """含索引的完整名"""
        base = self.qualified_name
        if self.index is not None:
            if isinstance(self.index, tuple):
                return '{}[{}]'.format(base, ','.join(str(i) for i in self.index))
            return '{}[{}]'.format(base, self.index)
        elif self.index_var:
            return '{}[{}]'.format(base, self.index_var)
        return base


def parse_expression(expr: str) -> SymbolExpression:
    """解析变量路径表达式

    Examples:
        'EM06V.Axis_Z1.In.Location_Auto[1]' → root=EM06V, path=[Axis_Z1, In, Location_Auto], index=1
        'Cell_Info[6,2].ProtectCoverScanFinish' → root=Cell_Info, path=[ProtectCoverScanFinish], index=(6,2)
        'Reset[2]' → root=Reset, path=[], index=2
        'EM06V.Cy_Clamp1.In.Auto_R' → root=EM06V, path=[Cy_Clamp1, In, Auto_R], index=None
    """
    expr = expr.strip()

    # Pattern: root[indices].path  OR  root.path[indices]  OR  root[indices]  OR  root.path
    # Strategy: extract root, then parse the rest

    # Check for root[index] at the start
    m = re.match(r'^(\w+)\[([^\]]*)\]\.?(.*)$', expr)
    if m:
        root = m.group(1)
        index_str = m.group(2)
        rest = m.group(3)
        index, index_var = _parse_index(index_str)
    else:
        # Check for root.path (no initial index)
        m2 = re.match(r'^(\w+)\.(.*)$', expr)
        if m2:
            root = m2.group(1)
            rest = m2.group(2)
            index = None
            index_var = ''
        else:
            # Just root[index] or root
            m3 = re.match(r'^(\w+)\[([^\]]*)\]$', expr)
            if m3:
                root = m3.group(1)
                index, index_var = _parse_index(m3.group(2))
                rest = ''
            else:
                # Plain variable name
                m4 = re.match(r'^(\w+)$', expr)
                if m4:
                    return SymbolExpression(raw=expr, root=m4.group(1), path=[], index=None)
                return SymbolExpression(raw=expr, root=expr, path=[], index=None)

    # Parse rest as path with optional trailing index
    path = []
    if rest:
        # Split by . but handle trailing [index]
        parts = rest.split('.')
        for i, part in enumerate(parts):
            # Check for part[index]
            m_idx = re.match(r'^(\w+)\[([^\]]*)\]$', part)
            if m_idx:
                path.append(m_idx.group(1))
                idx, idx_var = _parse_index(m_idx.group(2))
                # This index applies to the last path element
                # For simplicity, store it on the expression
                if i == len(parts) - 1:
                    index = idx
                    index_var = idx_var
            else:
                path.append(part)

    return SymbolExpression(raw=expr, root=root, path=path, index=index, index_var=index_var)


def _parse_index(index_str: str) -> tuple:
    """解析索引字符串 → (index_value, index_var)"""
    index_str = index_str.strip()
    if not index_str:
        return (None, '')

    # Multi-dimensional: 6,2
    if ',' in index_str:
        parts = [p.strip() for p in index_str.split(',')]
        try:
            return (tuple(int(p) for p in parts), '')
        except ValueError:
            return (None, index_str)

    # Single integer
    try:
        return (int(index_str), '')
    except ValueError:
        # Variable index: EM06V.AxisMoveNumber
        return (None, index_str)


def resolve_symbol(store, expr_str: str) -> dict:
    """在 EvidenceStore 中解析变量表达式

    Args:
        store: EvidenceStore 实例
        expr_str: 变量路径表达式

    Returns:
        {
            'expression': str,
            'parsed': SymbolExpression,
            'resolved': bool,
            'entity': Entity or None,
            'evidence': [...],
            'resolution_level': str,  # exact | structural | reference | alias | candidate | unresolved
            'candidates': [...],  # for candidate level
        }
    """
    expr = parse_expression(expr_str)

    # Level 1: exact qualified identity
    entity = _find_exact(store, expr)
    if entity:
        evidence = _collect_evidence(store, entity)
        return {
            'expression': expr_str,
            'parsed': _expr_to_dict(expr),
            'resolved': True,
            'entity': _entity_brief(entity),
            'evidence': evidence,
            'resolution_level': 'exact',
            'candidates': [],
        }

    # Level 2: scope-aware structural resolution
    entity = _find_structural(store, expr)
    if entity:
        evidence = _collect_evidence(store, entity)
        return {
            'expression': expr_str,
            'parsed': _expr_to_dict(expr),
            'resolved': True,
            'entity': _entity_brief(entity),
            'evidence': evidence,
            'resolution_level': 'structural',
            'candidates': [],
        }

    # Level 3: explicit reference relation (FB params)
    entity = _find_via_reference(store, expr)
    if entity:
        evidence = _collect_evidence(store, entity)
        return {
            'expression': expr_str,
            'parsed': _expr_to_dict(expr),
            'resolved': True,
            'entity': _entity_brief(entity),
            'evidence': evidence,
            'resolution_level': 'reference',
            'candidates': [],
        }

    # Level 4: validated alias relation
    entity = _find_via_alias(store, expr)
    if entity:
        evidence = _collect_evidence(store, entity)
        return {
            'expression': expr_str,
            'parsed': _expr_to_dict(expr),
            'resolved': True,
            'entity': _entity_brief(entity),
            'evidence': evidence,
            'resolution_level': 'alias',
            'candidates': [],
        }

    # Level 5: candidate/fuzzy matching
    candidates = _find_candidates(store, expr)
    return {
        'expression': expr_str,
        'parsed': _expr_to_dict(expr),
        'resolved': False,
        'entity': None,
        'evidence': [],
        'resolution_level': 'candidate' if candidates else 'unresolved',
        'candidates': [_entity_brief(e) for e in candidates[:5]],
    }


def _find_exact(store, expr: SymbolExpression):
    """Level 1: 查找完全匹配的实体"""
    # Try the full expression as key
    entities = store.find_by_key('plc_variable', expr.raw)
    if entities:
        return entities[0]

    # Try qualified_name (without index)
    entities = store.find_by_key('plc_variable', expr.qualified_name)
    if entities:
        return entities[0]

    # Try full_name
    entities = store.find_by_key('plc_variable', expr.full_name)
    if entities:
        return entities[0]

    return None


def _find_structural(store, expr: SymbolExpression):
    """Level 2: 按 root/path 结构查找"""
    # Try just the root
    entities = store.find_by_key('plc_variable', expr.root)
    if entities:
        return entities[0]

    # Try the last path element (most specific)
    if expr.path:
        last = expr.path[-1]
        entities = store.find_by_key('plc_variable', last)
        if entities:
            return entities[0]

    # Try root + first path element
    if expr.path:
        combined = '{}.{}'.format(expr.root, expr.path[0])
        entities = store.find_by_key('plc_variable', combined)
        if entities:
            return entities[0]

    return None


def _find_via_reference(store, expr: SymbolExpression):
    """Level 3: 通过 FB 参数关系查找"""
    # Check if any FB instance has a parameter matching this expression
    # Look for input_param/output_param relations pointing to variables
    # that might match our expression

    # Try finding FB instances with the root as name
    fb_instances = store.find_entities('fb_instance', key=expr.root)
    if fb_instances:
        # Check parameters of this FB instance
        for fb in fb_instances:
            for r in store.find_relations_from(fb.entity_id, 'input_param'):
                tgt = store.get_entity(r.target_id)
                if tgt and expr.path and expr.path[-1] in tgt.key:
                    return tgt
            for r in store.find_relations_from(fb.entity_id, 'output_param'):
                tgt = store.get_entity(r.target_id)
                if tgt and expr.path and expr.path[-1] in tgt.key:
                    return tgt

    return None


def _find_via_alias(store, expr: SymbolExpression):
    """Level 4: 通过别名查找"""
    # Check if there's a variable that's documented_as something containing our expression
    # This is a weaker form of resolution
    if expr.path:
        search_term = expr.path[-1]
        for e in store.entities.values():
            if e.entity_type == 'plc_variable' and search_term == e.key.split('.')[-1]:
                return e
    return None


def _find_candidates(store, expr: SymbolExpression):
    """Level 5: 模糊匹配, 仅产生候选"""
    candidates = []
    search_terms = [expr.root]
    if expr.path:
        search_terms.extend(expr.path)

    for e in store.entities.values():
        if e.entity_type != 'plc_variable':
            continue
        for term in search_terms:
            if term and term in e.key:
                candidates.append(e)
                break

    return candidates[:10]


def _collect_evidence(store, entity) -> list:
    """收集实体的证据链"""
    evidence = []

    # documentation
    for r in store.find_relations_from(entity.entity_id, 'documented_as'):
        tgt = store.get_entity(r.target_id)
        if tgt:
            evidence.append({
                'type': 'documentation',
                'value': tgt.display_name or tgt.key,
                'confidence': r.confidence,
            })

    # maps_to (address)
    for r in store.find_relations_from(entity.entity_id, 'maps_to'):
        tgt = store.get_entity(r.target_id)
        if tgt:
            evidence.append({
                'type': 'address',
                'value': tgt.key,
            })
            # Check for HMI label
            for r2 in store.find_relations_from(tgt.entity_id, 'labelled_as'):
                tgt2 = store.get_entity(r2.target_id)
                if tgt2:
                    evidence.append({
                        'type': 'hmi_label',
                        'value': tgt2.display_name or tgt2.key,
                    })

    # belongs_to (station)
    for r in store.find_relations_from(entity.entity_id, 'belongs_to'):
        tgt = store.get_entity(r.target_id)
        if tgt:
            evidence.append({
                'type': 'station',
                'value': tgt.display_name or tgt.key,
                'entity_type': tgt.entity_type,
            })

    # HMI binds_to
    for r in store.find_relations_to(entity.entity_id, 'binds_to'):
        tgt = store.get_entity(r.source_id)
        if tgt:
            evidence.append({
                'type': 'hmi_component',
                'value': tgt.display_name or tgt.key,
            })

    # FB params
    for r in store.find_relations_to(entity.entity_id, 'input_param'):
        tgt = store.get_entity(r.source_id)
        if tgt:
            evidence.append({
                'type': 'fb_input_param',
                'fb_instance': tgt.key,
            })
    for r in store.find_relations_to(entity.entity_id, 'output_param'):
        tgt = store.get_entity(r.source_id)
        if tgt:
            evidence.append({
                'type': 'fb_output_param',
                'fb_instance': tgt.key,
            })

    return evidence


def _entity_brief(entity) -> dict:
    return {
        'entity_id': entity.entity_id,
        'entity_type': entity.entity_type,
        'key': entity.key,
        'display_name': entity.display_name,
        'scope': entity.scope,
    }


def _expr_to_dict(expr: SymbolExpression) -> dict:
    return {
        'raw': expr.raw,
        'root': expr.root,
        'path': expr.path,
        'index': expr.index,
        'index_var': expr.index_var,
        'qualified_name': expr.qualified_name,
        'full_name': expr.full_name,
    }


def resolve_batch(store, expressions: list) -> dict:
    """批量解析变量表达式

    Returns:
        {
            'total': int,
            'resolved': int,
            'unresolved': int,
            'by_level': {'exact': N, 'structural': N, ...},
            'results': [...],
        }
    """
    results = []
    by_level = {}

    for expr in expressions:
        result = resolve_symbol_v2(store, expr)
        results.append(result)
        level = result['resolution_level']
        by_level[level] = by_level.get(level, 0) + 1

    resolved = sum(1 for r in results if r['resolved'])

    return {
        'total': len(expressions),
        'resolved': resolved,
        'unresolved': len(expressions) - resolved,
        'by_level': by_level,
        'results': results,
    }


# ============================================================
# v2: Member-level resolution with L0-L5 levels
# ============================================================

def _find_datatype_for_root(store, root_var_name):
    """Find the datatype that a root variable (like EM06V) uses.
    
    Path: plc_variable EM06V -[used_by]-> datatype EM06_Variable
    """
    root_entities = store.find_by_key('plc_variable', root_var_name)
    for root_e in root_entities:
        for r in store.find_relations_to(root_e.entity_id, 'used_by'):
            dt = store.get_entity(r.source_id)
            if dt and dt.entity_type == 'datatype':
                return dt
    return None


def _find_member_in_datatype(store, datatype_entity, member_name):
    """Find a member variable within a datatype's scope.
    
    e.g. find 'Axis_Z1' in scope 'datatype:EM06_Variable'
    """
    target_scope = 'datatype:{}'.format(datatype_entity.key)
    entities = store.find_entities('plc_variable', key=member_name, scope=target_scope)
    if entities:
        return entities[0]
    return None


def _resolve_member_chain(store, root_var_name, path_parts):
    """Traverse the member chain: root → datatype → member → sub-datatype → sub-member → ...
    
    Returns list of resolved entities, one per path element that was found.
    """
    resolved_members = []
    
    # Step 1: Find root variable
    root_entities = store.find_by_key('plc_variable', root_var_name)
    if not root_entities:
        return resolved_members
    
    root_entity = root_entities[0]
    resolved_members.append({
        'level': 'root',
        'name': root_var_name,
        'entity': root_entity,
        'evidence': _collect_evidence(store, root_entity),
    })
    
    # Step 2: Find datatype for root
    datatype = _find_datatype_for_root(store, root_var_name)
    if not datatype:
        return resolved_members
    
    resolved_members.append({
        'level': 'datatype',
        'name': datatype.key,
        'entity': datatype,
        'evidence': [],
    })
    
    # Step 3: Traverse path members
    current_datatype = datatype
    for i, member_name in enumerate(path_parts):
        member_entity = _find_member_in_datatype(store, current_datatype, member_name)
        if member_entity:
            member_evidence = _collect_evidence(store, member_entity)
            resolved_members.append({
                'level': 'member_{}'.format(i + 1),
                'name': member_name,
                'entity': member_entity,
                'evidence': member_evidence,
            })
            
            # Check if this member has a sub-datatype (for deeper nesting)
            sub_dt = None
            for r in store.find_relations_to(member_entity.entity_id, 'used_by'):
                candidate = store.get_entity(r.source_id)
                if candidate and candidate.entity_type == 'datatype':
                    sub_dt = candidate
                    break
            
            if sub_dt:
                current_datatype = sub_dt
                resolved_members.append({
                    'level': 'sub_datatype_{}'.format(i + 1),
                    'name': sub_dt.key,
                    'entity': sub_dt,
                    'evidence': [],
                })
            else:
                # No deeper datatype, stop traversal
                break
        else:
            # Member not found in datatype, stop traversal
            break
    
    return resolved_members


def _classify_resolution_level(resolved_members, path_parts):
    """Classify the resolution level based on how deep we got.
    
    L0 unresolved — nothing resolved
    L1 root resolved — root variable found, no members
    L2 object resolved — first path member (object/instance) found
    L3 member resolved — second path member (interface/member) found  
    L4 operation resolved — third path member (command/operation) found
    L5 physical semantic — has documentation with physical meaning
    """
    if not resolved_members:
        return 'L0_unresolved'
    
    # Count how many path members were resolved
    member_count = sum(1 for m in resolved_members if m['level'].startswith('member_'))
    
    if member_count == 0:
        return 'L1_root_resolved'
    elif member_count == 1:
        return 'L2_object_resolved'
    elif member_count == 2:
        return 'L3_member_resolved'
    elif member_count >= 3:
        # Check if we have physical semantic documentation
        for m in resolved_members:
            for ev in m.get('evidence', []):
                if ev['type'] == 'documentation' and ev.get('value'):
                    return 'L5_physical_semantic'
        return 'L4_operation_resolved'
    
    # Check for documentation at any level
    for m in resolved_members:
        if m.get('evidence'):
            for ev in m['evidence']:
                if ev['type'] == 'documentation':
                    return 'L5_physical_semantic'
    
    return 'L1_root_resolved'


def _build_semantic_fields(resolved_members, expr):
    """Build actor/operation/target/position/station fields from resolved members.
    
    Convention for PLC structured variables:
    root (EM06V) → station
    path[0] (Axis_Z1) → actor (the physical object)
    path[1] (In) → interface direction (input/output)
    path[2] (Location_Auto) → operation/command
    index → position/parameter
    """
    fields = {
        'station': {'value': '', 'evidence': [], 'resolved': False},
        'actor': {'value': '', 'evidence': [], 'resolved': False},
        'operation': {'value': '', 'evidence': [], 'resolved': False},
        'target': {'value': '', 'evidence': [], 'resolved': False},
        'position': {'value': '', 'evidence': [], 'resolved': False},
    }
    
    for m in resolved_members:
        level = m['level']
        evidence = m.get('evidence', [])
        docs = [e['value'] for e in evidence if e['type'] == 'documentation']
        
        if level == 'root':
            # Station
            if docs:
                fields['station']['value'] = docs[0]
                fields['station']['evidence'] = evidence
                fields['station']['resolved'] = True
            elif m.get('entity'):
                fields['station']['value'] = m['entity'].key
                fields['station']['resolved'] = True
        
        elif level.startswith('member_'):
            member_idx = int(level.split('_')[1])
            if member_idx == 1:
                # Actor (e.g., Axis_Z1, Cy_Clamp1)
                if docs:
                    fields['actor']['value'] = docs[0]
                    fields['actor']['evidence'] = evidence
                    fields['actor']['resolved'] = True
                elif m.get('entity'):
                    fields['actor']['value'] = m['entity'].key
                    fields['actor']['resolved'] = True
            elif member_idx == 2:
                # Interface direction (In/Out) — skip, not a semantic field
                pass
            elif member_idx == 3:
                # Operation (e.g., Location_Auto, Auto_R, Auto_V)
                if docs:
                    fields['operation']['value'] = docs[0]
                    fields['operation']['evidence'] = evidence
                    fields['operation']['resolved'] = True
                elif m.get('entity'):
                    fields['operation']['value'] = m['entity'].key
                    fields['operation']['resolved'] = True
    
    # Position from index
    if expr.index is not None:
        if isinstance(expr.index, int):
            fields['position']['value'] = str(expr.index)
            fields['position']['resolved'] = True
        elif isinstance(expr.index, tuple):
            fields['position']['value'] = ','.join(str(i) for i in expr.index)
            fields['position']['resolved'] = True
    elif expr.index_var:
        fields['position']['value'] = expr.index_var
        fields['position']['resolved'] = False
    
    return fields


def resolve_symbol_v2(store, expr_str: str) -> dict:
    """v2: Member-level symbol resolution with L0-L5 levels.
    
    Resolution levels:
    L0 unresolved — nothing resolved
    L1 root resolved — root variable found (station-level)
    L2 object resolved — first member (actor) found
    L3 member resolved — second member found  
    L4 operation resolved — third member (command) found
    L5 physical semantic — has documentation with physical meaning
    
    Resolution path:
    expression → root variable → datatype → member → sub-datatype → sub-member → ...
    """
    expr = parse_expression(expr_str)
    
    # First try exact match (existing v1 logic)
    entity = _find_exact(store, expr)
    if entity:
        evidence = _collect_evidence(store, entity)
        docs = [e for e in evidence if e['type'] == 'documentation']
        level = 'L5_physical_semantic' if docs else 'L1_root_resolved'
        return {
            'expression': expr_str,
            'parsed': _expr_to_dict(expr),
            'resolved': True,
            'resolution_level': level,
            'resolved_members': [{
                'level': 'root',
                'name': entity.key,
                'entity': _entity_brief(entity),
                'evidence': evidence,
            }],
            'semantic_fields': _build_semantic_fields(
                [{'level': 'root', 'name': entity.key, 'entity': entity, 'evidence': evidence}],
                expr),
            'candidates': [],
        }
    
    # Member-level resolution: root → datatype → members
    resolved_members = _resolve_member_chain(store, expr.root, expr.path)
    
    if resolved_members:
        level = _classify_resolution_level(resolved_members, expr.path)
        semantic_fields = _build_semantic_fields(resolved_members, expr)
        
        # Also check HMI reachability
        hmi_reachable = _check_hmi_reachable(store, resolved_members)
        
        return {
            'expression': expr_str,
            'parsed': _expr_to_dict(expr),
            'resolved': True,
            'resolution_level': level,
            'resolved_members': [
                {
                    'level': m['level'],
                    'name': m['name'],
                    'entity': _entity_brief(m['entity']) if m.get('entity') else None,
                    'evidence': m.get('evidence', []),
                }
                for m in resolved_members
            ],
            'semantic_fields': semantic_fields,
            'hmi_reachable': hmi_reachable,
            'candidates': [],
        }
    
    # Fallback: candidate matching
    candidates = _find_candidates(store, expr)
    return {
        'expression': expr_str,
        'parsed': _expr_to_dict(expr),
        'resolved': False,
        'resolution_level': 'L0_unresolved',
        'resolved_members': [],
        'semantic_fields': {},
        'hmi_reachable': {'reachable': False, 'path': []},
        'candidates': [_entity_brief(e) for e in candidates[:5]],
    }


def _check_hmi_reachable(store, resolved_members):
    """Check if any resolved member has HMI label evidence."""
    for m in resolved_members:
        entity = m.get('entity')
        if not entity:
            continue
        # Check direct HMI label via address
        for r in store.find_relations_from(entity.entity_id, 'maps_to'):
            addr = store.get_entity(r.target_id)
            if addr:
                for r2 in store.find_relations_from(addr.entity_id, 'labelled_as'):
                    label = store.get_entity(r2.target_id)
                    if label:
                        return {
                            'reachable': True,
                            'level': 'exact',
                            'path': [
                                {'type': 'variable', 'value': entity.key},
                                {'type': 'address', 'value': addr.key},
                                {'type': 'hmi_label', 'value': label.display_name or label.key},
                            ],
                        }
        # Check HMI component binding
        for r in store.find_relations_to(entity.entity_id, 'binds_to'):
            comp = store.get_entity(r.source_id)
            if comp:
                return {
                    'reachable': True,
                    'level': 'structural',
                    'path': [
                        {'type': 'variable', 'value': entity.key},
                        {'type': 'hmi_component', 'value': comp.key},
                    ],
                }
    
    # Check if station-level has HMI (indirect)
    root_member = resolved_members[0] if resolved_members else None
    if root_member and root_member.get('entity'):
        root_e = root_member['entity']
        station_docs = [e for e in root_member.get('evidence', []) if e['type'] == 'documentation']
        if station_docs:
            return {
                'reachable': False,
                'level': 'station_only',
                'path': [{'type': 'station', 'value': station_docs[0]['value']}],
            }
    
    return {'reachable': False, 'level': 'unreachable', 'path': []}
