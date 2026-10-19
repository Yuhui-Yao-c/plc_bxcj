#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/evidence_extractors/plcopen_extractor.py — PLCopen evidence extractor

从 DecodedProgram (Canonical IR + VariableDeclarations) 创建 Entity + Relation。

不做:
  - 重新解析 XML (Decoder 已完成)
  - 判断 Step / actor / operation 语义

创建的实体类型:
  - plc_variable, documentation_text, physical_address, pou, datatype

创建的关系类型:
  - documented_as, maps_to, belongs_to, used_by, instance_of, calls,
    input_param, output_param, named_as

可选配置 (从 Program Pattern Spec 读取):
  - station_code_pattern: POU 名 → station 的正则 (项目级约定)
  - filename_station_pattern: 文件名 → station 的正则
  - namespace_segments: HMI binding 中的命名空间段 (用于变量名提取)
"""
from __future__ import annotations

import re
from pathlib import Path

from ..evidence_model import Entity, Relation, EvidenceStore, make_entity_id
from ..canonical_ir import DecodedProgram, ProgramUnit, VariableDeclaration


class PLCopenEvidenceExtractor:
    """DecodedProgram → Entity + Relation"""

    # Default physical address patterns (can be overridden by config).
    # These are common PLC physical address formats, not project-specific
    # conventions. Project-specific patterns should come from evidence_config.
    DEFAULT_Y_ADDR_PATTERN = r'Y\d{6}'
    DEFAULT_Y_DOT_PATTERN = r'Y\d+\.\d+'
    # Default station prefix strip pattern (project convention from Spec)
    DEFAULT_STATION_PREFIX_STRIP = r'^[A-Z]{2}\d{2}V?\.'

    def __init__(self, config: dict = None):
        """Initialize with optional project-level config.

        Args:
            config: {
                'station_code_pattern': str (regex for POU name → station),
                'filename_station_pattern': str (regex for filename → station),
                'filename_chinese_pattern': str (regex for filename → station Chinese name),
                'physical_address_patterns': {
                    'y_address': str, 'y_dot_address': str,
                },
                'station_prefix_strip_pattern': str,
            }
        """
        config = config or {}
        self.station_pou_pattern = re.compile(
            config.get('station_code_pattern', r'^([A-Z]{2}\d{2})(?:[_\s]+(.+))?$'))
        self.filename_pattern = re.compile(
            config.get('filename_station_pattern',
                        r'\d+_([A-Z]{2}\d{2})_+(.+?)__'))
        addr_patterns = config.get('physical_address_patterns', {}) or {}
        self.y_addr_pattern = re.compile(
            addr_patterns.get('y_address', self.DEFAULT_Y_ADDR_PATTERN))
        self.y_dot_pattern = re.compile(
            addr_patterns.get('y_dot_address', self.DEFAULT_Y_DOT_PATTERN))
        self.station_prefix_strip = re.compile(
            config.get('station_prefix_strip_pattern',
                        self.DEFAULT_STATION_PREFIX_STRIP))

    def extract(self, decoded: DecodedProgram, store: EvidenceStore) -> dict:
        """Extract entities and relations from a DecodedProgram.

        Returns:
            stats dict with counts
        """
        stats = {
            'variables': 0, 'documentations': 0, 'addresses': 0,
            'pous': 0, 'datatypes': 0, 'stations': 0,
            'fb_types': 0, 'fb_instances': 0, 'fb_calls': 0,
            'param_bindings': 0,
        }

        # 1. Create POU and datatype entities FIRST (needed for belongs_to)
        for unit in decoded.program_units:
            if unit.unit_type == 'pou':
                pou_name = unit.name
                pou_id = make_entity_id('pou', pou_name)
                store.add_entity(Entity(pou_id, 'pou', pou_name, pou_name))
                stats['pous'] += 1

        # 2. Create datatype entities from variable declarations
        datatypes_seen = set()
        for var_decl in decoded.variable_declarations:
            if var_decl.derived_type and var_decl.derived_type not in datatypes_seen:
                dt_id = make_entity_id('datatype', var_decl.derived_type)
                store.add_entity(Entity(dt_id, 'datatype',
                                        var_decl.derived_type,
                                        var_decl.derived_type))
                datatypes_seen.add(var_decl.derived_type)
                stats['datatypes'] += 1
        # Also create datatype entities from variable scopes (dataType containers)
        for var_decl in decoded.variable_declarations:
            if var_decl.scope:
                for part in var_decl.scope.split('/'):
                    if ':' in part:
                        ct, cn = part.split(':', 1)
                        if ct == 'datatype' and cn not in datatypes_seen:
                            dt_id = make_entity_id('datatype', cn)
                            store.add_entity(Entity(dt_id, 'datatype', cn, cn))
                            datatypes_seen.add(cn)
                            stats['datatypes'] += 1

        # 3. Extract variable declarations → entities + relations
        for var_decl in decoded.variable_declarations:
            self._extract_variable_declaration(var_decl, store, stats)

        # 4. Extract station relations from POU names
        for unit in decoded.program_units:
            if unit.unit_type == 'pou':
                self._extract_pou_station(unit, store, stats)

        # 5. Extract FB call graph from Call elements in ACTION units only
        # (matches old FBCallGraphExtractor behavior: only actions have FB calls)
        for unit in decoded.program_units:
            if unit.unit_type == 'action':
                self._extract_fb_calls(unit, store, stats)

        # 6. Extract station from filename
        if decoded.source_file:
            fname = Path(decoded.source_file).name
            self._extract_station_from_filename(fname, store, stats)

        return stats

    def _extract_variable_declaration(self, decl: VariableDeclaration,
                                       store: EvidenceStore, stats: dict):
        """Create plc_variable entity + documentation/address/datatype relations."""
        var_entity = Entity(
            make_entity_id('plc_variable', decl.name, scope=decl.scope),
            'plc_variable', decl.name, decl.name, scope=decl.scope)
        store.add_entity(var_entity)
        stats['variables'] += 1

        # Documentation
        if decl.documentation:
            doc_entity = Entity(
                make_entity_id('documentation_text', decl.documentation[:200]),
                'documentation_text', decl.documentation[:200],
                decl.documentation[:200])
            store.add_entity(doc_entity)
            store.add_relation(Relation(
                var_entity.entity_id, doc_entity.entity_id,
                'documented_as', 'exact', 'plc_documentation',
                f'变量 {decl.name} (scope={decl.scope}) documentation'))
            stats['documentations'] += 1

            # Y addresses in documentation
            for pat in (self.y_addr_pattern, self.y_dot_pattern):
                for m in pat.finditer(decl.documentation):
                    addr = m.group(0)
                    addr_entity = Entity(
                        make_entity_id('physical_address', addr),
                        'physical_address', addr, addr)
                    store.add_entity(addr_entity)
                    store.add_relation(Relation(
                        var_entity.entity_id, addr_entity.entity_id,
                        'maps_to', 'exact', 'plc_documentation_y_addr',
                        f'documentation 含 {addr}'))

        # AT address binding
        if decl.at_address:
            addr_entity = Entity(
                make_entity_id('physical_address', decl.at_address),
                'physical_address', decl.at_address, decl.at_address)
            store.add_entity(addr_entity)
            store.add_relation(Relation(
                var_entity.entity_id, addr_entity.entity_id,
                'maps_to', 'exact', 'plc_at_binding',
                f'变量 {decl.name} AT 声明'))
            stats['addresses'] += 1

        # Derived type
        if decl.derived_type:
            dt_id = make_entity_id('datatype', decl.derived_type)
            if dt_id not in store.entities:
                store.add_entity(Entity(
                    dt_id, 'datatype', decl.derived_type,
                    decl.derived_type))
                stats['datatypes'] += 1
            store.add_relation(Relation(
                dt_id, var_entity.entity_id,
                'used_by', 'structural', 'plc_derived_type',
                f'{decl.derived_type} 被 {decl.name} 引用 (scope={decl.scope})'))

        # Belongs to POU/datatype (from scope)
        if decl.scope:
            parts = decl.scope.split('/')
            for part in parts:
                if ':' in part:
                    ct, cn = part.split(':', 1)
                    cid = make_entity_id(ct, cn)
                    if cid in store.entities:
                        store.add_relation(Relation(
                            var_entity.entity_id, cid,
                            'belongs_to', 'structural',
                            f'plc_{ct}_structure',
                            f'{decl.name} 在 {ct} {cn} 内'))

    def _extract_pou_station(self, unit: ProgramUnit, store: EvidenceStore,
                              stats: dict):
        """Create station relation from POU name (POU entity already exists)."""
        pou_name = unit.name
        pou_id = make_entity_id('pou', pou_name)

        # Station from POU name pattern
        m = self.station_pou_pattern.match(pou_name)
        if m:
            station_id = m.group(1)
            chinese_part = (m.group(2) or '').strip().rstrip('_').strip()
            station_entity = Entity(
                make_entity_id('station_thread', station_id),
                'station_thread', station_id, station_id)
            store.add_entity(station_entity)
            store.add_relation(Relation(
                pou_id, station_entity.entity_id,
                'named_as', 'exact', 'pou_name_pattern',
                f'POU {pou_name} 匹配工站'))
            if chinese_part and re.search(r'[\u4e00-\u9fff]', chinese_part):
                doc_entity = Entity(
                    make_entity_id('documentation_text', chinese_part),
                    'documentation_text', chinese_part, chinese_part)
                store.add_entity(doc_entity)
                store.add_relation(Relation(
                    station_entity.entity_id, doc_entity.entity_id,
                    'named_as', 'exact', 'pou_name_chinese_part',
                    '含中文工站名'))
            stats['stations'] += 1

    def _extract_station_from_filename(self, filename: str,
                                        store: EvidenceStore, stats: dict):
        """Extract station from filename pattern."""
        m = self.filename_pattern.search(filename)
        if not m:
            return
        station_id = m.group(1)
        chinese_part = m.group(2).strip()
        station_entity = Entity(
            make_entity_id('station_thread', station_id),
            'station_thread', station_id, station_id)
        store.add_entity(station_entity)
        if chinese_part and re.search(r'[\u4e00-\u9fff]', chinese_part):
            doc_entity = Entity(
                make_entity_id('documentation_text', chinese_part),
                'documentation_text', chinese_part, chinese_part)
            store.add_entity(doc_entity)
            store.add_relation(Relation(
                station_entity.entity_id, doc_entity.entity_id,
                'named_as', 'exact', 'filename_pattern',
                '含中文工站名'))

    def _extract_fb_calls(self, unit: ProgramUnit, store: EvidenceStore,
                           stats: dict):
        """Extract FB call graph from Call elements in program units."""
        from ..canonical_ir import Call as CallElement

        pou_name = unit.name.split('.')[0] if '.' in unit.name else unit.name
        action_name = unit.name.split('.')[1] if '.' in unit.name else ''
        pou_id = make_entity_id('pou', pou_name)

        for elem in unit.elements:
            if not isinstance(elem, CallElement):
                continue

            instance_name = elem.instance_name
            fb_type = elem.callee
            if not instance_name or not fb_type:
                continue

            # FB type entity
            fb_type_id = make_entity_id('datatype', fb_type)
            store.add_entity(Entity(
                fb_type_id, 'datatype', fb_type, fb_type))
            stats['fb_types'] += 1

            # FB instance entity
            scope_parts = [f'pou:{pou_name}']
            if action_name:
                scope_parts.append(f'action:{action_name}')
            instance_scope = '/'.join(scope_parts)
            fb_instance_id = make_entity_id(
                'fb_instance', instance_name, scope=instance_scope)
            store.add_entity(Entity(
                fb_instance_id, 'fb_instance', instance_name, instance_name,
                scope=instance_scope))
            stats['fb_instances'] += 1

            # instance_of relation
            store.add_relation(Relation(
                fb_instance_id, fb_type_id,
                'instance_of', 'exact', 'plcopen_block_typeName',
                f'typeName={fb_type}'))

            # calls relation
            call_detail = f'POU {pou_name}'
            if action_name:
                call_detail += f' action {action_name}'
            call_detail += f' 内 block {instance_name}'
            store.add_relation(Relation(
                pou_id, fb_instance_id,
                'calls', 'exact', 'plcopen_block_in_pou', call_detail))
            stats['fb_calls'] += 1

            # Parameter bindings
            for param in elem.inputs:
                var_ref = param.get('value')
                base_var = self._extract_base_var(var_ref)
                if not base_var:
                    continue
                var_entity = self._get_or_create_var(base_var, store)
                store.add_relation(Relation(
                    fb_instance_id, var_entity.entity_id,
                    'input_param', 'exact', 'plcopen_block_param_binding',
                    f'FB {instance_name} input {param["name"]} = {var_ref.name}'))
                stats['param_bindings'] += 1

            for param in elem.outputs:
                var_ref = param.get('value')
                base_var = self._extract_base_var(var_ref)
                if not base_var:
                    continue
                var_entity = self._get_or_create_var(base_var, store)
                store.add_relation(Relation(
                    fb_instance_id, var_entity.entity_id,
                    'output_param', 'exact', 'plcopen_block_param_binding',
                    f'FB {instance_name} output {param["name"]} = {var_ref.name}'))
                stats['param_bindings'] += 1

    def _extract_base_var(self, var_ref) -> str:
        """Extract base variable name from a VariableRef."""
        if var_ref is None:
            return ''
        name = getattr(var_ref, 'name', '') or getattr(var_ref, 'base_name', '')
        if not name:
            return ''
        # Strip station prefix using configurable pattern (project convention)
        clean = self.station_prefix_strip.sub('', name.strip())
        m = re.match(r'^([A-Za-z_]\w*)', clean)
        return m.group(1) if m else ''

    def _get_or_create_var(self, base_var: str,
                            store: EvidenceStore) -> Entity:
        candidates = store.find_by_key('plc_variable', base_var)
        if candidates:
            return candidates[0]
        var_entity = Entity(
            make_entity_id('plc_variable', base_var),
            'plc_variable', base_var, base_var)
        store.add_entity(var_entity)
        return var_entity
