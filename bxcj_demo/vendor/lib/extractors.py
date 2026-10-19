#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib/extractors.py — 统一语义证据提取器（被 opencode 调用的工具库）

合并自 plc_zjph/semantic 的三个提取器，去掉 LLM 相关代码，保留全部格式解析逻辑：
PLCopen XML / SMC2 CDATA JSON / 威纶通 MT8000 HMI 二进制 / FB 调用链。

提取器:
  - PLCVarDocExtractor           : 变量 + documentation + AT 地址绑定 (PLCopen/SMC2)
  - POUStationExtractor          : POU 名/文件名 → 工站
  - HMIExtractor                 : HMI 二进制 Y地址 → 中文标签 (正则)
  - DirectSymbolBindingExtractor : HMI Application.Parameter.* 直接符号绑定
  - HMIComponentExtractor        : MT8000 结构化长度前缀记录 → HMI 组件
  - FBCallGraphExtractor         : PLCopen XML LD/FBD block 调用链

由 tools/build_evidence.py 调用 build_evidence_store(plc_path, hmi_path) → EvidenceStore，
调用方再执行 store.save(output_path) 持久化为 JSON。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

from .evidence_model import Entity, Relation, EvidenceStore, make_entity_id


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


MT8000_MAGIC = b'MT8000Series'


def _is_smc2_xml(path: Path) -> bool:
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(4096)
    except Exception:
        return False
    return 'type="json"' in head or ('<group' in head and '<pou' not in head)


def find_hmi_project(search_root: str) -> str | None:
    root = Path(search_root)
    if not root.exists():
        return None
    for p in root.rglob('project'):
        try:
            with open(p, 'rb') as f:
                if f.read(len(MT8000_MAGIC)) == MT8000_MAGIC:
                    return str(p)
        except Exception:
            continue
    for p in root.rglob('*.bin'):
        try:
            with open(p, 'rb') as f:
                if f.read(len(MT8000_MAGIC)) == MT8000_MAGIC:
                    return str(p)
        except Exception:
            continue
    return None


def _resolve_hmi_path(hmi_path: str) -> str | None:
    p = Path(hmi_path)
    if p.is_file():
        return str(p)
    if p.is_dir():
        return find_hmi_project(hmi_path)
    return None


# ============================================================
# PLCVarDocExtractor — PLC 变量 + documentation + AT 地址绑定
# ============================================================

class PLCVarDocExtractor:
    """PLCopen XML / SMC2 XML 变量声明 + documentation + AT 地址绑定。
    关系: documented_as / maps_to / belongs_to / used_by。
    """

    Y_ADDR_PATTERN = re.compile(r'(Y\d{6})')
    Y_DOT_PATTERN = re.compile(r'(Y\d+\.\d+)')
    AT_PATTERN = re.compile(r'<AT>\s*([^<\s]+)\s*</AT>', re.IGNORECASE)

    def __init__(self, encoding: str = 'utf-8'):
        self.encoding = encoding

    def extract_from_plcopen_xml(self, xml_path: str, store: EvidenceStore,
                                 max_vars: int = 100000) -> int:
        path = Path(xml_path)
        if not path.exists():
            return 0
        var_count = 0
        container_stack: list = []
        scan_count = 0
        try:
            context = ET.iterparse(str(path), events=('start', 'end'))
            for event, elem in context:
                scan_count += 1
                if scan_count > 5000000:
                    break
                tag = _strip_ns(elem.tag)
                if event == 'start':
                    if tag == 'pou':
                        name = elem.get('name', '')
                        if name:
                            container_stack.append(('pou', name))
                            store.add_entity(Entity(make_entity_id('pou', name), 'pou', name, name, scope=''))
                    elif tag == 'dataType':
                        name = elem.get('name', '')
                        if name:
                            container_stack.append(('datatype', name))
                            store.add_entity(Entity(make_entity_id('datatype', name), 'datatype', name, name, scope=''))
                    continue
                if tag in ('pou', 'dataType'):
                    if container_stack:
                        container_stack.pop()
                    elem.clear()
                    continue
                if tag != 'variable':
                    continue
                var_name = elem.get('name', '')
                if not var_name:
                    elem.clear()
                    continue
                scope = '/'.join(f'{t}:{n}' for t, n in container_stack)

                doc_text = ''
                for child in elem:
                    if _strip_ns(child.tag) == 'documentation':
                        doc_raw = ''
                        for sub in child.iter():
                            if sub.text:
                                doc_raw += sub.text + ' '
                            if sub.tail:
                                doc_raw += sub.tail + ' '
                        doc_text = re.sub(r'\s+', ' ', doc_raw).strip()[:300]

                at_addr = ''
                at_m = self.AT_PATTERN.search(ET.tostring(elem, encoding='unicode'))
                if at_m:
                    at_addr = at_m.group(1).strip()

                derived_type = ''
                for child in elem:
                    ctag = _strip_ns(child.tag)
                    if ctag == 'type':
                        for sub in child:
                            stag = _strip_ns(sub.tag)
                            if stag == 'derived':
                                derived_type = sub.get('name', '')
                            elif stag == 'array':
                                for sub2 in sub.iter():
                                    if _strip_ns(sub2.tag) == 'derived':
                                        derived_type = sub2.get('name', '')
                                        break
                    elif ctag == 'derived':
                        derived_type = child.get('name', '')

                var_entity = Entity(make_entity_id('plc_variable', var_name, scope=scope), 'plc_variable', var_name, var_name, scope=scope)
                store.add_entity(var_entity)
                var_count += 1

                if doc_text:
                    doc_entity = Entity(make_entity_id('documentation_text', doc_text), 'documentation_text', doc_text, doc_text, scope='')
                    store.add_entity(doc_entity)
                    store.add_relation(Relation(var_entity.entity_id, doc_entity.entity_id, 'documented_as', 'exact', 'plc_documentation', f'变量 {var_name} (scope={scope}) documentation'))

                if at_addr:
                    addr_entity = Entity(make_entity_id('physical_address', at_addr), 'physical_address', at_addr, at_addr, scope='')
                    store.add_entity(addr_entity)
                    store.add_relation(Relation(var_entity.entity_id, addr_entity.entity_id, 'maps_to', 'exact', 'plc_at_binding', f'变量 {var_name} AT 声明'))

                for pat in (self.Y_ADDR_PATTERN, self.Y_DOT_PATTERN):
                    for m in pat.finditer(doc_text):
                        addr = m.group(1)
                        addr_entity = Entity(make_entity_id('physical_address', addr), 'physical_address', addr, addr, scope='')
                        store.add_entity(addr_entity)
                        store.add_relation(Relation(var_entity.entity_id, addr_entity.entity_id, 'maps_to', 'exact', 'plc_documentation_y_addr', f'documentation 含 {addr}'))

                if container_stack:
                    ct, cn = container_stack[-1]
                    cid = make_entity_id(ct, cn)
                    if cid in store.entities:
                        store.add_relation(Relation(var_entity.entity_id, cid, 'belongs_to', 'structural', f'plc_{ct}_structure', f'{var_name} 在 {ct} {cn} 内'))

                if derived_type:
                    dt_id = make_entity_id('datatype', derived_type)
                    if dt_id not in store.entities:
                        store.add_entity(Entity(dt_id, 'datatype', derived_type, derived_type, scope=''))
                    store.add_relation(Relation(dt_id, var_entity.entity_id, 'used_by', 'structural', 'plc_derived_type', f'{derived_type} 被 {var_name} 引用 (scope={scope})'))

                if var_count >= max_vars:
                    break
                elem.clear()
        except Exception as e:
            print(f'[PLCVarDocExtractor] 异常: {type(e).__name__}: {e}', file=sys.stderr)
        return var_count

    def extract_from_smc2_xml(self, xml_path: str, store: EvidenceStore,
                              source_schema: dict = None, max_records: int = 5000) -> int:
        path = Path(xml_path)
        if not path.exists():
            return 0
        record_type_field = (source_schema or {}).get('record_type_field', 'type')
        record_type_values = (source_schema or {}).get('record_type_values', ['json'])
        comment_field = (source_schema or {}).get('comment_field', 'CMT')
        var_field = (source_schema or {}).get('variable_field', 'Var')
        element_path = (source_schema or {}).get('element_collection_path', 'CLs')

        rec_count = 0
        try:
            context = ET.iterparse(str(path), events=('end',))
            for event, elem in context:
                if _strip_ns(elem.tag) != 'entry':
                    elem.clear()
                    continue
                etype = elem.get(record_type_field, '')
                if record_type_values and etype not in record_type_values:
                    elem.clear()
                    continue
                ename = elem.get('name', f'entry_{rec_count}')
                text = (elem.text or '').strip()
                if not text or not text.startswith('{'):
                    elem.clear()
                    continue
                try:
                    rec = json.loads(text)
                except json.JSONDecodeError:
                    elem.clear()
                    continue
                if not isinstance(rec, dict):
                    elem.clear()
                    continue

                cmt = str(rec.get(comment_field, '')).strip()
                if cmt:
                    store.add_entity(Entity(make_entity_id('documentation_text', cmt[:200]), 'documentation_text', cmt[:200], cmt[:200]))
                    pou_entity = Entity(make_entity_id('pou', ename), 'pou', ename, ename)
                    store.add_entity(pou_entity)
                    store.add_relation(Relation(pou_entity.entity_id, make_entity_id('documentation_text', cmt[:200]), 'documented_as', 'exact', 'smc2_cmt', f'记录 {ename} CMT'))

                elements = rec.get(element_path, [])
                if isinstance(elements, list):
                    for el in elements:
                        if not isinstance(el, dict):
                            continue
                        var_name = str(el.get(var_field, '')).strip()
                        if not var_name:
                            continue
                        base_m = re.match(r'^([A-Za-z_]\w*)', var_name)
                        if not base_m:
                            continue
                        base_name = base_m.group(1)
                        var_entity = Entity(make_entity_id('plc_variable', base_name), 'plc_variable', base_name, base_name)
                        store.add_entity(var_entity)
                        if cmt and make_entity_id('pou', ename) in store.entities:
                            store.add_relation(Relation(var_entity.entity_id, make_entity_id('pou', ename), 'belongs_to', 'structural', 'smc2_record_membership', f'{base_name} 在记录 {ename}'))

                rec_count += 1
                if rec_count >= max_records:
                    break
                elem.clear()
        except Exception as e:
            print(f'[PLCVarDocExtractor/SMC2] 异常: {type(e).__name__}: {e}', file=sys.stderr)
        return rec_count


# ============================================================
# POUStationExtractor — POU/文件名 → 工站
# ============================================================

class POUStationExtractor:
    """POU 名/文件名 → 工站。关系: named_as。"""

    FILENAME_PATTERN = re.compile(r'\d+_([A-Z]{2}\d{2})_+(.+?)__')
    POU_PATTERN = re.compile(r'^([A-Z]{2}\d{2})(?:[_\s]+(.+))?$')

    def _emit_station(self, station_id: str, chinese_part: str, pou_name: str | None,
                      store: EvidenceStore) -> str:
        station_entity = Entity(make_entity_id('station_thread', station_id), 'station_thread', station_id, station_id, scope='')
        store.add_entity(station_entity)
        if pou_name is not None:
            pou_id = make_entity_id('pou', pou_name)
            if pou_id in store.entities:
                store.add_relation(Relation(pou_id, station_entity.entity_id, 'named_as', 'exact', 'pou_name_pattern', f'POU {pou_name} 匹配工站'))
        if chinese_part and re.search(r'[\u4e00-\u9fff]', chinese_part):
            doc_entity = Entity(make_entity_id('documentation_text', chinese_part), 'documentation_text', chinese_part, chinese_part, scope='')
            store.add_entity(doc_entity)
            src = 'pou_name_chinese_part' if pou_name is not None else 'filename_pattern'
            store.add_relation(Relation(station_entity.entity_id, doc_entity.entity_id, 'named_as', 'exact', src, '含中文工站名'))
        return station_id

    def extract_from_pou_name(self, pou_name: str, store: EvidenceStore) -> str | None:
        if not pou_name:
            return None
        m = self.POU_PATTERN.match(pou_name)
        if not m:
            return None
        chinese_part = (m.group(2) or '').strip().rstrip('_').strip()
        return self._emit_station(m.group(1), chinese_part, pou_name, store)

    def extract_from_filename(self, filename: str, store: EvidenceStore) -> str | None:
        if not filename:
            return None
        m = self.FILENAME_PATTERN.search(filename)
        if not m:
            return None
        return self._emit_station(m.group(1), m.group(2).strip(), None, store)

    def extract_all_pous_from_xml(self, xml_path: str, store: EvidenceStore) -> int:
        path = Path(xml_path)
        if not path.exists():
            return 0
        count = 0
        try:
            context = ET.iterparse(str(path), events=('end',))
            for event, elem in context:
                if _strip_ns(elem.tag) != 'pou':
                    elem.clear()
                    continue
                pou_name = elem.get('name', '')
                if pou_name:
                    store.add_entity(Entity(make_entity_id('pou', pou_name), 'pou', pou_name, pou_name, scope=''))
                    if self.extract_from_pou_name(pou_name, store):
                        count += 1
                elem.clear()
        except Exception:
            pass
        return count


# ============================================================
# HMIExtractor — 威纶通 HMI 二进制 → Y地址→中文标签 (正则)
# ============================================================

class HMIExtractor:
    """HMI 二进制 Y地址 → 中文标签。关系: physical_address -[labelled_as]-> hmi_label。"""

    Y6_TEXT_PATTERN = re.compile(rb'([XY]\d{6})\s+(?:\d+#)?((?:[\xe4-\xe9][\x80-\xbf]{2}){2,30})')
    YDOT_TEXT_PATTERN = re.compile(rb'(Y\d+\.\d+)_[A-Z]((?:[\xe4-\xe9][\x80-\xbf]{2}){2,30})')

    def extract(self, hmi_path: str, store: EvidenceStore, encoding: str = 'utf-8') -> int:
        path = Path(hmi_path)
        if not path.exists():
            return 0
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            return 0
        mappings: dict = {}
        for m in self.Y6_TEXT_PATTERN.finditer(data):
            addr, text = m.group(1).decode('ascii'), m.group(2).decode('utf-8', errors='replace')
            if addr not in mappings and text not in ('预留', '备用'):
                mappings[addr] = text
        for m in self.YDOT_TEXT_PATTERN.finditer(data):
            addr, text = m.group(1).decode('ascii'), m.group(2).decode('utf-8', errors='replace')
            if addr not in mappings and text not in ('预留', '备用'):
                mappings[addr] = text
        for addr, text in mappings.items():
            addr_entity = Entity(make_entity_id('physical_address', addr), 'physical_address', addr, addr, scope='')
            store.add_entity(addr_entity)
            label_entity = Entity(make_entity_id('hmi_label', text), 'hmi_label', text, text, scope='')
            store.add_entity(label_entity)
            store.add_relation(Relation(addr_entity.entity_id, label_entity.entity_id, 'labelled_as', 'exact', 'hmi_binary', f'HMI {addr} 关联文本'))
        return len(mappings)


# ============================================================
# DirectSymbolBindingExtractor — HMI 直接符号绑定
# ============================================================

class DirectSymbolBindingExtractor:
    """HMI Application.Parameter.* 符号绑定 + M0S0I 编号标签。"""

    SYMBOL_PATTERN = re.compile(rb'Application\.Parameter\.([A-Za-z_][A-Za-z_0-9.]{2,60})')
    M0S0I_PATTERN = re.compile(rb'(M\d+S\d+I\d+)[_\s]+((?:[\xe4-\xe9][\x80-\xbf]{2}){2,30})')

    def extract(self, hmi_path: str, store: EvidenceStore) -> dict:
        path = Path(hmi_path)
        if not path.exists():
            return {'symbol_bindings': 0, 'm0s0i_labels': 0}
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            return {'symbol_bindings': 0, 'm0s0i_labels': 0}
        stats = {'symbol_bindings': 0, 'm0s0i_labels': 0}

        symbols = {m.group(1).decode('ascii', errors='replace') for m in self.SYMBOL_PATTERN.finditer(data)}
        stats['symbol_bindings'] = len(symbols)
        for var in symbols:
            last_seg = var.split('.')[-1]
            for c in store.find_by_key('plc_variable', last_seg):
                sym_entity = Entity(make_entity_id('hmi_label', f'symbol:{var}'), 'hmi_label', var, var, scope='')
                store.add_entity(sym_entity)
                store.add_relation(Relation(c.entity_id, sym_entity.entity_id, 'labelled_as', 'structural', 'hmi_direct_symbol_binding', f'Application.Parameter.{var}'))

        m0s0i_map: dict = {}
        for m in self.M0S0I_PATTERN.finditer(data):
            code, text = m.group(1).decode('ascii'), m.group(2).decode('utf-8', errors='replace')
            if code not in m0s0i_map:
                m0s0i_map[code] = text
        stats['m0s0i_labels'] = len(m0s0i_map)
        for code, text in m0s0i_map.items():
            addr_entity = Entity(make_entity_id('physical_address', code), 'physical_address', code, code, scope='')
            store.add_entity(addr_entity)
            label_entity = Entity(make_entity_id('hmi_label', text), 'hmi_label', text, text, scope='')
            store.add_entity(label_entity)
            store.add_relation(Relation(addr_entity.entity_id, label_entity.entity_id, 'labelled_as', 'exact', 'hmi_m0s0i_label', f'M0S0I {code} → {text}'))
            for pc in store.find_by_key('plc_variable', code):
                store.add_relation(Relation(pc.entity_id, addr_entity.entity_id, 'maps_to', 'exact', 'hmi_m0s0i_variable_match', f'PLC {code} 匹配 M0S0I'))
        return stats


# ============================================================
# HMIComponentExtractor — MT8000 结构化组件记录
# ============================================================

class HMIComponentExtractor:
    """MT8000 结构化长度前缀记录 → HMI 组件。
    记录类型: mixed(变量+中文) / binding(仅变量) / label(仅中文) / m0s0i。
    """

    BINDING_PATH_PATTERN = re.compile(
        r'(?:CODESYS\s+V3\s+\(Ethernet\)\s*:\s*|Local\s+HMI\s*:\s*)'
        r'(Application\.[^\s]+|LW[\w-]+|RW[\w-]+|Y[\d.]+|X[\d.]+)')

    def extract(self, hmi_path: str, store: EvidenceStore) -> dict:
        path = Path(hmi_path)
        if not path.exists():
            return self._empty_stats()
        with open(path, 'rb') as f:
            data = f.read()
        if data[:len(MT8000_MAGIC)] != MT8000_MAGIC:
            return self._empty_stats()

        records = self._scan_records(data)
        stats = Counter()
        components_created = 0
        relations_created = 0
        for r in records:
            text = r['text']
            is_binding = 'CODESYS' in text or 'Local HMI' in text
            has_chinese = any('\u4e00' <= c <= '\u9fff' for c in text)
            is_m0s0i = bool(re.match(r'M\d+S\d+[IQ]\d+', text))
            if is_binding and has_chinese:
                stats['mixed'] += 1
                nc, nr = self._process_mixed_record(r, store)
            elif is_binding:
                stats['binding'] += 1
                nc, nr = self._process_binding_record(r, store)
            elif is_m0s0i:
                stats['m0s0i'] += 1
                nc, nr = 0, self._process_m0s0i_record(r, store)
            elif has_chinese:
                stats['label'] += 1
                nc, nr = self._process_label_record(r, store)
            else:
                stats['other'] += 1
                nc, nr = 0, 0
            components_created += nc
            relations_created += nr
        return {
            'total_records': len(records), 'mixed_count': stats['mixed'],
            'binding_count': stats['binding'], 'label_count': stats['label'],
            'm0s0i_count': stats['m0s0i'], 'components_created': components_created,
            'relations_created': relations_created,
        }

    def _scan_records(self, data: bytes) -> list:
        records = []
        i = 0
        while i < len(data) - 4:
            length = data[i] | (data[i + 1] << 8)
            if 4 <= length <= 300 and i + 2 + length <= len(data):
                raw = data[i + 2:i + 2 + length]
                try:
                    text = raw.decode('utf-8')
                    if sum(1 for c in text if c.isprintable()) >= length * 0.7:
                        prefix4 = data[i - 4:i] if i >= 4 else b'\x00\x00\x00\x00'
                        records.append({'pos': i, 'length': length, 'text': text, 'prefix4': prefix4.hex()})
                        i += 2 + length
                        continue
                except UnicodeDecodeError:
                    pass
            i += 1
        return records

    def _new_component(self, record: dict, display: str, store: EvidenceStore) -> Entity:
        comp = Entity(make_entity_id('hmi_component', f'comp@{record["pos"]}'), 'hmi_component', f'comp@{record["pos"]}', display_name=display, scope='')
        store.add_entity(comp)
        return comp

    def _get_or_create_var(self, base_var: str, store: EvidenceStore) -> Entity:
        candidates = store.find_by_key('plc_variable', base_var)
        if candidates:
            return candidates[0]
        var_entity = Entity(make_entity_id('plc_variable', base_var), 'plc_variable', base_var, base_var, scope='')
        store.add_entity(var_entity)
        return var_entity

    def _process_mixed_record(self, record: dict, store: EvidenceStore) -> tuple:
        text = record['text']
        m = self.BINDING_PATH_PATTERN.search(text)
        if not m:
            return (0, 0)
        binding_path = m.group(1)
        var_path = binding_path[len('Application.'):] if binding_path.startswith('Application.') else binding_path
        base_var = self._extract_var_name(var_path)
        if not base_var:
            return (0, 0)

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
            return (0, 0)

        comp = self._new_component(record, f'HMI组件({chinese_text})', store)
        var_entity = self._get_or_create_var(base_var, store)
        label_entity = Entity(make_entity_id('hmi_label', chinese_text), 'hmi_label', chinese_text, chinese_text, scope='')
        store.add_entity(label_entity)
        store.add_relation(Relation(comp.entity_id, var_entity.entity_id, 'binds_to', 'exact', 'hmi_mixed_record', f'pos={record["pos"]}: {text[:80]}'))
        store.add_relation(Relation(comp.entity_id, label_entity.entity_id, 'labelled_as', 'exact', 'hmi_mixed_record', f'pos={record["pos"]}'))
        store.add_relation(Relation(var_entity.entity_id, label_entity.entity_id, 'labelled_as', 'exact', 'hmi_mixed_record_direct', f'{base_var} → {chinese_text}'))
        return (1, 3)

    def _process_binding_record(self, record: dict, store: EvidenceStore) -> tuple:
        text = record['text']
        m = self.BINDING_PATH_PATTERN.search(text)
        if not m:
            return (0, 0)
        binding_path = m.group(1)
        var_path = binding_path[len('Application.'):] if binding_path.startswith('Application.') else binding_path
        base_var = self._extract_var_name(var_path)
        if not base_var:
            return (0, 0)
        comp = self._new_component(record, f'HMI组件({base_var})', store)
        var_entity = self._get_or_create_var(base_var, store)
        store.add_relation(Relation(comp.entity_id, var_entity.entity_id, 'binds_to', 'structural', 'hmi_binding_record', f'pos={record["pos"]}: {binding_path}'))
        return (1, 1)

    def _process_label_record(self, record: dict, store: EvidenceStore) -> tuple:
        text = record['text'].strip()
        if len(text) < 2:
            return (0, 0)
        comp = self._new_component(record, f'HMI组件({text[:20]})', store)
        label_entity = Entity(make_entity_id('hmi_label', text[:100]), 'hmi_label', text[:100], text[:100], scope='')
        store.add_entity(label_entity)
        store.add_relation(Relation(comp.entity_id, label_entity.entity_id, 'labelled_as', 'structural', 'hmi_label_record', f'pos={record["pos"]}'))
        return (1, 1)

    def _process_m0s0i_record(self, record: dict, store: EvidenceStore) -> int:
        text = record['text'].strip()
        m = re.match(r'(M\d+S\d+[IQ]\d+)[_\s]*(.+)', text)
        if not m:
            return 0
        code, label_text = m.group(1), m.group(2).strip()
        addr_entity = Entity(make_entity_id('physical_address', code), 'physical_address', code, code, scope='')
        store.add_entity(addr_entity)
        label_entity = Entity(make_entity_id('hmi_label', label_text), 'hmi_label', label_text, label_text, scope='')
        store.add_entity(label_entity)
        store.add_relation(Relation(addr_entity.entity_id, label_entity.entity_id, 'labelled_as', 'exact', 'hmi_m0s0i_structured', f'pos={record["pos"]}'))
        for pc in store.find_by_key('plc_variable', code):
            store.add_relation(Relation(pc.entity_id, addr_entity.entity_id, 'maps_to', 'exact', 'hmi_m0s0i_structured_match', f'PLC {code} 匹配 M0S0I'))
        return 1

    def _extract_var_name(self, var_path: str) -> str:
        if not var_path:
            return ''
        if var_path.startswith(('LW', 'RW', 'Y', 'X')):
            return var_path
        namespace_segments = {'Parameter', 'PartContolVar', 'HANS', 'Hans', 'CATL', 'Local_HMI', 'Custom', 'CQ', 'Station', 'MachineOpen', 'MachineO', 'System_Var', 'Application'}
        for part in reversed(var_path.split('.')):
            base = re.sub(r'\[\d+\]', '', part).strip()
            if base and base not in namespace_segments:
                return base
        return ''

    def _empty_stats(self) -> dict:
        return {'total_records': 0, 'mixed_count': 0, 'binding_count': 0, 'label_count': 0, 'm0s0i_count': 0, 'components_created': 0, 'relations_created': 0}


# ============================================================
# FBCallGraphExtractor — FB/POU 调用链
# ============================================================

class FBCallGraphExtractor:
    """PLCopen XML LD/FBD block 调用链。
    关系: calls / instance_of / input_param / output_param。
    """

    def extract(self, xml_path: str, store: EvidenceStore) -> dict:
        path = Path(xml_path)
        if not path.exists():
            return {'fb_types': 0, 'fb_instances': 0, 'fb_calls': 0, 'param_bindings': 0}
        stats = {'fb_types': 0, 'fb_instances': 0, 'fb_calls': 0, 'param_bindings': 0}
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            pou_names = set(re.findall(r'<pou\s+name="([^"]+)"', content))
            action_positions = [(m.start(), m.group(1)) for m in re.finditer(r'<action\s+name="([^"]+)"', content)]
            for i, (action_start, action_name) in enumerate(action_positions):
                action_end = action_positions[i + 1][0] if i + 1 < len(action_positions) else len(content)
                action_body = content[action_start:action_end]
                pou_name = self._find_pou_for_action(content, action_start, pou_names)

                local_ids: dict = {}
                var_pattern = re.compile(r'<(?:outVariable|inVariable|inOutVariable)\s+localId="([^"]+)"[^>]*>.*?<expression>([^<]*)</expression>', re.DOTALL)
                for m in var_pattern.finditer(action_body):
                    local_ids[m.group(1)] = m.group(2).strip()

                block_pattern = re.compile(r'<block\s+localId="([^"]*)"\s+typeName="([^"]*)"\s+instanceName="([^"]*)"\s*>(.*?)</block>', re.DOTALL)
                blocks_in_action = []
                for b_m in block_pattern.finditer(action_body):
                    blocks_in_action.append({
                        'instance': b_m.group(3), 'fb_type': b_m.group(2),
                        'local_id': b_m.group(1), 'action': action_name,
                        'params': self._parse_params_from_xml(b_m.group(4), local_ids),
                    })
                if blocks_in_action and pou_name:
                    s = self._process_pou(pou_name, blocks_in_action, store)
                    for k in stats:
                        stats[k] += s.get(k, 0)
        except Exception as e:
            print(f'[FBCallGraphExtractor] 异常: {type(e).__name__}: {e}', file=sys.stderr)
        return stats

    def _find_pou_for_action(self, content: str, action_pos: int, pou_names: set) -> str:
        before = content[:action_pos]
        pou_opens = list(re.finditer(r'<pou\s+name="([^"]+)"', before))
        pou_closes = list(re.finditer(r'</pou>', before))
        if (len(pou_opens) - len(pou_closes)) > 0 and pou_opens:
            return pou_opens[-1].group(1)
        if pou_opens:
            return pou_opens[-1].group(1)
        return ''

    def _parse_params_from_xml(self, block_body: str, local_ids: dict) -> list:
        results = []
        for section, direction in (('inputVariables', 'input'), ('outputVariables', 'output'), ('inOutVariables', 'inout')):
            sec = re.search(rf'<{section}>(.*?)</{section}>', block_body, re.DOTALL)
            if sec:
                results.extend(self._parse_param_section_regex(sec.group(1), direction, local_ids))
        return results

    def _parse_param_section_regex(self, section_xml: str, direction: str, local_ids: dict) -> list:
        results = []
        for m in re.finditer(r'<variable\s+formalParameter="([^"]+)"[^>]*>(.*?)</variable>', section_xml, re.DOTALL):
            var_body = m.group(2)
            expr = ''
            ref_m = re.search(r'refLocalId="([^"]+)"', var_body)
            if ref_m:
                expr = local_ids.get(ref_m.group(1), '')
            if not expr:
                exp_m = re.search(r'<expression>([^<]*)</expression>', var_body)
                if exp_m:
                    expr = exp_m.group(1).strip()
            results.append({'name': m.group(1), 'direction': direction, 'expression': expr})
        return results

    def _process_pou(self, pou_name: str, blocks: list, store: EvidenceStore) -> dict:
        stats = {'fb_types': 0, 'fb_instances': 0, 'fb_calls': 0, 'param_bindings': 0}
        pou_id = make_entity_id('pou', pou_name)
        store.add_entity(Entity(pou_id, 'pou', pou_name, pou_name, scope=''))
        for block_info in blocks:
            instance_name = block_info['instance']
            fb_type = block_info['fb_type']
            action_name = block_info.get('action', '')

            fb_type_id = make_entity_id('datatype', fb_type)
            store.add_entity(Entity(fb_type_id, 'datatype', fb_type, fb_type, scope=''))
            stats['fb_types'] += 1

            scope_parts = [f'pou:{pou_name}'] + ([f'action:{action_name}'] if action_name else [])
            instance_scope = '/'.join(scope_parts)
            fb_instance_id = make_entity_id('fb_instance', instance_name, scope=instance_scope)
            store.add_entity(Entity(fb_instance_id, 'fb_instance', instance_name, instance_name, scope=instance_scope))
            stats['fb_instances'] += 1

            store.add_relation(Relation(fb_instance_id, fb_type_id, 'instance_of', 'exact', 'plcopen_block_typeName', f'typeName={fb_type}'))
            call_detail = f'POU {pou_name}' + (f' action {action_name}' if action_name else '') + f' 内 block {instance_name}'
            store.add_relation(Relation(pou_id, fb_instance_id, 'calls', 'exact', 'plcopen_block_in_pou', call_detail))
            stats['fb_calls'] += 1

            for param in block_info.get('params', []):
                expr = param['expression']
                if not expr:
                    continue
                base_var = self._extract_base_var(expr)
                if not base_var:
                    continue
                var_entity = self._get_or_create_var(base_var, store)
                rel_type = 'input_param' if param['direction'] == 'input' else 'output_param'
                store.add_relation(Relation(fb_instance_id, var_entity.entity_id, rel_type, 'exact', 'plcopen_block_param_binding', f'FB {instance_name} {param["direction"]} {param["name"]} = {expr}'))
                stats['param_bindings'] += 1
        return stats

    def _get_or_create_var(self, base_var: str, store: EvidenceStore) -> Entity:
        candidates = store.find_by_key('plc_variable', base_var)
        if candidates:
            return candidates[0]
        var_entity = Entity(make_entity_id('plc_variable', base_var), 'plc_variable', base_var, base_var, scope='')
        store.add_entity(var_entity)
        return var_entity

    def _extract_base_var(self, expr: str) -> str:
        if not expr:
            return ''
        expr_clean = re.sub(r'^[A-Z]{2}\d{2}V?\.', '', expr.strip())
        m = re.match(r'^([A-Za-z_]\w*)', expr_clean)
        return m.group(1) if m else ''


# ============================================================
# 统一入口 — build_evidence_store
# ============================================================

def build_evidence_store(plc_path: str, hmi_path: str | None = None) -> EvidenceStore:
    """从 PLC 源码 (+ 可选 HMI) 构建 EvidenceStore。

    Args:
        plc_path: PLC 源码路径。可为 .xml 文件或目录(目录则取第一个 .xml)。
                  自动识别 PLCopen XML 与 SMC2 XML 格式。
        hmi_path: HMI 路径(可选)。可为文件或目录(目录则搜索 MT8000 project)。

    Returns:
        EvidenceStore。调用方可执行 store.save(path) 持久化为 JSON。
    """
    store = EvidenceStore()
    plc = Path(plc_path) if plc_path else None
    if plc and plc.is_dir():
        xmls = sorted(plc.rglob('*.xml'))
        plc = xmls[0] if xmls else None

    if plc and plc.is_file():
        if _is_smc2_xml(plc):
            PLCVarDocExtractor().extract_from_smc2_xml(str(plc), store)
        else:
            PLCVarDocExtractor().extract_from_plcopen_xml(str(plc), store)
            POUStationExtractor().extract_all_pous_from_xml(str(plc), store)
            FBCallGraphExtractor().extract(str(plc), store)
            if plc.name:
                POUStationExtractor().extract_from_filename(plc.name, store)

    if hmi_path:
        hmi_file = _resolve_hmi_path(hmi_path)
        if hmi_file:
            HMIExtractor().extract(hmi_file, store)
            HMIComponentExtractor().extract(hmi_file, store)
            DirectSymbolBindingExtractor().extract(hmi_file, store)
    return store
