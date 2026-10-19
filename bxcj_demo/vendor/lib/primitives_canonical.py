#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/primitives_canonical.py — Canonical IR-based Program Primitives

消费 DecodedProgram (Canonical IR)，不接触原始文件格式。
同一个 comparison_state_machine Primitive 可以处理:
  - PLCopen XML (通过 PLCopenDecoder → Canonical IR)
  - SMC2 CDATA JSON (通过 SMC2Decoder → Canonical IR)
  - 未来其他格式 (通过新 Decoder → Canonical IR)

算法与 lib/primitives.py 中的 XML-based 版本相同，但输入是 Canonical IR:
  - Condition 元素 (EQ/NE/GT/...) → step discovery
  - Assignment 元素 (MOVE/coil) → transition discovery
  - RawFragment 元素 (EXECUTE/ST) → ST code step discovery
  - VariableRef 元素 (contact) → guard
  - Assignment 元素 (coil) → action
  - Timer 元素 → timer
  - Call 元素 → call

config dict 关键字段 (来自 ProgramPatternSpec):
  必填:
    - state_variables: [{name, pattern, ...}]
    - eq_block_type: 比较块类型 (默认 "EQ")
    - strategies: 策略列表
  可选:
    - step_var_extract_pattern: 从状态变量表达式提取 (station, var, thread) 的正则
    - filename_station_pattern: 从文件名提取 station 的正则
    - comment_step_pattern: 注释步序正则
    - common_enable_vars: 公共使能变量列表
"""
from __future__ import annotations
import re
from collections import defaultdict
from typing import Any

from .canonical_ir import (
    DecodedProgram, ProgramUnit, VariableDeclaration,
    VariableRef, Literal, Condition, Assignment, Call, Timer,
    Comment, Branch, RawFragment, SourceLocation, ProgramConnection,
)


def _is_literal_value(val: Any) -> bool:
    """Check if a value is a literal (int, bool, or Literal element)."""
    if isinstance(val, Literal):
        return True
    if isinstance(val, (int, bool)):
        return True
    if isinstance(val, str):
        val = val.strip()
        if re.match(r'^-?\d+$', val):
            return True
        if re.match(r'^\w+#\w+$', val, re.IGNORECASE):
            return True
        if val.upper() in ('TRUE', 'FALSE'):
            return True
    return False


def _parse_literal(val: Any):
    """Parse a literal value to int."""
    if isinstance(val, Literal):
        return val.value if isinstance(val.value, int) else None
    if isinstance(val, (int, bool)):
        return int(val)
    if isinstance(val, str):
        m = re.match(r'^(?:INT#)?(\d+)$', val.strip())
        if m:
            return int(m.group(1))
    return None


def _parse_timer_value(val: str) -> str:
    """Parse timer value (T#3S → 3s)."""
    if not val:
        return ''
    val = val.strip()
    m = re.match(r'^T#([\d.]+)(ms|s|S|m|h)$', val)
    if m:
        return m.group(1) + m.group(2).lower()
    return val


def _is_common_enable(var: str, common_enable_vars) -> bool:
    """Check if variable is a common enable."""
    if not var or not common_enable_vars:
        return False
    for pattern in common_enable_vars:
        if var == pattern or var.startswith(pattern + '[') or var.startswith(pattern + '.'):
            return True
    return False


def discover_steps_from_canonical(decoded_programs: list, config: dict) -> list:
    """从 DecodedProgram (Canonical IR) 发现步序候选。

    实现 comparison_state_machine 的 step discovery 策略:
      - graph_comparison: Condition 元素 (EQ) 匹配 state variable + literal
      - st_code: RawFragment 元素中的 ST 代码赋值
      - comment_only: Comment 元素中以数字开头的步序编号

    Args:
        decoded_programs: list of DecodedProgram
        config: dict 配置

    Returns:
        step_candidates list
    """
    state_vars_config = config.get('state_variables', [])
    if state_vars_config:
        step_var_names = [sv['name'] for sv in state_vars_config]
    else:
        single = config.get('step_var_name', '')
        step_var_names = [single] if single else []

    if not step_var_names:
        return []

    eq_operator = config.get('eq_block_type', 'EQ')
    strategies = config.get('strategies', ['graph_comparison'])
    comment_pattern = config.get('comment_step_pattern', r'^(\d+)')
    file_station_map = config.get('file_station_map')
    filename_station_pattern = config.get('filename_station_pattern')

    # Step variable extraction pattern: extracts (station, var, thread) from expression
    step_var_extract_re = re.compile(
        config.get('step_var_extract_pattern',
                   r'([A-Z]{2}\d{2})V?\.(\w+)\[(\d+)\]'))

    # ST code step pattern: state_var[thread] := value
    # Built from state variable names
    sv_pat = '|'.join(re.escape(v) for v in step_var_names)

    step_var_re = re.compile(sv_pat)
    all_candidates = []

    for dp in decoded_programs:
        source_file = getattr(dp, 'source_file', '') or ''
        file_name = source_file.split('/')[-1].split('\\')[-1] if source_file else ''
        file_station = _detect_station(file_name, file_station_map,
                                        filename_station_pattern)
        file_thread_steps = defaultdict(set)

        for unit in dp.program_units:
            # graph_comparison: find Condition elements with EQ operator
            if 'graph_comparison' in strategies:
                for elem in unit.elements:
                    if not isinstance(elem, Condition):
                        continue
                    if elem.operator != eq_operator:
                        continue
                    # Check if any operand is a state variable
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
                        elif isinstance(operand, str):
                            if _is_literal_value(operand):
                                value_input = operand

                    if step_var_input and value_input:
                        val = _parse_literal(value_input)
                        if isinstance(val, int):
                            sv_m = step_var_extract_re.search(step_var_input)
                            if sv_m:
                                # Handle both 2-group and 3-group patterns:
                                # 3-group: (station, var, thread)
                                # 2-group: (var, thread) — station from file
                                if sv_m.lastindex and sv_m.lastindex >= 3:
                                    ext_station = sv_m.group(1)
                                    ext_var = sv_m.group(2)
                                    ext_thread = int(sv_m.group(3))
                                elif sv_m.lastindex and sv_m.lastindex >= 2:
                                    ext_station = file_station
                                    ext_var = sv_m.group(1)
                                    ext_thread = int(sv_m.group(2))
                                else:
                                    ext_station = file_station
                                    ext_var = sv_m.group(1) if sv_m.lastindex else ''
                                    ext_thread = -1
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

            # st_code: find RawFragment elements with ST code
            if 'st_code' in strategies:
                for elem in unit.elements:
                    if not isinstance(elem, RawFragment):
                        continue
                    if elem.language != 'st':
                        continue
                    text = elem.text
                    if not text:
                        continue
                    st_pat_str = config.get('st_code_step_pattern')
                    if st_pat_str:
                        st_pat = re.compile(st_pat_str.format(sv_pat=sv_pat))
                    else:
                        st_pat = re.compile(
                            rf'([A-Z]{{2}}\d{{2}})V?\.({sv_pat})\[(\d+)\]\s*(?::=|=)\s*(\d+)')
                    for m in st_pat.finditer(text):
                        val = int(m.group(4) if m.lastindex >= 4 else m.group(3))
                        if val > 0:
                            thread = int(m.group(3))
                            station = m.group(1) if m.lastindex >= 4 else ''
                            all_candidates.append({
                                'station': station,
                                'thread_idx': thread,
                                'value': val,
                                'var_name': step_var_names[0],
                                'state_variable': step_var_names[0],
                                'source_type': 'st_code',
                                'source_file': file_name,
                                'evidence': {
                                    'st_snippet': text[max(0, m.start() - 20):m.end() + 50]},
                            })

            # comment_only: find Comment elements with step numbers
            if 'comment_only' in strategies and file_thread_steps:
                comment_re = re.compile(comment_pattern)
                for elem in unit.elements:
                    if not isinstance(elem, Comment):
                        continue
                    text = elem.text
                    if not text:
                        continue
                    for line in text.split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        m = comment_re.match(line)
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
                                        'evidence': {'comment_text': text[:200]},
                                    })

    return all_candidates


def extract_step_logic_from_canonical(decoded_programs: list, config: dict) -> list:
    """从 DecodedProgram (Canonical IR) 提取步序逻辑。

    对每个已发现的 step，在同一 ProgramUnit 内收集:
      - actions: Assignment 元素 (coil, 非 state variable)
      - guards: VariableRef 元素 (contact, 非 state variable)
      - transitions: Assignment 元素 (MOVE writing to state variable)
      - timers: Timer 元素
      - calls: Call 元素
      - st_code: RawFragment 元素

    Args:
        decoded_programs: list of DecodedProgram
        config: dict 配置

    Returns:
        step_logic list
    """
    state_vars_config = config.get('state_variables', [])
    if state_vars_config:
        step_var_names = [sv['name'] for sv in state_vars_config]
    else:
        single = config.get('step_var_name', '')
        step_var_names = [single] if single else []

    eq_operator = config.get('eq_block_type', 'EQ')
    strategies = config.get('strategies', ['graph_comparison'])
    comment_pattern = config.get('comment_step_pattern', r'^(\d+)')
    file_station_map = config.get('file_station_map')
    filename_station_pattern = config.get('filename_station_pattern')
    common_enable_vars = config.get('common_enable_vars', [])

    step_var_extract_re = re.compile(
        config.get('step_var_extract_pattern',
                   r'([A-Z]{2}\d{2})V?\.(\w+)\[(\d+)\]'))

    sv_pat = '|'.join(re.escape(v) for v in step_var_names)
    step_var_re = re.compile(sv_pat)
    step_var_index_re = re.compile(r'(?:' + sv_pat + r')\[(\d+)\]')

    step_logic_map = {}

    for dp in decoded_programs:
        source_file = getattr(dp, 'source_file', '') or ''
        file_name = source_file.split('/')[-1].split('\\')[-1] if source_file else ''
        file_station = _detect_station(file_name, file_station_map,
                                        filename_station_pattern)

        for unit in dp.program_units:
            # Classify elements in this unit
            classified = _classify_canonical_elements(
                unit.elements, step_var_re, sv_pat, config)

            # Discover steps in this unit
            file_steps = _discover_unit_steps(
                unit.elements, step_var_names, eq_operator,
                strategies, comment_pattern, file_station,
                step_var_extract_re)

            # Collect comments
            unit_comments = [elem.text for elem in unit.elements
                             if isinstance(elem, Comment) and elem.text]

            for step in file_steps:
                step_station = step.get('station', file_station)
                key = (step.get('state_variable', ''), step_station,
                       step['thread_idx'], step['value'])
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

                _collect_actions_canonical(logic, classified, step_var_re)
                _collect_guards_canonical(logic, classified, step_var_re,
                                          common_enable_vars)
                _collect_transitions_canonical(logic, classified)
                _collect_timers_canonical(logic, classified)
                _collect_calls_canonical(logic, classified)
                _collect_st_code_canonical(logic, classified, step_var_re,
                                           step_var_names[0] if step_var_names else '')

                if not logic['comment']:
                    matched = _match_comment_canonical(
                        unit_comments, step['value'], comment_pattern)
                    if matched:
                        logic['comment'] = matched

    return list(step_logic_map.values())


def _detect_station(file_name: str, file_station_map: dict = None,
                    station_pattern: str = None) -> str:
    """Detect station from file name."""
    if file_station_map and file_name in file_station_map:
        return file_station_map[file_name]
    if station_pattern:
        station_re = re.compile(station_pattern)
    else:
        station_re = re.compile(r'([A-Z]{2}\d{2})')
    m = station_re.search(file_name)
    if m:
        return m.group(1)
    return ''


def _classify_canonical_elements(elements: list, step_var_re, sv_pat: str,
                                  config: dict) -> dict:
    """Classify Canonical Elements by type.

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

    transition_var_re = re.compile(r'(?:' + sv_pat + r')\[(\d+)\]')
    st_assign_re = None

    for elem in elements:
        if isinstance(elem, Assignment):
            # Check if it's a transition (target is state variable)
            if elem.target:
                target_name = elem.target.name or elem.target.base_name
                if target_name and step_var_re.search(target_name):
                    # Transition: state_var[thread] := value
                    sv_m = transition_var_re.search(target_name)
                    target_thread = int(sv_m.group(1)) if sv_m else -1
                    target_value = None
                    if elem.source:
                        if isinstance(elem.source, Literal):
                            target_value = elem.source.value
                        elif isinstance(elem.source, VariableRef):
                            target_value = None  # Variable source, not a literal
                        elif isinstance(elem.source, str):
                            target_value = _parse_literal(elem.source)
                    if target_value and isinstance(target_value, int):
                        classified['transitions'].append({
                            'target_thread': target_thread,
                            'target_value': target_value,
                            'target_var': target_name,
                        })
                    continue
                # Regular action (coil)
                var = target_name
                if var and not step_var_re.search(var):
                    classified['actions'].append({'var': var, 'negated': False})
            continue

        if isinstance(elem, VariableRef):
            # Contact → guard
            var = elem.name or elem.base_name
            if var and not step_var_re.search(var):
                classified['guards'].append({'var': var, 'negated': elem.negated})
            continue

        if isinstance(elem, Timer):
            classified['timers'].append({
                'name': elem.timer_type,
                'pt': _parse_timer_value(elem.preset),
            })
            continue

        if isinstance(elem, Call):
            if elem.callee:
                classified['calls'].append({'name': elem.callee})
            continue

        if isinstance(elem, RawFragment):
            if elem.text:
                classified['st_code'].append({'text': elem.text})
            continue

    return classified


def _discover_unit_steps(elements: list, step_var_names: list,
                          eq_operator: str, strategies: list,
                          comment_pattern: str, file_station: str,
                          step_var_extract_re) -> list:
    """Discover step candidates in a single ProgramUnit."""
    steps = []
    file_thread_steps = defaultdict(set)
    step_var_re = re.compile('|'.join(re.escape(v) for v in step_var_names))

    if 'graph_comparison' in strategies:
        for elem in elements:
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
                val = _parse_literal(value_input)
                if isinstance(val, int):
                    sv_m = step_var_extract_re.search(step_var_input)
                    if sv_m:
                        if sv_m.lastindex and sv_m.lastindex >= 3:
                            ext_station = sv_m.group(1)
                            ext_var = sv_m.group(2)
                            ext_thread = int(sv_m.group(3))
                        elif sv_m.lastindex and sv_m.lastindex >= 2:
                            ext_station = file_station
                            ext_var = sv_m.group(1)
                            ext_thread = int(sv_m.group(2))
                        else:
                            ext_station = file_station
                            ext_var = sv_m.group(1) if sv_m.lastindex else ''
                            ext_thread = -1
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
                            'state_variable': step_var_names[0] if step_var_names else '',
                            'source_type': 'graph_comparison',
                            'evidence': {'expr': f'{step_var_input}={val}'},
                        })

    if 'comment_only' in strategies and file_thread_steps:
        comment_re = re.compile(comment_pattern)
        for elem in elements:
            if not isinstance(elem, Comment):
                continue
            text = elem.text
            if not text:
                continue
            for line in text.split('\n'):
                line = line.strip()
                if not line:
                    continue
                m = comment_re.match(line)
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
                                'state_variable': step_var_names[0] if step_var_names else '',
                                'source_type': 'comment_only',
                                'evidence': {'comment_text': text[:200]},
                            })

    return steps


def _collect_actions_canonical(logic, classified, step_var_re):
    for a in classified['actions']:
        var = a['var']
        if not var or step_var_re.search(var):
            continue
        if var not in logic['actions']:
            logic['actions'].append(var)


def _collect_guards_canonical(logic, classified, step_var_re, common_enable_vars):
    for g in classified['guards']:
        var = g['var']
        if not var or step_var_re.search(var):
            continue
        label = ('NOT ' + var) if g['negated'] else var
        if label not in logic['guards']:
            logic['guards'].append(label)
        if _is_common_enable(var, common_enable_vars):
            if label not in logic['common_enables']:
                logic['common_enables'].append(label)


def _collect_transitions_canonical(logic, classified):
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


def _collect_timers_canonical(logic, classified):
    for tm in classified['timers']:
        timer_info = {'name': tm['name'], 'pt': tm['pt']}
        existing = [(e['name'], e['pt']) for e in logic['timers']]
        if (timer_info['name'], timer_info['pt']) not in existing:
            logic['timers'].append(timer_info)


def _collect_calls_canonical(logic, classified):
    for c in classified['calls']:
        if c['name'] not in [e['name'] for e in logic['calls']]:
            logic['calls'].append({'name': c['name']})


def _collect_st_code_canonical(logic, classified, step_var_re, step_var_name):
    st_assign_re = re.compile(
        re.escape(step_var_name) + r'\[(\d+)\]\s*:=\s*(?:INT#)?(\d+)')

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


def _match_comment_canonical(comments: list, step_value: int,
                              comment_pattern: str) -> str:
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
