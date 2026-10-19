#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/decoders/plcopen_decoder.py — PLCopen XML → Canonical Program IR

职责:
  解析 PLCopen XML 文件，输出 DecodedProgram:
    - ProgramUnits (含 Canonical Program Elements)
    - VariableDeclarations (结构化变量声明)

不做:
  - 判断什么是 Step / actor / station / operation
  - 创建 Entity / Relation (那是 Evidence Extractor 的事)
  - 应用项目命名规则

PLCopen 元素映射:
  <pou>              → ProgramUnit (unit_type=pou)
  <action>           → ProgramUnit (unit_type=action)
  <variable>         → VariableDeclaration
  <contact>          → VariableRef (negated flag)
  <coil>             → Assignment (target=VariableRef)
  <block typeName="EQ">   → Condition (operator=EQ)
  <block typeName="MOVE"> → Assignment (target=VariableRef, source=Literal)
  <block typeName="EXECUTE"> → RawFragment (language=st)
  <block typeName="TON/TOF/TP"> → Timer
  <block typeName="other"> → Call
  <comment>          → Comment
  <inVariable>       → VariableRef (as input)
  <outVariable>      → VariableRef (as output)
  <inOutVariable>    → VariableRef (as inout)
"""
from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ..canonical_ir import (
    DecodedProgram, ProgramUnit, VariableDeclaration,
    VariableRef, Literal, Condition, Assignment, Call, Timer,
    Comment, Branch, RawFragment, SourceLocation, ProgramConnection,
)


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def _get_ns_tag(root) -> str:
    ns = ''
    m = re.match(r'\{(.+?)\}', root.tag)
    if m:
        ns = m.group(1)
    return f'{{{ns}}}' if ns else ''


def _is_literal_value(expr: str) -> bool:
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


def _parse_literal_value(expr: str):
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


def _parse_variable_ref(expr_text: str, negated: bool = False,
                        source_loc: SourceLocation = None) -> VariableRef:
    """Parse a variable expression text into a VariableRef."""
    expr_text = (expr_text or '').strip()
    if not expr_text:
        return VariableRef(name='', source_location=source_loc)

    # Try root[index].path
    m = re.match(r'^(\w+)\[([^\]]*)\]\.?(.*)$', expr_text)
    if m:
        base = m.group(1)
        index = _parse_index(m.group(2))
        rest = m.group(3)
    else:
        m2 = re.match(r'^(\w+)\.(.*)$', expr_text)
        if m2:
            base = m2.group(1)
            rest = m2.group(2)
            index = None
        else:
            m3 = re.match(r'^(\w+)\[([^\]]*)\]$', expr_text)
            if m3:
                base = m3.group(1)
                index = _parse_index(m3.group(2))
                rest = ''
            else:
                m4 = re.match(r'^(\w+)$', expr_text)
                if m4:
                    return VariableRef(
                        name=expr_text, base_name=m4.group(1),
                        negated=negated, source_location=source_loc)
                return VariableRef(
                    name=expr_text, base_name=expr_text,
                    negated=negated, source_location=source_loc)

    path = []
    if rest:
        parts = rest.split('.')
        for i, part in enumerate(parts):
            m_idx = re.match(r'^(\w+)\[([^\]]*)\]$', part)
            if m_idx:
                path.append(m_idx.group(1))
                if i == len(parts) - 1:
                    index = _parse_index(m_idx.group(2))
            else:
                path.append(part)

    return VariableRef(
        name=expr_text, base_name=base, path=path,
        index=index, negated=negated, source_location=source_loc)


def _parse_index(index_str: str):
    index_str = index_str.strip()
    if not index_str:
        return None
    if ',' in index_str:
        parts = [p.strip() for p in index_str.split(',')]
        try:
            return tuple(int(p) for p in parts)
        except ValueError:
            return None
    try:
        return int(index_str)
    except ValueError:
        return None


class PLCopenDecoder:
    """PLCopen XML → DecodedProgram

    使用 iterparse 处理大文件。输出:
      - program_units: 每个 POU/action 的 Canonical Elements
      - variable_declarations: 变量声明记录
    """

    AT_PATTERN = re.compile(r'<AT>\s*([^<\s]+)\s*</AT>', re.IGNORECASE)

    def decode(self, xml_path: str) -> DecodedProgram:
        """Decode a single PLCopen XML file into DecodedProgram."""
        path = Path(xml_path)
        program = DecodedProgram(
            format='plcopen_xml',
            source_file=str(path),
        )
        if not path.exists():
            return program

        try:
            tree = ET.parse(str(path))
            root = tree.getroot()
        except Exception:
            return program

        ns_tag = _get_ns_tag(root)
        elements_map = self._build_elements_map(root)
        file_name = path.name

        # 1. Decode variable declarations
        program.variable_declarations = self._decode_variable_declarations(
            root, ns_tag, file_name)

        # 2. Decode program units (POUs and their actions)
        program.program_units = self._decode_program_units(
            root, ns_tag, elements_map, file_name)

        program.raw_stats = {
            'total_variables': len(program.variable_declarations),
            'total_program_units': len(program.program_units),
            'total_elements': sum(len(u.elements) for u in program.program_units),
            'total_connections': sum(len(u.connections) for u in program.program_units),
        }
        return program

    def decode_files(self, xml_files: list) -> list:
        """Decode multiple PLCopen XML files. Returns list of DecodedProgram."""
        return [self.decode(f) for f in xml_files]

    def _build_elements_map(self, root) -> dict:
        elements = {}
        for elem in root.iter():
            lid = elem.get('localId')
            if lid:
                elements[lid] = elem
        return elements

    def _decode_variable_declarations(self, root, ns_tag: str,
                                       file_name: str) -> list:
        """Decode all <variable> elements into VariableDeclaration records.

        Uses recursive walk to properly track pou/dataType nesting.
        """
        declarations = []
        container_stack = []
        self._walk_for_variables(root, container_stack, ns_tag,
                                  file_name, declarations)
        return declarations

    def _walk_for_variables(self, elem, container_stack: list,
                             ns_tag: str, file_name: str,
                             declarations: list):
        """Recursively walk XML tree, tracking pou/dataType nesting."""
        tag = _strip_ns(elem.tag)
        pushed = False

        if tag == 'pou':
            name = elem.get('name', '')
            if name:
                container_stack.append(('pou', name))
                pushed = True
        elif tag == 'dataType':
            name = elem.get('name', '')
            if name:
                container_stack.append(('datatype', name))
                pushed = True

        if tag == 'variable':
            var_name = elem.get('name', '')
            if var_name:
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
                at_m = self.AT_PATTERN.search(
                    ET.tostring(elem, encoding='unicode'))
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

                declarations.append(VariableDeclaration(
                    name=var_name, scope=scope, derived_type=derived_type,
                    documentation=doc_text, at_address=at_addr,
                    source_file=file_name,
                ))

        for child in elem:
            self._walk_for_variables(child, container_stack, ns_tag,
                                      file_name, declarations)

        if pushed:
            container_stack.pop()

    def _decode_program_units(self, root, ns_tag: str,
                               elements_map: dict, file_name: str) -> list:
        """Decode POUs and actions into ProgramUnits with Canonical Elements.

        In some PLCopen XML exports, <action> elements are siblings of <pou>
        elements (not children). This method finds ALL <action> elements
        and determines their parent POU by position.
        """
        units = []

        # Build parent map for finding action → POU relationships
        parent_map = {child: parent for parent in root.iter() for child in parent}

        # 1. Decode POU elements (direct body only, not action subtrees)
        pou_names = set()
        for pou_elem in root.iter(f'{ns_tag}pou'):
            pou_name = pou_elem.get('name', '')
            if not pou_name:
                continue
            pou_names.add(pou_name)

            pou_unit = ProgramUnit(
                name=pou_name,
                unit_type='pou',
                source_file=file_name,
                source_location=SourceLocation(
                    file=file_name, element_tag='pou'),
            )
            elements, connections = self._decode_elements_in_pou(
                pou_elem, ns_tag, elements_map, file_name)
            pou_unit.elements = elements
            pou_unit.connections = connections
            units.append(pou_unit)

        # 2. Decode ALL <action> elements (both within and outside POUs)
        for action_elem in root.iter(f'{ns_tag}action'):
            action_name = action_elem.get('name', '')
            if not action_name:
                continue

            # Determine parent POU
            pou_name = self._find_parent_pou(action_elem, parent_map, ns_tag)

            if pou_name:
                unit_name = f'{pou_name}.{action_name}'
            else:
                unit_name = action_name

            action_unit = ProgramUnit(
                name=unit_name,
                unit_type='action',
                source_file=file_name,
                source_location=SourceLocation(
                    file=file_name, element_tag='action'),
            )
            elements, connections = self._decode_elements_in_scope(
                action_elem, ns_tag, elements_map, file_name)
            action_unit.elements = elements
            action_unit.connections = connections
            units.append(action_unit)

        return units

    def _find_parent_pou(self, action_elem, parent_map: dict,
                          ns_tag: str) -> str:
        """Find the parent POU of an action element.

        Handles three PLCopen XML layouts:
        1. <pou><action>...</action></pou> — action inside pou
        2. <pou><actions><action>...</action></actions></pou> — action in actions container
        3. <pous><pou>...</pou><action>...</action></pous> — action as sibling of pou
        """
        # Case 1 & 2: Walk up parent chain to find <pou>
        parent = parent_map.get(action_elem)
        while parent is not None:
            if _strip_ns(parent.tag) == 'pou':
                return parent.get('name', '')
            parent = parent_map.get(parent)

        # Case 3: Action is sibling of <pou> elements
        # Find nearest preceding <pou> sibling
        direct_parent = parent_map.get(action_elem)
        if direct_parent is not None:
            prev_pou_name = ''
            for child in direct_parent:
                if child is action_elem:
                    break
                if _strip_ns(child.tag) == 'pou':
                    name = child.get('name', '')
                    if name:
                        prev_pou_name = name
            if prev_pou_name:
                return prev_pou_name

        # Fallback: try extracting POU name from action name
        action_name = action_elem.get('name', '')
        if action_name and '.' in action_name:
            return action_name.rsplit('.', 1)[0]

        return ''

    def _decode_elements_in_pou(self, pou_elem, ns_tag: str,
                                 elements_map: dict, file_name: str):
        """Decode program elements within a POU's direct body (not actions).

        Only processes elements in <body> children of the POU, skipping
        <action> subtrees which are handled separately.
        Returns (elements, connections).
        """
        elements = []
        connection_elems = []
        for child in pou_elem:
            if _strip_ns(child.tag) == 'body':
                for elem in child.iter():
                    if elem is child:
                        continue
                    tag = _strip_ns(elem.tag)
                    canon = self._decode_single_element(
                        elem, tag, ns_tag, elements_map, file_name)
                    if canon is None:
                        continue
                    if isinstance(canon, list):
                        elements.extend(canon)
                    else:
                        elements.append(canon)
                    # Collect elements that may have connections
                    if elem.get('localId'):
                        connection_elems.append(elem)
        connections = self._build_connections(connection_elems, ns_tag, file_name)
        return elements, connections

    def _decode_elements_in_scope(self, scope_elem, ns_tag: str,
                                   elements_map: dict, file_name: str):
        """Decode program elements within a scope's body (e.g. action body).

        Returns (elements, connections).
        """
        elements = []
        connection_elems = []
        for child in scope_elem:
            if _strip_ns(child.tag) == 'body':
                for elem in child.iter():
                    if elem is child:
                        continue
                    tag = _strip_ns(elem.tag)
                    canon = self._decode_single_element(
                        elem, tag, ns_tag, elements_map, file_name)
                    if canon is None:
                        continue
                    if isinstance(canon, list):
                        elements.extend(canon)
                    else:
                        elements.append(canon)
                    if elem.get('localId'):
                        connection_elems.append(elem)
        connections = self._build_connections(connection_elems, ns_tag, file_name)
        return elements, connections

    def _build_connections(self, elems_with_local_id: list,
                            ns_tag: str, file_name: str) -> list:
        """Build ProgramConnection list from elements' connection@refLocalId.

        For each element with localId, find all <connection refLocalId="..."/>
        descendants. Each represents a wire from refLocalId element → this element.

        Connection types:
          - block inputVariables/variable/connection → param_binding
            (formalParameter available)
          - contact/coil connectionPointIn/connection → wire
          - inVariable/outVariable connectionPointOut → wire (source side)

        Returns list of ProgramConnection.
        """
        connections = []
        for elem in elems_with_local_id:
            target_id = elem.get('localId', '')
            if not target_id:
                continue
            tag = _strip_ns(elem.tag)
            # Determine connection type and formal parameter
            # Walk up to find formalParameter context (for block params)
            formal_param = ''
            parent = None
            # Try to find formalParameter on the element or its parent
            fp = elem.get('formalParameter', '')
            if fp:
                formal_param = fp

            # Find all connection descendants with refLocalId
            for conn in elem.iter(f'{ns_tag}connection'):
                ref = conn.get('refLocalId', '')
                if not ref:
                    continue
                # Determine connection type
                # If elem is inside inputVariables/outputVariables → param_binding
                # Otherwise → wire
                conn_type = 'wire'
                # Check if we're in a block param context by looking at
                # whether the element has formalParameter or is a variable
                # inside inputVariables/outputVariables
                if tag == 'variable' or formal_param:
                    conn_type = 'param_binding'

                pc = ProgramConnection(
                    source_element_id=ref,
                    target_element_id=target_id,
                    connection_type=conn_type,
                    source_param='',
                    target_param=formal_param,
                    source_location=SourceLocation(
                        file=file_name,
                        local_id=target_id,
                        element_tag=tag,
                    ),
                )
                connections.append(pc)

        return connections

    def _decode_single_element(self, elem, tag: str, ns_tag: str,
                                elements_map: dict, file_name: str):
        """Decode a single XML element into Canonical Program Element(s).

        Returns None, a single element, or a list of elements.
        Sets element_id from @localId on each returned element for topology.
        """
        local_id = elem.get('localId', '')
        loc = SourceLocation(
            file=file_name,
            local_id=local_id,
            element_tag=tag,
        )

        if tag == 'contact':
            return self._decode_contact(elem, ns_tag, loc, local_id)

        if tag == 'coil':
            return self._decode_coil(elem, ns_tag, loc, local_id)

        if tag == 'comment':
            return self._decode_comment(elem, ns_tag, loc, local_id)

        if tag == 'block':
            return self._decode_block(elem, ns_tag, elements_map, loc, local_id)

        if tag in ('inVariable', 'inOutVariable', 'outVariable'):
            return None

        if tag == 'leftPowerRail':
            return None

        return None

    def _decode_contact(self, elem, ns_tag: str,
                         loc: SourceLocation, element_id: str = '') -> VariableRef:
        """Decode <contact> → VariableRef (with negated flag)."""
        var_elem = elem.find(f'{ns_tag}variable')
        var_text = ''
        if var_elem is not None and var_elem.text:
            var_text = var_elem.text.strip()
        negated = elem.get('negated', 'false') == 'true'
        ref = _parse_variable_ref(var_text, negated=negated, source_loc=loc)
        ref.element_id = element_id
        return ref

    def _decode_coil(self, elem, ns_tag: str,
                      loc: SourceLocation, element_id: str = '') -> Assignment:
        """Decode <coil> → Assignment (target=VariableRef).

        Captures storage attribute: 'set' (S), 'reset' (R), or 'none' (normal).
        """
        var_elem = elem.find(f'{ns_tag}variable')
        var_text = ''
        if var_elem is not None and var_elem.text:
            var_text = var_elem.text.strip()
        target = _parse_variable_ref(var_text, source_loc=loc)
        target.element_id = element_id
        storage = elem.get('storage', 'none')
        if storage not in ('none', 'set', 'reset'):
            storage = 'none'
        return Assignment(
            target=target,
            source=None,  # Coil source is resolved via connections
            storage=storage,
            source_location=loc,
            element_id=element_id,
        )

    def _decode_comment(self, elem, ns_tag: str,
                         loc: SourceLocation, element_id: str = '') -> Comment:
        """Decode <comment> → Comment."""
        content = elem.find(f'{ns_tag}content')
        text = ''
        if content is not None:
            for child in content:
                if child.text:
                    text += child.text + '\n'
                if child.tail:
                    text += child.tail
        text = text.strip()
        return Comment(text=text, source_location=loc, element_id=element_id)

    def _decode_block(self, elem, ns_tag: str, elements_map: dict,
                       loc: SourceLocation, element_id: str = ''):
        """Decode <block> → Condition / Assignment / Call / Timer / RawFragment.

        If the block has instanceName, a Call element is ALSO produced
        (in addition to the semantic element) to capture FB call graph info.
        The semantic element gets element_id=localId; the secondary Call
        element gets element_id='' (does not participate in topology).
        """
        type_name = elem.get('typeName', '')
        instance_name = elem.get('instanceName', '')

        # Determine semantic element based on typeName
        if type_name in ('EQ', 'NE', 'GT', 'LT', 'GE', 'LE'):
            semantic = self._decode_comparison_block(
                elem, ns_tag, elements_map, type_name, loc, element_id)
        elif type_name == 'MOVE':
            semantic = self._decode_move_block(
                elem, ns_tag, elements_map, loc, element_id)
        elif type_name in ('TON', 'TOF', 'TP'):
            semantic = self._decode_timer_block(
                elem, ns_tag, elements_map, type_name, loc, element_id)
        elif type_name == 'EXECUTE':
            semantic = self._decode_execute_block(elem, ns_tag, loc, element_id)
        else:
            semantic = self._decode_call_block(
                elem, ns_tag, elements_map, type_name, instance_name, loc, element_id)

        # If block has instanceName and semantic is not already a Call,
        # also produce a Call element for FB call graph (no element_id —
        # secondary artifact, does not participate in topology)
        if instance_name and not isinstance(semantic, Call):
            fb_call = self._decode_call_block(
                elem, ns_tag, elements_map, type_name, instance_name, loc,
                element_id='')
            return [semantic, fb_call]

        return semantic

    def _decode_comparison_block(self, elem, ns_tag: str,
                                  elements_map: dict, operator: str,
                                  loc: SourceLocation,
                                  element_id: str = '') -> Condition:
        """Decode EQ/NE/GT/LT/GE/LE block → Condition."""
        inputs = self._extract_block_inputs(elem, ns_tag, elements_map)
        operands = []
        for inp_text in inputs:
            if _is_literal_value(inp_text):
                operands.append(Literal(
                    value=_parse_literal_value(inp_text),
                    raw_text=inp_text,
                    source_location=loc,
                ))
            else:
                operands.append(_parse_variable_ref(inp_text, source_loc=loc))
        return Condition(
            operator=operator,
            operands=operands,
            source_location=loc,
            element_id=element_id,
        )

    def _decode_move_block(self, elem, ns_tag: str,
                            elements_map: dict,
                            loc: SourceLocation,
                            element_id: str = '') -> Assignment:
        """Decode MOVE block → Assignment."""
        inputs = self._extract_block_inputs(elem, ns_tag, elements_map)
        target_text = self._extract_block_output_target(elem, ns_tag)

        source = None
        for inp_text in inputs:
            if _is_literal_value(inp_text):
                source = Literal(
                    value=_parse_literal_value(inp_text),
                    raw_text=inp_text,
                    source_location=loc,
                )
                break
            elif inp_text:
                source = _parse_variable_ref(inp_text, source_loc=loc)
                break

        target = _parse_variable_ref(target_text, source_loc=loc) if target_text else None
        return Assignment(
            target=target,
            source=source,
            source_location=loc,
            element_id=element_id,
        )

    def _decode_timer_block(self, elem, ns_tag: str,
                             elements_map: dict, timer_type: str,
                             loc: SourceLocation,
                             element_id: str = '') -> Timer:
        """Decode TON/TOF/TP block → Timer."""
        inputs = self._extract_block_inputs(elem, ns_tag, elements_map)
        pt = ''
        for inp_text in inputs:
            if inp_text.startswith('T#') or re.match(r'^[\d.]+(ms|s|S|m|h)$', inp_text):
                pt = inp_text
                break
        return Timer(
            timer_type=timer_type,
            preset=pt,
            source_location=loc,
            element_id=element_id,
        )

    def _decode_execute_block(self, elem, ns_tag: str,
                                loc: SourceLocation,
                                element_id: str = '') -> RawFragment:
        """Decode EXECUTE block → RawFragment (ST code)."""
        st_text = ''
        for data in elem.iter(f'{ns_tag}data'):
            if 'stcode' in data.get('name', '').lower():
                for child in data:
                    if _strip_ns(child.tag) == 'STCode':
                        st_text = (child.text or '').strip()
                        break
        return RawFragment(
            language='st',
            text=st_text,
            source_location=loc,
            element_id=element_id,
        )

    def _decode_call_block(self, elem, ns_tag: str,
                            elements_map: dict, type_name: str,
                            instance_name: str,
                            loc: SourceLocation,
                            element_id: str = '') -> Call:
        """Decode generic block → Call."""
        inputs = []
        outputs = []

        iv = elem.find(f'{ns_tag}inputVariables')
        if iv is not None:
            for var in iv:
                fp = var.get('formalParameter', '')
                expr = self._extract_var_expression(var, ns_tag, elements_map)
                if expr:
                    if _is_literal_value(expr):
                        inputs.append({'name': fp, 'value': Literal(
                            value=_parse_literal_value(expr),
                            raw_text=expr, source_location=loc)})
                    else:
                        inputs.append({'name': fp, 'value': _parse_variable_ref(expr, source_loc=loc)})

        ov = elem.find(f'{ns_tag}outputVariables')
        if ov is not None:
            for var in ov:
                fp = var.get('formalParameter', '')
                if fp == 'ENO':
                    continue
                expr = self._extract_var_expression(var, ns_tag, elements_map)
                if expr:
                    outputs.append({'name': fp, 'value': _parse_variable_ref(expr, source_loc=loc)})

        return Call(
            callee=type_name,
            instance_name=instance_name,
            inputs=inputs,
            outputs=outputs,
            source_location=loc,
            element_id=element_id,
        )

    def _extract_block_inputs(self, block, ns_tag: str,
                               elements_map: dict) -> list:
        """Extract input expressions from a block's inputVariables.

        Skips EN (enable) inputs — EN is the execution condition, not
        a data input. This is critical for MOVE blocks where EN is
        connected to a contact/block and the actual source value is
        on a separate input.
        """
        inputs = []
        iv = block.find(f'{ns_tag}inputVariables')
        if iv is None:
            return inputs
        for var in iv:
            fp = var.get('formalParameter', '')
            if fp == 'EN':
                continue  # Skip enable input
            for cp in var.iter(f'{ns_tag}connection'):
                ref = cp.get('refLocalId')
                if ref and ref in elements_map:
                    expr = self._extract_element_expression(
                        elements_map[ref], ns_tag, elements_map)
                    if expr:
                        inputs.append(expr)
                        break
        return inputs

    def _extract_block_output_target(self, block, ns_tag: str) -> str:
        """Extract output target expression (for MOVE blocks)."""
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

    def _extract_var_expression(self, var_elem, ns_tag: str,
                                 elements_map: dict) -> str:
        """Extract expression from a variable element in block params."""
        ref_m = None
        for cp in var_elem.iter(f'{ns_tag}connection'):
            ref = cp.get('refLocalId')
            if ref and ref in elements_map:
                return self._extract_element_expression(
                    elements_map[ref], ns_tag, elements_map)
        for e in var_elem.iter(f'{ns_tag}expression'):
            txt = (e.text or '').strip()
            if txt:
                return txt
        return ''

    def _extract_element_expression(self, elem, ns_tag: str,
                                     elements_map: dict) -> str:
        """Extract expression text from an element (variable/contact/coil/etc)."""
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

        for e in elem.iter(f'{ns_tag}expression'):
            txt = (e.text or '').strip()
            if txt:
                return txt
        var_elem = elem.find(f'{ns_tag}variable')
        if var_elem is not None and var_elem.text:
            return var_elem.text.strip()
        return elem.get('value', '')
