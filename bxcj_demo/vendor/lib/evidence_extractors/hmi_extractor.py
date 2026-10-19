#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/evidence_extractors/hmi_extractor.py — HMI evidence extractor

从 HMIDecoder 输出的 raw records 创建 Entity + Relation。

创建的实体类型:
  - physical_address, hmi_label, hmi_component, plc_variable

创建的关系类型:
  - labelled_as, binds_to, maps_to

配置:
  - namespace_segments: set of namespace segments to skip when extracting var name
"""
from __future__ import annotations

import re

from ..evidence_model import Entity, Relation, EvidenceStore, make_entity_id


class HMIEvidenceExtractor:
    """HMI raw records → Entity + Relation"""

    DEFAULT_NAMESPACE_SEGMENTS = {
        'Parameter', 'PartContolVar', 'HANS', 'Hans', 'CATL', 'Local_HMI',
        'Custom', 'CQ', 'Station', 'MachineOpen', 'MachineO', 'System_Var',
        'Application',
    }

    def __init__(self, config: dict = None):
        config = config or {}
        ns = config.get('namespace_segments')
        self.namespace_segments = set(ns) if ns else self.DEFAULT_NAMESPACE_SEGMENTS

    def extract(self, hmi_records: dict, store: EvidenceStore) -> dict:
        """Extract entities and relations from HMI decoded records.

        Args:
            hmi_records: dict from HMIDecoder.decode()
            store: EvidenceStore to add entities/relations to

        Returns:
            stats dict
        """
        stats = {
            'y_labels': 0, 'm0s0i_labels': 0,
            'symbol_bindings': 0, 'components': 0, 'relations': 0,
        }

        records = hmi_records.get('records', {})

        # Y address labels
        for rec in records.get('y_address_labels', []):
            addr = rec['address']
            text = rec['label_text']
            addr_entity = Entity(
                make_entity_id('physical_address', addr),
                'physical_address', addr, addr)
            store.add_entity(addr_entity)
            label_entity = Entity(
                make_entity_id('hmi_label', text),
                'hmi_label', text, text)
            store.add_entity(label_entity)
            store.add_relation(Relation(
                addr_entity.entity_id, label_entity.entity_id,
                'labelled_as', 'exact', 'hmi_binary',
                f'HMI {addr} 关联文本'))
            stats['y_labels'] += 1

        # M0S0I labels
        for rec in records.get('m0s0i_labels', []):
            code = rec['code']
            text = rec['label_text']
            addr_entity = Entity(
                make_entity_id('physical_address', code),
                'physical_address', code, code)
            store.add_entity(addr_entity)
            label_entity = Entity(
                make_entity_id('hmi_label', text),
                'hmi_label', text, text)
            store.add_entity(label_entity)
            store.add_relation(Relation(
                addr_entity.entity_id, label_entity.entity_id,
                'labelled_as', 'exact', 'hmi_m0s0i_label',
                f'M0S0I {code} → {text}'))
            # Match PLC variables with same code
            for pc in store.find_by_key('plc_variable', code):
                store.add_relation(Relation(
                    pc.entity_id, addr_entity.entity_id,
                    'maps_to', 'exact', 'hmi_m0s0i_variable_match',
                    f'PLC {code} 匹配 M0S0I'))
            stats['m0s0i_labels'] += 1

        # Symbol bindings
        for rec in records.get('symbol_bindings', []):
            var_path = rec['symbol_path']
            last_seg = rec['var_name']
            for c in store.find_by_key('plc_variable', last_seg):
                sym_entity = Entity(
                    make_entity_id('hmi_label', f'symbol:{var_path}'),
                    'hmi_label', var_path, var_path)
                store.add_entity(sym_entity)
                store.add_relation(Relation(
                    c.entity_id, sym_entity.entity_id,
                    'labelled_as', 'structural', 'hmi_direct_symbol_binding',
                    f'Application.Parameter.{var_path}'))
                stats['symbol_bindings'] += 1

        # Structured components
        for rec in records.get('components', []):
            text = rec['text']
            rec_type = rec['record_type']
            pos = rec['pos']

            if rec_type == 'mixed':
                nr = self._process_mixed_component(text, pos, store)
                stats['components'] += nr
            elif rec_type == 'binding':
                nr = self._process_binding_component(text, pos, store)
                stats['components'] += nr
            elif rec_type == 'label':
                nr = self._process_label_component(text, pos, store)
                stats['components'] += nr
            elif rec_type == 'm0s0i':
                self._process_m0s0i_component(text, pos, store)

        return stats

    def _process_mixed_component(self, text: str, pos: int,
                                  store: EvidenceStore) -> int:
        binding_pattern = re.compile(
            r'(?:CODESYS\s+V3\s+\(Ethernet\)\s*:\s*|Local\s+HMI\s*:\s*)'
            r'(Application\.[^\s]+|LW[\w-]+|RW[\w-]+|Y[\d.]+|X[\d.]+)')
        m = binding_pattern.search(text)
        if not m:
            return 0
        binding_path = m.group(1)
        var_path = binding_path[len('Application.'):] if binding_path.startswith('Application.') else binding_path
        base_var = self._extract_var_name(var_path)
        if not base_var:
            return 0

        chinese_text = ''
        cn_m = re.search(r'([\u4e00-\u9fff][\u4e00-\u9fff\w]{1,30})', text[m.end():])
        if cn_m:
            chinese_text = cn_m.group(1)
        if not chinese_text:
            cn_in_path = re.search(r'\.([\u4e00-\u9fff][\u4e00-\u9fff\w]{1,30})', binding_path)
            if cn_in_path:
                chinese_text = cn_in_path.group(1)
        if not chinese_text:
            cn_in_var = re.search(r'([\u4e00-\u9fff][\u4e00-\u9fff\w]{1,30})', base_var)
            if cn_in_var:
                chinese_text = cn_in_var.group(1)
        if not chinese_text:
            return 0

        comp = Entity(
            make_entity_id('hmi_component', f'comp@{pos}'),
            'hmi_component', f'comp@{pos}',
            f'HMI组件({chinese_text})')
        store.add_entity(comp)
        var_entity = self._get_or_create_var(base_var, store)
        label_entity = Entity(
            make_entity_id('hmi_label', chinese_text),
            'hmi_label', chinese_text, chinese_text)
        store.add_entity(label_entity)
        store.add_relation(Relation(
            comp.entity_id, var_entity.entity_id,
            'binds_to', 'exact', 'hmi_mixed_record',
            f'pos={pos}: {text[:80]}'))
        store.add_relation(Relation(
            comp.entity_id, label_entity.entity_id,
            'labelled_as', 'exact', 'hmi_mixed_record', f'pos={pos}'))
        store.add_relation(Relation(
            var_entity.entity_id, label_entity.entity_id,
            'labelled_as', 'exact', 'hmi_mixed_record_direct',
            f'{base_var} → {chinese_text}'))
        return 1

    def _process_binding_component(self, text: str, pos: int,
                                    store: EvidenceStore) -> int:
        binding_pattern = re.compile(
            r'(?:CODESYS\s+V3\s+\(Ethernet\)\s*:\s*|Local\s+HMI\s*:\s*)'
            r'(Application\.[^\s]+|LW[\w-]+|RW[\w-]+|Y[\d.]+|X[\d.]+)')
        m = binding_pattern.search(text)
        if not m:
            return 0
        binding_path = m.group(1)
        var_path = binding_path[len('Application.'):] if binding_path.startswith('Application.') else binding_path
        base_var = self._extract_var_name(var_path)
        if not base_var:
            return 0
        comp = Entity(
            make_entity_id('hmi_component', f'comp@{pos}'),
            'hmi_component', f'comp@{pos}', f'HMI组件({base_var})')
        store.add_entity(comp)
        var_entity = self._get_or_create_var(base_var, store)
        store.add_relation(Relation(
            comp.entity_id, var_entity.entity_id,
            'binds_to', 'structural', 'hmi_binding_record',
            f'pos={pos}: {binding_path}'))
        return 1

    def _process_label_component(self, text: str, pos: int,
                                  store: EvidenceStore) -> int:
        text = text.strip()
        if len(text) < 2:
            return 0
        comp = Entity(
            make_entity_id('hmi_component', f'comp@{pos}'),
            'hmi_component', f'comp@{pos}', f'HMI组件({text[:20]})')
        store.add_entity(comp)
        label_entity = Entity(
            make_entity_id('hmi_label', text[:100]),
            'hmi_label', text[:100], text[:100])
        store.add_entity(label_entity)
        store.add_relation(Relation(
            comp.entity_id, label_entity.entity_id,
            'labelled_as', 'structural', 'hmi_label_record',
            f'pos={pos}'))
        return 1

    def _process_m0s0i_component(self, text: str, pos: int,
                                  store: EvidenceStore):
        text = text.strip()
        m = re.match(r'(M\d+S\d+[IQ]\d+)[_\s]*(.+)', text)
        if not m:
            return
        code, label_text = m.group(1), m.group(2).strip()
        addr_entity = Entity(
            make_entity_id('physical_address', code),
            'physical_address', code, code)
        store.add_entity(addr_entity)
        label_entity = Entity(
            make_entity_id('hmi_label', label_text),
            'hmi_label', label_text, label_text)
        store.add_entity(label_entity)
        store.add_relation(Relation(
            addr_entity.entity_id, label_entity.entity_id,
            'labelled_as', 'exact', 'hmi_m0s0i_structured',
            f'pos={pos}'))
        for pc in store.find_by_key('plc_variable', code):
            store.add_relation(Relation(
                pc.entity_id, addr_entity.entity_id,
                'maps_to', 'exact', 'hmi_m0s0i_structured_match',
                f'PLC {code} 匹配 M0S0I'))

    def _extract_var_name(self, var_path: str) -> str:
        if not var_path:
            return ''
        if var_path.startswith(('LW', 'RW', 'Y', 'X')):
            return var_path
        for part in reversed(var_path.split('.')):
            base = re.sub(r'\[\d+\]', '', part).strip()
            if base and base not in self.namespace_segments:
                return base
        return ''

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
