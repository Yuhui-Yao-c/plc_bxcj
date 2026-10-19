#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plc_bxcj/lib/primitives.py — PLCopen 步序提取工具库（opencode 调用）

本模块由 opencode 在 Phase 4（提取步序）调用，合并自 plc_zjph 的三个源文件：
  - primitives/plcopen_graph.py: PLCopen 图形化梯形图 Step Discovery
    （graph_comparison + st_code + comment_only 三策略）
  - adaptation/element_extractor.py: XML 元素值提取辅助函数
  - primitives/step_logic_extractor.py: Step 级逻辑提取器

设计原则：
  1. opencode 是大脑，Python 是手脚——本模块只做确定性提取，不做语义判断
  2. 配置用普通 dict 传入，不依赖 adaptation/schema.py 的 dataclass
  3. 所有提取结果带 evidence，可追溯
  4. 不猜测：宁可 unresolved，也不编造逻辑关系

统一接口：
  - discover_steps_plcopen(xml_files, config): 提取步序候选
  - extract_step_logic_plcopen(xml_files, config): 提取每步完整逻辑

config dict 关键字段（opencode 根据分析结果生成）：
  必填:
    - step_var_name: 步序变量名（如 "Process_steps"）
    - eq_block_type: 比较块类型名（如 "EQ"）
    - strategies: 策略列表（如 ["graph_comparison", "st_code", "comment_only"]）
    - comment_step_pattern: 注释步序正则（如 r"^(\\d+)"）
  可选（step_logic 用）:
    - assignment_element_types: 赋值元素类型（默认 ["coil"]）
    - condition_element_types: 条件元素类型（默认 ["contact"]）
    - transition_block_types: 转移块类型（默认 ["MOVE"]）
    - timer_block_types: 定时器类型（默认 ["TON", "TOF", "TP"]）
    - st_code_block_types: ST 代码块类型（默认 ["EXECUTE"]）
    - call_block_types: 函数调用块类型（空=提取所有非已知块）
    - common_enable_vars: 公共使能变量列表
    - file_station_map: {filename: station_id}

返回的 step_candidate / step_logic dict 字段与 plc_zjph 保持一致，
便于 opencode 复用分析经验。本模块自包含，不依赖 lib 包外任何模块。

Canonical IR 接口:
  discover_steps_from_decoded(decoded_programs, config) — 接受 DecodedProgram
  extract_step_logic_from_decoded(decoded_programs, config) — 接受 DecodedProgram

  这两个函数是 Canonical IR 接口的入口。内部仍使用稳定的 XML 算法。
  未来新 Primitive (如 SMC2) 可直接消费 Canonical IR，不需要 XML 文件。
"""
from __future__ import annotations
import re
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def _get_ns_tag(root) -> str:
    ns = ''
    m = re.match(r'\{(.+?)\}', root.tag)
    if m:
        ns = m.group(1)
    return f'{{{ns}}}' if ns else ''


def _build_elements_map(root) -> dict:
    elements = {}
    for elem in root.iter():
        lid = elem.get('localId')
        if lid:
            elements[lid] = elem
    return elements


def _detect_station(file_name: str, file_station_map: dict = None,
                    station_pattern: str = None) -> str:
    """Detect station code from file name.

    station_pattern is a regex string with one capture group for the station
    code. If not provided, uses a generic fallback pattern (not a project
    convention — project conventions come from ProgramPatternSpec via config).
    """
    if file_station_map and file_name in file_station_map:
        return file_station_map[file_name]
    if station_pattern:
        station_re = re.compile(station_pattern)
    else:
        # Generic fallback: two uppercase letters + two digits
        station_re = re.compile(r'([A-Z]{2}\d{2})')
    m = station_re.search(file_name)
    if m:
        return m.group(1)
    return ''


def extract_element_expression(elem, elements=None, ns_tag: str = '') -> str:
    """从单个 XML 元素提取其表达式文本

    支持 inVariable / inOutVariable / outVariable / contact / coil /
    leftPowerRail / block / comment 等类型，对 connection 引用自动解引用。
    """
    if elem is None:
        return ''
    tag = _strip_ns(elem.tag)

    if tag in ('inVariable', 'inOutVariable', 'outVariable'):
        for e in elem.iter(f'{ns_tag}expression'):
            txt = (e.text or '').strip()
            if txt:
                return txt
        return ''

    if tag == 'contact':
        var_elem = elem.find(f'{ns_tag}variable')
        if var_elem is not None and var_elem.text:
            var = var_elem.text.strip()
            negated = elem.get('negated', 'false') == 'true'
            return f'NOT {var}' if negated else var
        return ''

    if tag == 'coil':
        var_elem = elem.find(f'{ns_tag}variable')
        if var_elem is not None and var_elem.text:
            return var_elem.text.strip()
        return ''

    if tag == 'leftPowerRail':
        return 'TRUE'

    if tag == 'block':
        return elem.get('typeName', '')

    if tag == 'comment':
        return ''

    for e in elem.iter(f'{ns_tag}expression'):
        txt = (e.text or '').strip()
        if txt:
            return txt
    var_elem = elem.find(f'{ns_tag}variable')
    if var_elem is not None and var_elem.text:
        return var_elem.text.strip()
    return elem.get('value', '')


def extract_block_inputs(block, elements: dict, ns_tag: str = '') -> list:
    """提取 block 元素的所有输入表达式

    遍历 inputVariables > variable > connectionPointIn > connection，
    对每个 connection 的 refLocalId 解引用。每个 variable 只取第一个有效 connection。
    """
    inputs = []
    iv = block.find(f'{ns_tag}inputVariables')
    if iv is None:
        return inputs
    for var in iv:
        for cp in var.iter(f'{ns_tag}connection'):
            ref = cp.get('refLocalId')
            if ref and ref in elements:
                expr = extract_element_expression(elements[ref], elements, ns_tag)
                if expr:
                    inputs.append(expr)
                    break
    return inputs


def extract_block_output_target(block, ns_tag: str = '') -> str:
    """提取 block 的输出写入目标（MOVE 块的赋值目标）

    遍历 outputVariables > variable（跳过 ENO）> expression。
    """
    ov = block.find(f'{ns_tag}outputVariables')
    if ov is None:
        return ''
    for var in ov:
        fp = var.get('formalParameter', '')
        if fp == 'ENO':
            continue
        for e in var.iter(f'{ns_tag}expression'):
            txt = (e.text or '').strip()
            if txt:
                return txt
    return ''


def is_literal_value(expr: str) -> bool:
    """判断表达式是否是字面量值（数字、INT#数字、BOOL#TRUE 等）"""
    if not expr:
        return False
    expr = expr.strip()
    if re.match(r'^-?\d+$', expr):
        return True
    if re.match(r'^\w+#\w+$', expr, re.IGNORECASE):
        return True
    if expr.upper() in ('TRUE', 'FALSE'):
        return True
    return False


def parse_literal_value(expr: str):
    """解析字面量表达式，返回 int / str / None"""
    if not expr:
        return None
    expr = expr.strip()
    m = re.match(r'^(\w+)#(.+)$', expr, re.IGNORECASE)
    if m:
        val_str = m.group(2)
        try:
            return int(val_str)
        except ValueError:
            return val_str
    try:
        return int(expr)
    except ValueError:
        pass
    if expr.upper() in ('TRUE', 'FALSE'):
        return expr.upper()
    return None


def _parse_literal(val):
    """解析字面值（支持 INT# 前缀），用于 step_logic"""
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        m = re.match(r'^(?:INT#)?(\d+)$', val.strip())
        if m:
            return int(m.group(1))
    return None


def _parse_timer_value(val: str) -> str:
    """解析定时器时间值（如 T#3S → 3s）"""
    if not val:
        return ''
    val = val.strip()
    m = re.match(r'^T#([\d.]+)(ms|s|S|m|h)$', val)
    if m:
        return m.group(1) + m.group(2).lower()
    return val


def _is_common_enable(var: str, common_enable_vars) -> bool:
    """判断变量是否属于 Agent 识别的公共使能条件（精确/前缀匹配）"""
    if not var or not common_enable_vars:
        return False
    for pattern in common_enable_vars:
        if var == pattern or var.startswith(pattern + '[') or var.startswith(pattern + '.'):
            return True
    return False


def _parse_xml_file(file_path):
    """解析 XML 文件，返回 (file_name, root) 或 None"""
    if not isinstance(file_path, (str, Path)):
        return None
    file_path = Path(file_path)
    file_name = file_path.name
    try:
        tree = ET.parse(str(file_path))
        root = tree.getroot()
    except Exception:
        return None
    return file_name, root


def discover_steps_plcopen(xml_files, config: dict) -> list:
    """从 PLCopen XML 文件列表提取步序候选

    实现 3 种 Step Discovery 策略:
      - graph_comparison: EQ/NE/GT 等图形比较块（step_var[idx] == value）
      - st_code: EXECUTE 块中的 ST 代码赋值（step_var[idx] := value）
      - comment_only: XML 注释中以数字开头的步序编号（补充）

    Args:
        xml_files: list of Path 或 str（PLCopen XML 文件路径）
        config: dict 配置，见模块 docstring

    Returns:
        step_candidates list，每个元素:
        {
            'station': str, 'thread_idx': int, 'value': int,
            'var_name': str, 'state_variable': str,
            'source_type': str, 'source_file': str,
            'evidence': {...}
        }
    """
    # Support multiple state variables (e.g. Process_steps + Init_Steps)
    state_vars_config = config.get('state_variables', [])
    if state_vars_config:
        step_var_names = [sv['name'] for sv in state_vars_config]
    else:
        single = config.get('step_var_name', '')
        step_var_names = [single] if single else []

    if not step_var_names:
        return []

    eq_block_type = config.get('eq_block_type', 'EQ')
    strategies = config.get('strategies', ['graph_comparison'])
    comment_pattern = config.get('comment_step_pattern', r'^(\d+)')
    file_station_map = config.get('file_station_map')
    # Filename station pattern: for extracting station from FILE NAMES.
    # This is a project convention from ProgramPatternSpec.
    # Default is generic two-letters-two-digits format.
    filename_station_pattern = config.get('filename_station_pattern')
    # Step variable extraction pattern: extracts (station, var_name, thread_idx)
    # from a state variable EXPRESSION like "EM06V.Process_steps[0]".
    # This is different from filename_station_pattern — variable expressions
    # have "V?\." suffix, file names have "_" suffix.
    step_var_extract_re = re.compile(
        config.get('step_var_extract_pattern',
                   r'([A-Z]{2}\d{2})V?\.(\w+)\[(\d+)\]'))

    # Build regex matching any state variable
    step_var_pattern = '|'.join(re.escape(v) for v in step_var_names)
    step_var_re = re.compile(step_var_pattern)

    all_candidates = []

    for file_path in xml_files:
        parsed = _parse_xml_file(file_path)
        if parsed is None:
            continue
        file_name, root = parsed

        ns_tag = _get_ns_tag(root)
        elements = _build_elements_map(root)
        file_station = _detect_station(file_name, file_station_map, filename_station_pattern)
        file_thread_steps = defaultdict(set)

        if 'graph_comparison' in strategies:
            for block in root.iter(f'{ns_tag}block'):
                if block.get('typeName') != eq_block_type:
                    continue
                inputs = extract_block_inputs(block, elements, ns_tag)
                if len(inputs) < 2:
                    continue

                step_var_input = None
                value_input = None
                for inp in inputs:
                    if step_var_re.search(inp) and re.search(r'\w+\[\d+\]', inp):
                        step_var_input = inp
                    elif is_literal_value(inp):
                        value_input = inp

                if step_var_input and value_input:
                    val = parse_literal_value(value_input)
                    if isinstance(val, int):
                        sv_m = step_var_extract_re.search(step_var_input)
                        if sv_m:
                            ext_station = sv_m.group(1)
                            ext_thread = int(sv_m.group(3))
                            ext_var = sv_m.group(2)
                        else:
                            sv_m2 = re.search(r'(\w+)\[(\d+)\]', step_var_input)
                            ext_station = file_station
                            ext_thread = int(sv_m2.group(2)) if sv_m2 else -1
                            ext_var = sv_m2.group(1) if sv_m2 else ''

                        file_thread_steps[ext_thread].add(val)

                        if val > 0:
                            all_candidates.append({
                                'station': ext_station or file_station,
                                'thread_idx': ext_thread,
                                'value': val,
                                'var_name': ext_var,
                                'state_variable': ext_var,
                                'source_type': 'graph_comparison',
                                'source_file': file_name,
                                'evidence': {'expr': f'{step_var_input}={val}'},
                            })

        if 'st_code' in strategies:
            for block in root.iter(f'{ns_tag}block'):
                if block.get('typeName') != 'EXECUTE':
                    continue
                stcode_text = _extract_block_stcode(block, ns_tag)
                if not stcode_text:
                    continue
                sv_pat = '|'.join(re.escape(v) for v in step_var_names)
                # Build ST code pattern from configurable step_var_extract_pattern
                # The pattern must capture: (station, var_name, thread_idx, value)
                st_pat_str = config.get('st_code_step_pattern')
                if st_pat_str:
                    st_pat = re.compile(st_pat_str.format(sv_pat=sv_pat))
                else:
                    st_pat = re.compile(
                        rf'([A-Z]{{2}}\d{{2}})V?\.({sv_pat})\[(\d+)\]\s*(?::=|=)\s*(\d+)')
                for m in st_pat.finditer(stcode_text):
                    val = int(m.group(4))
                    if val > 0:
                        all_candidates.append({
                            'station': m.group(1),
                            'thread_idx': int(m.group(3)),
                            'value': val,
                            'var_name': m.group(2),
                            'state_variable': m.group(2),
                            'source_type': 'st_code',
                            'source_file': file_name,
                            'evidence': {
                                'st_snippet': stcode_text[
                                    max(0, m.start() - 20):m.end() + 50]},
                        })

        if 'comment_only' in strategies and file_thread_steps:
            comment_re = re.compile(comment_pattern)
            for comment in root.iter(f'{ns_tag}comment'):
                content = comment.find(f'{ns_tag}content')
                if content is None:
                    continue
                for child in content:
                    txt = (child.text or '').strip()
                    if not txt:
                        continue
                    m = comment_re.match(txt)
                    if m:
                        step_val = int(m.group(1))
                        if step_val <= 0:
                            continue
                        for thread_idx, eq_vals in file_thread_steps.items():
                            if step_val not in eq_vals:
                                all_candidates.append({
                                    'station': file_station,
                                    'thread_idx': thread_idx,
                                    'value': step_val,
                                    'var_name': step_var_names[0],
                                    'state_variable': step_var_names[0],
                                    'source_type': 'comment_only',
                                    'source_file': file_name,
                                    'evidence': {'comment_text': txt[:200]},
                                })

    return all_candidates


def extract_step_logic_plcopen(xml_files, config: dict) -> list:
    """提取每个 Step 的完整逻辑上下文

    从 PLCopen XML 直接提取每步的 actions / comment / transitions /
    guards / timers / calls / st_code。步序候选先由内部 _discover_file_steps
    发现，再在同文件中扫描相关元素并归类到对应 step。

    相比 plc_zjph/step_logic_extractor.py 的简化:
      - 不依赖 source_schema dataclass，全部用 config dict
      - 直接从 XML 提取，无需预处理成 record dict
      - 核心: actions + comment；可选: guards / transitions / timers / calls / st_code

    Args:
        xml_files: list of Path 或 str
        config: dict 配置，见模块 docstring

    Returns:
        list of step_logic dict:
        {
            'thread_idx': int, 'step_value': int, 'station': str,
            'comment': str,
            'actions': [str],              # 输出赋值变量（排除 step_var）
            'guards': [str],               # 条件变量（排除 step_var）
            'common_enables': [str],       # 公共使能子集
            'transitions': [{target_thread, target_value, ...}],
            'timers': [{name, pt}],
            'calls': [{name}],
            'st_code': [str],              # 内嵌 ST 代码片段（前 200 字符）
            'source_file': str,
            'evidence': {...}
        }
    """
    # Support multiple state variables
    state_vars_config = config.get('state_variables', [])
    if state_vars_config:
        step_var_names = [sv['name'] for sv in state_vars_config]
    else:
        single = config.get('step_var_name', '')
        step_var_names = [single] if single else []

    eq_block_type = config.get('eq_block_type', 'EQ')
    strategies = config.get('strategies', ['graph_comparison'])
    comment_pattern = config.get('comment_step_pattern', r'^(\d+)')
    file_station_map = config.get('file_station_map')
    filename_station_pattern = config.get('filename_station_pattern')
    step_var_extract_re = re.compile(
        config.get('step_var_extract_pattern',
                   r'([A-Z]{2}\d{2})V?\.(\w+)\[(\d+)\]'))

    assignment_types = set(config.get('assignment_element_types', ['coil']))
    condition_types = set(config.get('condition_element_types', ['contact']))
    transition_types = set(config.get('transition_block_types', ['MOVE']))
    timer_types = set(config.get('timer_block_types', ['TON', 'TOF', 'TP']))
    st_code_types = set(config.get('st_code_block_types', ['EXECUTE']))
    call_block_types = set(config.get('call_block_types', []))
    common_enable_vars = config.get('common_enable_vars', [])

    if not step_var_names:
        return []

    # Build regex matching any state variable (non-capturing group)
    step_var_pattern = '|'.join(re.escape(v) for v in step_var_names)
    step_var_filter_re = re.compile(step_var_pattern)
    step_var_index_re = re.compile(r'(?:' + step_var_pattern + r')\[(\d+)\]')
    step_logic_map = {}

    for file_path in xml_files:
        parsed = _parse_xml_file(file_path)
        if parsed is None:
            continue
        file_name, root = parsed

        ns_tag = _get_ns_tag(root)
        elements = _build_elements_map(root)
        file_station = _detect_station(file_name, file_station_map, filename_station_pattern)

        # Run discovery for each state variable
        file_steps = []
        for sv_name in step_var_names:
            sv_config = dict(config)
            sv_config['step_var_name'] = sv_name
            sv_config['strategies'] = strategies
            file_steps.extend(_discover_file_steps(
                root, elements, ns_tag, sv_name, eq_block_type,
                strategies, comment_pattern, file_station, file_name,
                step_var_extract_re, filename_station_pattern, config))

        if not file_steps:
            continue

        classified = _classify_xml_elements(
            root, elements, ns_tag, step_var_filter_re,
            assignment_types, condition_types, transition_types,
            timer_types, st_code_types, call_block_types,
            step_var_pattern)
        file_comments = _collect_comments(root, ns_tag)

        for step in file_steps:
            # Include station in key to avoid collision between stations
            # (EM02 Process_steps[0]=100 and EM08 Process_steps[0]=100 are different steps)
            step_station = step.get('station', file_station)
            key = (step.get('state_variable', ''), step_station, step['thread_idx'], step['value'])
            if key not in step_logic_map:
                step_logic_map[key] = {
                    'thread_idx': step['thread_idx'],
                    'step_value': step['value'],
                    'state_variable': step.get('state_variable', ''),
                    'station': step_station,
                    'comment': '',
                    'actions': [],
                    'guards': [],
                    'common_enables': [],
                    'transitions': [],
                    'timers': [],
                    'calls': [],
                    'st_code': [],
                    'source_file': file_name,
                    'evidence': dict(step.get('evidence', {})),
                }
            logic = step_logic_map[key]

            _collect_actions(logic, classified, step_var_filter_re)
            _collect_guards(logic, classified, step_var_filter_re, common_enable_vars)
            _collect_transitions(logic, classified)
            _collect_timers(logic, classified)
            _collect_calls(logic, classified)
            _collect_st_code(logic, classified, step_var_filter_re, step_var_names[0])

            if not logic['comment']:
                matched = _match_comment(file_comments, step['value'], comment_pattern)
                if matched:
                    logic['comment'] = matched

    return list(step_logic_map.values())


def _extract_block_stcode(block, ns_tag: str) -> str:
    """从 EXECUTE block 中提取 STCode 文本"""
    for data in block.iter(f'{ns_tag}data'):
        if 'stcode' in data.get('name', '').lower():
            for child in data:
                if _strip_ns(child.tag) == 'STCode':
                    return (child.text or '').strip()
    return ''


def _discover_file_steps(root, elements, ns_tag, step_var_name, eq_block_type,
                         strategies, comment_pattern, file_station, file_name,
                         step_var_extract_re=None, filename_station_pattern=None,
                         config=None):
    """发现单文件内的步序候选（供 step_logic 关联使用）

    返回 list of {thread_idx, value, station, source_type, evidence}，
    比 discover_steps_plcopen 的候选精简（不含 var_name / source_file）。
    """
    steps = []
    file_thread_steps = defaultdict(set)
    if step_var_extract_re is None:
        step_var_extract_re = re.compile(
            r'([A-Z]{2}\d{2})V?\.(\w+)\[(\d+)\]')

    if 'graph_comparison' in strategies:
        for block in root.iter(f'{ns_tag}block'):
            if block.get('typeName') != eq_block_type:
                continue
            inputs = extract_block_inputs(block, elements, ns_tag)
            if len(inputs) < 2:
                continue
            step_var_input = None
            value_input = None
            for inp in inputs:
                if step_var_name in inp and re.search(r'\w+\[\d+\]', inp):
                    step_var_input = inp
                elif is_literal_value(inp):
                    value_input = inp
            if step_var_input and value_input:
                val = parse_literal_value(value_input)
                if isinstance(val, int):
                    sv_m = step_var_extract_re.search(step_var_input)
                    if sv_m:
                        ext_station = sv_m.group(1)
                        ext_thread = int(sv_m.group(3))
                    else:
                        sv_m2 = re.search(r'(\w+)\[(\d+)\]', step_var_input)
                        ext_station = file_station
                        ext_thread = int(sv_m2.group(2)) if sv_m2 else -1
                    file_thread_steps[ext_thread].add(val)
                    if val > 0:
                        steps.append({
                            'thread_idx': ext_thread,
                            'value': val,
                            'station': ext_station or file_station,
                            'state_variable': step_var_name,
                            'source_type': 'graph_comparison',
                            'evidence': {'expr': f'{step_var_input}={val}'},
                        })

    if 'st_code' in strategies:
        for block in root.iter(f'{ns_tag}block'):
            if block.get('typeName') != 'EXECUTE':
                continue
            stcode_text = _extract_block_stcode(block, ns_tag)
            if not stcode_text:
                continue
            sv = re.escape(step_var_name)
            st_pat_str = (config or {}).get('st_code_step_pattern')
            if st_pat_str:
                st_pat = re.compile(st_pat_str.format(sv_pat=sv))
            else:
                st_pat = re.compile(
                    rf'([A-Z]{{2}}\d{{2}})V?\.{sv}\[(\d+)\]\s*(?::=|=)\s*(\d+)')
            for m in st_pat.finditer(stcode_text):
                val = int(m.group(3))
                if val > 0:
                    steps.append({
                        'thread_idx': int(m.group(2)),
                        'value': val,
                        'station': m.group(1),
                        'state_variable': step_var_name,
                        'source_type': 'st_code',
                        'evidence': {
                            'st_snippet': stcode_text[
                                max(0, m.start() - 20):m.end() + 50]},
                    })

    if 'comment_only' in strategies and file_thread_steps:
        comment_re = re.compile(comment_pattern)
        for comment in root.iter(f'{ns_tag}comment'):
            content = comment.find(f'{ns_tag}content')
            if content is None:
                continue
            for child in content:
                txt = (child.text or '').strip()
                if not txt:
                    continue
                m = comment_re.match(txt)
                if m:
                    step_val = int(m.group(1))
                    if step_val <= 0:
                        continue
                    for thread_idx, eq_vals in file_thread_steps.items():
                        if step_val not in eq_vals:
                            steps.append({
                                'thread_idx': thread_idx,
                                'value': step_val,
                                'station': file_station,
                                'state_variable': step_var_name,
                                'source_type': 'comment_only',
                                'evidence': {'comment_text': txt[:200]},
                            })

    return steps


def _classify_xml_elements(root, elements, ns_tag, step_var_re,
                           assignment_types, condition_types, transition_types,
                           timer_types, st_code_types, call_block_types,
                           step_var_pattern_str=''):
    """一次遍历 XML，按类别分类元素

    Returns:
        {
            'actions': [{var, negated}],
            'guards': [{var, negated}],
            'transitions': [{target_thread, target_value, target_var}],
            'timers': [{name, pt}],
            'calls': [{name}],
            'st_code': [{text}],
        }
    """
    classified = {
        'actions': [], 'guards': [], 'transitions': [],
        'timers': [], 'calls': [], 'st_code': [],
    }

    for elem in root.iter():
        tag = _strip_ns(elem.tag)

        if tag in assignment_types:
            var_elem = elem.find(f'{ns_tag}variable')
            var = var_elem.text.strip() if (var_elem is not None and var_elem.text) else ''
            if var:
                negated = elem.get('negated', 'false') == 'true'
                classified['actions'].append({'var': var, 'negated': negated})
            continue

        if tag in condition_types:
            var_elem = elem.find(f'{ns_tag}variable')
            if var_elem is not None and var_elem.text:
                var = var_elem.text.strip()
                negated = elem.get('negated', 'false') == 'true'
                classified['guards'].append({'var': var, 'negated': negated})
            continue

        if tag != 'block':
            continue

        tn = elem.get('typeName', '')

        if tn in transition_types:
            tgt = extract_block_output_target(elem, ns_tag)
            inputs = extract_block_inputs(elem, elements, ns_tag)
            src_val = ''
            for inp in inputs:
                if is_literal_value(inp):
                    src_val = inp
                    break
            sv_m = re.search(r'(?:' + step_var_pattern_str + r')\[(\d+)\]', tgt) if tgt else None
            target_thread = int(sv_m.group(1)) if sv_m else -1
            target_value = _parse_literal(src_val) if src_val else None
            classified['transitions'].append({
                'target_thread': target_thread,
                'target_value': target_value,
                'target_var': tgt,
            })
        elif tn in timer_types:
            inputs = extract_block_inputs(elem, elements, ns_tag)
            pt = ''
            for inp in inputs:
                if inp.startswith('T#') or re.match(r'^[\d.]+(ms|s|S|m|h)$', inp):
                    pt = inp
                    break
            classified['timers'].append({
                'name': tn,
                'pt': _parse_timer_value(pt),
            })
        elif tn in st_code_types:
            stcode_text = _extract_block_stcode(elem, ns_tag)
            if stcode_text:
                classified['st_code'].append({'text': stcode_text})
        else:
            if call_block_types:
                if tn in call_block_types:
                    classified['calls'].append({'name': tn})
            else:
                if tn and tn not in ('EQ', 'NE', 'GT', 'LT', 'GE', 'LE'):
                    classified['calls'].append({'name': tn})

    return classified


def _collect_comments(root, ns_tag: str) -> list:
    """收集文件内所有注释文本"""
    comments = []
    for comment in root.iter(f'{ns_tag}comment'):
        content = comment.find(f'{ns_tag}content')
        if content is None:
            continue
        for child in content:
            txt = (child.text or '').strip()
            if txt:
                comments.append(txt)
    return comments


def _match_comment(comments, step_value: int, comment_pattern: str) -> str:
    """根据步序值匹配注释文本（取前 200 字符）"""
    comment_re = re.compile(comment_pattern)
    for txt in comments:
        for line in txt.split('\n'):
            line = line.strip()
            if not line:
                continue
            m = comment_re.match(line)
            if m and m.lastindex is not None and m.lastindex >= 1:
                try:
                    val = int(m.group(1))
                except (ValueError, IndexError):
                    continue
                if val == step_value:
                    return txt[:200]
    return ''


def _collect_actions(logic, classified, step_var_re):
    """收集 Action 输出赋值（排除 step_var 赋值，避免与 Transition 混淆）"""
    for a in classified['actions']:
        var = a['var']
        if not var:
            continue
        if step_var_re.search(var):
            continue
        if var not in logic['actions']:
            logic['actions'].append(var)


def _collect_guards(logic, classified, step_var_re, common_enable_vars):
    """收集 Guard 条件（排除 step_var；标注公共使能）"""
    for g in classified['guards']:
        var = g['var']
        if not var:
            continue
        if step_var_re.search(var):
            continue
        label = ('NOT ' + var) if g['negated'] else var
        if label not in logic['guards']:
            logic['guards'].append(label)
        if _is_common_enable(var, common_enable_vars):
            if label not in logic['common_enables']:
                logic['common_enables'].append(label)


def _collect_transitions(logic, classified):
    """收集 Transition 状态转移（MOVE 块写入 step_var）"""
    for t in classified['transitions']:
        if t['target_thread'] < 0 or t['target_value'] is None:
            continue
        if t['target_value'] <= 0:
            continue
        transition = {
            'target_thread': t['target_thread'],
            'target_value': t['target_value'],
        }
        if transition not in logic['transitions']:
            logic['transitions'].append(transition)


def _collect_timers(logic, classified):
    """收集 Timer 定时器（按 name+pt 去重）"""
    for tm in classified['timers']:
        timer_info = {'name': tm['name'], 'pt': tm['pt']}
        existing = [(e['name'], e['pt']) for e in logic['timers']]
        if (timer_info['name'], timer_info['pt']) not in existing:
            logic['timers'].append(timer_info)


def _collect_calls(logic, classified):
    """收集 Call 函数调用（按 name 去重）"""
    for c in classified['calls']:
        if c['name'] not in [e['name'] for e in logic['calls']]:
            logic['calls'].append({'name': c['name']})


def _collect_st_code(logic, classified, step_var_re, step_var_name):
    """收集内嵌 ST 代码片段，并从中提取状态变量赋值作为 Transition candidate

    检测模式: step_var[idx] := value（含 INT# 前缀）。
    Transition 带 source: 'st_code' 标记，供 opencode 区分来源。
    """
    st_assign_re = re.compile(
        re.escape(step_var_name) +
        r'\[(\d+)\]\s*:=\s*(?:INT#)?(\d+)')

    for s in classified['st_code']:
        text = s['text']
        snippet = text[:200]
        if snippet not in logic['st_code']:
            logic['st_code'].append(snippet)

        for match in st_assign_re.findall(text):
            target_thread = int(match[0])
            target_value = int(match[1])
            if target_value <= 0:
                continue
            transition = {
                'target_thread': target_thread,
                'target_value': target_value,
                'source': 'st_code',
            }
            if transition not in logic['transitions']:
                logic['transitions'].append(transition)


# ============================================================
# Canonical IR Interface — DecodedProgram input
# ============================================================

def discover_steps_from_decoded(decoded_programs: list, config: dict) -> list:
    """从 DecodedProgram (Canonical IR) 发现步序。

    Canonical IR 接口入口。内部使用稳定的 XML 算法，
    从 DecodedProgram.source_file 提取 XML 路径并调用现有算法。

    未来新 Primitive (如 SMC2) 可直接消费 Canonical IR 元素，
    不需要通过 XML 文件。

    Args:
        decoded_programs: list of DecodedProgram (from PLCopenDecoder)
        config: dict 配置

    Returns:
        step_candidates list (同 discover_steps_plcopen)
    """
    xml_files = []
    for dp in decoded_programs:
        if hasattr(dp, 'source_file') and dp.source_file:
            xml_files.append(dp.source_file)
    return discover_steps_plcopen(xml_files, config)


def extract_step_logic_from_decoded(decoded_programs: list, config: dict) -> list:
    """从 DecodedProgram (Canonical IR) 提取步序逻辑。

    Canonical IR 接口入口。内部使用稳定的 XML 算法。

    Args:
        decoded_programs: list of DecodedProgram (from PLCopenDecoder)
        config: dict 配置

    Returns:
        step_logic list (同 extract_step_logic_plcopen)
    """
    xml_files = []
    for dp in decoded_programs:
        if hasattr(dp, 'source_file') and dp.source_file:
            xml_files.append(dp.source_file)
    return extract_step_logic_plcopen(xml_files, config)
