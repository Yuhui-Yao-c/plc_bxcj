#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inspectors.py — 文件格式探测与结构扫描工具库

本模块由 opencode (AI agent) 调用，用于探测 PLC/HMI 工程文件的格式与结构。
合并并精简自 plc_zjph/inspector/generic_source_inspector.py 与 table_inspector.py，
仅保留本工程必需的核心探测功能，且不依赖任何第三方库（仅标准库）。

提供两个统一接口:
  inspect_source(path) -> dict
      探测文件结构，返回 summary dict。支持:
        - PLCopen XML:  扫描标签频率、CDATA 数量、变量模式(数组访问/赋值/比较)、字符串模式
        - HMI 二进制:   检测 MT8000 magic，扫描结构化记录(binding/label/mixed/m0s0i 分类)
        - Excel(.xlsx): 扫描 sheet 名、列名
        - JSON:         顶层 key / 记录数
  parse_trace(trace_path) -> set[tuple[int, int]]
      解析 trace.csv，找到 m_nNextAutoStep[N] 列，返回出现过的 (thread, step_value) 集合

设计原则:
  1. 只描述"文件结构是什么"，不判断业务语义
  2. 仅使用 Python 标准库
  3. 支持大文件流式扫描
"""
from __future__ import annotations
import csv
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET


MT8000_MAGIC = b'MT8000Series'

_XLSX_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'

_SKIP_WORDS = frozenset({
    'type', 'name', 'value', 'count', 'class', 'true', 'false', 'null',
    'none', 'string', 'self', 'cls', 'http', 'https', 'www', 'entry',
    'group', 'json', 'xml',
})


def _detect_file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == '.xml':
        return 'xml'
    if suffix == '.json':
        return 'json'
    if suffix in ('.xlsx', '.xls'):
        return 'excel'
    if suffix == '.csv':
        return 'csv'
    with open(path, 'rb') as f:
        head = f.read(20)
    if head[:len(MT8000_MAGIC)] == MT8000_MAGIC:
        return 'hmi_binary'
    if head[:5] == b'<?xml':
        return 'xml'
    if head[:1] in (b'{', b'['):
        return 'json'
    return 'unknown'


def _detect_encoding(path: Path) -> str:
    with open(path, 'rb') as f:
        head = f.read(4)
    if head[:3] == b'\xef\xbb\xbf':
        return 'utf-8-sig'
    if head[:2] == b'\xff\xfe':
        return 'utf-16-le'
    if head[:2] == b'\xfe\xff':
        return 'utf-16-be'
    try:
        with open(path, 'r', encoding='utf-8') as f:
            f.read(1024)
        return 'utf-8'
    except (UnicodeDecodeError, UnicodeError):
        return 'latin-1'


def _detect_magic(path: Path) -> str:
    with open(path, 'rb') as f:
        head = f.read(16)
    return head.hex()[:32]


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def _scan_xml_structure(path: Path, max_events: int = 50000) -> dict:
    tag_counter = Counter()
    attr_counter = Counter()
    cdata_count = 0
    text_lengths = []
    root_tag = ''
    namespace = ''
    sample_attrs = {}

    context = ET.iterparse(str(path), events=('start',))
    event_count = 0
    try:
        for event, elem in context:
            event_count += 1
            if event_count > max_events:
                break
            tag = _strip_ns(elem.tag)
            if not root_tag:
                root_tag = tag
                m = re.match(r'\{(.+?)\}', elem.tag)
                if m:
                    namespace = m.group(1)
            tag_counter[tag] += 1
            for ak, av in elem.attrib.items():
                ak_clean = _strip_ns(ak)
                attr_counter[ak_clean] += 1
                if ak_clean not in sample_attrs and len(sample_attrs) < 20:
                    sample_attrs[ak_clean] = str(av)[:100]
            if elem.text and elem.text.strip():
                txt = elem.text.strip()
                if txt.startswith('{') or txt.startswith('['):
                    cdata_count += 1
                text_lengths.append(len(txt))
            elem.clear()
    except ET.ParseError as e:
        return {'error': f'XML parse error: {e}'}
    finally:
        if hasattr(context, 'close'):
            context.close()

    text_length_stats = {}
    if text_lengths:
        text_length_stats = {
            'count': len(text_lengths),
            'min': min(text_lengths),
            'max': max(text_lengths),
            'avg': round(sum(text_lengths) / len(text_lengths), 1),
        }
    return {
        'root_tag': root_tag,
        'namespace': namespace,
        'tag_frequency': dict(tag_counter.most_common(30)),
        'attribute_frequency': dict(attr_counter.most_common(20)),
        'cdata_count': cdata_count,
        'text_length_stats': text_length_stats,
        'sample_attributes': sample_attrs,
        'events_scanned': event_count,
    }


def _scan_string_patterns(path: Path, encoding: str,
                          max_chars: int = 5 * 1024 * 1024) -> dict:
    try:
        with open(path, 'r', encoding=encoding, errors='replace') as f:
            content = f.read(max_chars)
    except Exception:
        return {}

    array_access = Counter()
    for m in re.finditer(r'(\w+)\[(\d+)\]', content):
        array_access[m.group(1)] += 1

    assignment_patterns = Counter()
    for m in re.finditer(r'(\w+(?:\[\d+\])?(?:\.\w+)*)\s*:=\s*(\S+)', content):
        assignment_patterns[m.group(1)] += 1
    for m in re.finditer(r'(\w+(?:\[\d+\])?(?:\.\w+)*)\s*=\s*INT#?(\d+)', content):
        assignment_patterns[m.group(1)] += 1

    comparison_patterns = Counter()
    for m in re.finditer(r'(\w+(?:\[\d+\])?(?:\.\w+)*)\s*==\s*(\S+)', content):
        comparison_patterns[f'{m.group(1)} == {m.group(2)}'] += 1
    for m in re.finditer(r'(\w+(?:\[\d+\])?(?:\.\w+)*)\s*!=\s*(\S+)', content):
        comparison_patterns[f'{m.group(1)} != {m.group(2)}'] += 1

    address_patterns = Counter()
    for m in re.finditer(r'(%[IQXWM]\d+\.?\d*)', content):
        address_patterns[m.group(1)[:8]] += 1
    for m in re.finditer(r'(Y\d{6})', content):
        address_patterns['Y######'] += 1

    word_freq = Counter()
    for m in re.finditer(r'\b([a-zA-Z_]\w{2,30})\b', content):
        w = m.group(1)
        if w.lower() not in _SKIP_WORDS:
            word_freq[w] += 1

    return {
        'array_access_frequency': dict(array_access.most_common(30)),
        'assignment_patterns': dict(assignment_patterns.most_common(20)),
        'comparison_patterns': dict(comparison_patterns.most_common(20)),
        'address_patterns': dict(address_patterns.most_common(10)),
        'high_freq_words': dict(word_freq.most_common(30)),
        'scanned_chars': len(content),
    }


def _scan_hmi_binary_structure(path: Path) -> dict:
    with open(path, 'rb') as f:
        data = f.read()

    is_mt8000 = data[:len(MT8000_MAGIC)] == MT8000_MAGIC
    if not is_mt8000:
        return {'is_mt8000': False,
                'file_size_mb': round(len(data) / 1024 / 1024, 2)}

    records = []
    i = 0
    while i < len(data) - 4:
        length = data[i] | (data[i + 1] << 8)
        if 4 <= length <= 300 and i + 2 + length <= len(data):
            raw = data[i + 2:i + 2 + length]
            try:
                text = raw.decode('utf-8')
                printable = sum(1 for c in text if c.isprintable())
                if printable >= length * 0.7:
                    prefix4 = data[i - 4:i] if i >= 4 else b'\x00\x00\x00\x00'
                    records.append({
                        'pos': i,
                        'length': length,
                        'text': text,
                        'prefix4': prefix4.hex(),
                    })
                    i += 2 + length
                    continue
            except UnicodeDecodeError:
                pass
        i += 1

    categories = Counter()
    binding_namespaces = Counter()
    mixed_samples = []
    label_samples = []
    binding_samples = []
    m0s0i_samples = []
    prefix4_dist = Counter()

    for r in records:
        t = r['text']
        is_binding = 'CODESYS' in t or 'Local HMI' in t
        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in t)
        is_m0s0i = ('M0S0I' in t or 'M0S1Q' in t
                    or 'M0S3Q' in t or 'M0S4Q' in t)

        if is_binding:
            prefix4_dist[r['prefix4']] += 1
            if 'Application.' in t:
                ns = t.split('Application.')[1].split('.')[0]
                binding_namespaces[ns] += 1
            elif 'Local HMI' in t:
                binding_namespaces['Local_HMI'] += 1
            if has_chinese:
                categories['mixed'] += 1
                if len(mixed_samples) < 20:
                    mixed_samples.append({'pos': r['pos'],
                                          'text': t[:150],
                                          'prefix4': r['prefix4']})
            else:
                categories['binding'] += 1
                if len(binding_samples) < 10:
                    binding_samples.append({'pos': r['pos'],
                                            'text': t[:150],
                                            'prefix4': r['prefix4']})
        elif is_m0s0i:
            categories['m0s0i'] += 1
            if len(m0s0i_samples) < 10:
                m0s0i_samples.append({'pos': r['pos'],
                                      'text': t[:100],
                                      'prefix4': r['prefix4']})
        elif has_chinese:
            categories['label'] += 1
            if len(label_samples) < 10:
                label_samples.append({'pos': r['pos'],
                                      'text': t[:100],
                                      'prefix4': r['prefix4']})
        else:
            categories['other'] += 1

    return {
        'is_mt8000': True,
        'file_size_mb': round(len(data) / 1024 / 1024, 2),
        'total_records': len(records),
        'categories': dict(categories),
        'binding_namespaces': dict(binding_namespaces.most_common(15)),
        'mixed_records_sample': mixed_samples,
        'label_records_sample': label_samples,
        'binding_records_sample': binding_samples,
        'm0s0i_records_sample': m0s0i_samples,
        'prefix4_distribution': dict(prefix4_dist.most_common(15)),
    }


def _scan_excel_structure(path: Path) -> dict:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        return {'error': f'cannot read xlsx: {e}'}

    sheets_info = []
    try:
        shared_strings = []
        try:
            with zf.open('xl/sharedStrings.xml') as f:
                tree = ET.parse(f)
            for si in tree.getroot().findall(f'{_XLSX_NS}si'):
                texts = ''.join(t.text or '' for t in si.iter(f'{_XLSX_NS}t'))
                shared_strings.append(texts)
        except KeyError:
            pass

        sheet_names = []
        try:
            with zf.open('xl/workbook.xml') as f:
                wb_tree = ET.parse(f)
            for sheet in wb_tree.getroot().iter(f'{_XLSX_NS}sheet'):
                sheet_names.append(sheet.attrib.get('name', ''))
        except (KeyError, ET.ParseError):
            pass

        sheet_files = sorted(
            n for n in zf.namelist()
            if re.match(r'xl/worksheets/sheet\d+\.xml$', n)
        )

        for idx, sn in enumerate(sheet_names):
            columns = []
            row_count = 0
            sheet_file = (sheet_files[idx]
                          if idx < len(sheet_files) else None)
            if sheet_file:
                try:
                    with zf.open(sheet_file) as f:
                        sheet_tree = ET.parse(f)
                    sd = sheet_tree.getroot().find(f'{_XLSX_NS}sheetData')
                    if sd is not None:
                        rows = sd.findall(f'{_XLSX_NS}row')
                        row_count = len(rows)
                        if rows:
                            for c in rows[0].findall(f'{_XLSX_NS}c'):
                                cell_type = c.attrib.get('t', '')
                                val = ''
                                v_elem = c.find(f'{_XLSX_NS}v')
                                if v_elem is not None and v_elem.text:
                                    if cell_type == 's':
                                        try:
                                            val = shared_strings[int(v_elem.text)]
                                        except (ValueError, IndexError):
                                            val = v_elem.text
                                    else:
                                        val = v_elem.text
                                is_elem = c.find(f'{_XLSX_NS}is')
                                if is_elem is not None:
                                    val = ''.join(
                                        t.text or ''
                                        for t in is_elem.iter(f'{_XLSX_NS}t'))
                                columns.append(str(val)[:80])
                except (KeyError, ET.ParseError):
                    pass
            sheets_info.append({
                'name': sn,
                'row_count': row_count,
                'columns': columns,
            })
    finally:
        zf.close()

    return {
        'sheets': sheets_info,
        'sheet_count': len(sheets_info),
    }


def inspect_source(path) -> dict:
    """探测文件结构，返回 summary dict。

    自动识别 PLCopen XML / HMI MT8000 二进制 / Excel(.xlsx) / JSON，
    并执行对应的结构扫描。不识别的格式仅返回文件层信息。
    """
    path = Path(path)
    if not path.exists():
        return {'error': f'File not found: {path}'}

    result = {
        'file_name': path.name,
        'file_size_mb': round(path.stat().st_size / 1024 / 1024, 2),
    }

    result['file_type'] = _detect_file_type(path)
    result['encoding'] = _detect_encoding(path)
    result['magic_hex'] = _detect_magic(path)

    if result['file_type'] == 'hmi_binary':
        result['hmi_structure'] = _scan_hmi_binary_structure(path)
        return result

    if result['file_type'] == 'xml':
        result['xml_structure'] = _scan_xml_structure(path)
        string_patterns = _scan_string_patterns(path, result['encoding'])
        if string_patterns:
            result['string_patterns'] = string_patterns
        return result

    if result['file_type'] == 'excel':
        result['excel_structure'] = _scan_excel_structure(path)
        return result

    if result['file_type'] == 'json':
        try:
            with open(path, 'r', encoding=result['encoding']) as f:
                data = json.load(f)
            if isinstance(data, dict):
                result['json_top_keys'] = list(data.keys())[:30]
            elif isinstance(data, list):
                result['json_record_count'] = len(data)
                if data and isinstance(data[0], dict):
                    result['json_top_keys'] = list(data[0].keys())[:30]
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            result['json_error'] = str(e)
        return result

    return result


def parse_trace(trace_path) -> set:
    """解析 trace.csv，返回出现过的步序集合 {(thread, step_value)}。

    在表头中查找形如 m_nNextAutoStep[N] 的列，N 即线程号；
    遍历所有数据行，将每个非空单元格解析为整数，收集 (N, value) 对。
    """
    trace_path = Path(trace_path)
    if not trace_path.exists():
        return set()

    encoding = _detect_encoding(trace_path)
    if encoding == 'latin-1':
        encoding = 'utf-8-sig'

    step_pattern = re.compile(r'm_nNextAutoStep\[(\d+)\]')
    thread_columns = {}
    steps = set()

    try:
        with open(trace_path, 'r', encoding=encoding, errors='replace',
                  newline='') as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return set()

            for col_idx, col_name in enumerate(header):
                m = step_pattern.match(col_name.strip())
                if m:
                    thread_columns[col_idx] = int(m.group(1))

            if not thread_columns:
                return set()

            for row in reader:
                for col_idx, thread in thread_columns.items():
                    if col_idx < len(row):
                        val = row[col_idx].strip()
                        if val:
                            try:
                                steps.add((thread, int(val)))
                            except ValueError:
                                pass
    except OSError:
        return set()

    return steps
