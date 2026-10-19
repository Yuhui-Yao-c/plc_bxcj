#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/decoders/smc2_decoder.py — SMC2 CDATA JSON → Canonical Program IR

职责:
  解析 SMC2 XML 文件 (CODESYS SMC2 导出格式)，输出 DecodedProgram:
    - ProgramUnits (含 Canonical Program Elements + ProgramConnections)
    - VariableDeclarations (从 VLs 或变量引用中提取)

不做:
  - 判断什么是 Step / actor / station / operation
  - 创建 Entity / Relation
  - 应用项目命名规则
  - 硬编码 m_nNextAutoStep / thread 语义

SMC2 格式:
  外层 XML: <smc2 name="..." entries="N" source="...">
    <group name="json" count="N">
      <entry name="uuid.xml" type="json" size="N"><![CDATA[ ... ]]></entry>
      ...
    </group>
    <group name="slwd" .../>
    <group name="cxil" .../>
    <group name="other" .../>

  CDATA 内容: 换行分隔的 JSON 对象，每个对象是一个 "rung" (network):
    {
      "CLs": [...],      // 逻辑元素列表
      "CMT": "...",      // 注释 (可选)
      "LRI": N,          // 左母线输入 element index
      "RRI": N,          // 右母线输出 element index
      "VLs": [...]       // 变量连接点
    }

  CLs 元素类型:
    LD  — Load (contact): {__type:"LD", Ix:N, Not:bool, Var:"name", X:N, Y:N}
    ST  — Store (coil):   {__type:"ST", Ix:N, Var:"name", X:N, Y:N}
    HL  — Horizontal Link (wire): {__type:"HL", X:N, Y:N}
    F   — Function block: {__type:"F", Ix:N, Name:"=", In:[...], Out:[...], X:N, PL:bool}
    FB  — Function Block call: similar to F
    IST — ???
    JMP — Jump

  F 元素参数结构:
    In: [{__type:"PF"|"PRM", Arg:"EN"|"In1", Ix:N, Type:"...", Var:"..."}]
    Out: [{__type:"PF"|"PRM", Arg:"ENO"|"Out", Ix:N, Type:"...", Var:"..."}]
    PF = formal parameter (no value), PRM = parameter with value

映射规则 (格式知识，非业务知识):
  LD            → variable_ref (Not=true → negated)
  ST            → assignment (target=variable_ref)
  F Name="="    → condition (operator=EQ, operands from In[1].Var, In[2].Var)
  F Name="<>"   → condition (operator=NE)
  F Name=">"    → condition (operator=GT)
  F Name=">="   → condition (operator=GE)
  F Name="<"    → condition (operator=LT)
  F Name="<="   → condition (operator=LE)
  F Name="MOVE" → assignment (target=Out[1].Var, source=In[1].Var)
  F Name=other  → call (callee=Name, inputs from In[], outputs from Out[])
  CMT           → comment
  X,Y 坐标       → program_connection (series: 同 Y 连续 X; parallel: 同 X 不同 Y)
  In/Out 参数    → program_connection (connection_type=param_binding)
  Ix            → element_id
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ..canonical_ir import (
    DecodedProgram, ProgramUnit, VariableDeclaration,
    VariableRef, Literal, Condition, Assignment, Call, Timer,
    Comment, Branch, RawFragment, SourceLocation, ProgramConnection,
    ECRecord,
)


# Operator mapping from SMC2 F Name to Canonical operator
SMC2_OPERATOR_MAP = {
    '=': 'EQ',
    '<>': 'NE',
    '>': 'GT',
    '>=': 'GE',
    '<': 'LT',
    '<=': 'LE',
}


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


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
                        source_loc: SourceLocation = None,
                        element_id: str = '',
                        network_id: str = '') -> VariableRef:
    """Parse a variable expression text into a VariableRef."""
    expr_text = (expr_text or '').strip()
    ref = VariableRef(
        name=expr_text,
        base_name=expr_text,
        negated=negated,
        source_location=source_loc,
        element_id=element_id,
        network_id=network_id,
    )
    if not expr_text:
        return ref

    # Try root[index].path
    m = re.match(r'^(\w+)\[([^\]]*)\]\.?(.*)$', expr_text)
    if m:
        ref.base_name = m.group(1)
        idx_str = m.group(2)
        rest = m.group(3)
        try:
            if ',' in idx_str:
                ref.index = tuple(int(p.strip()) for p in idx_str.split(','))
            else:
                ref.index = int(idx_str)
        except ValueError:
            pass
        if rest:
            ref.path = rest.split('.')
        return ref

    m2 = re.match(r'^(\w+)\.(.*)$', expr_text)
    if m2:
        ref.base_name = m2.group(1)
        ref.path = m2.group(2).split('.')
        return ref

    m3 = re.match(r'^(\w+)$', expr_text)
    if m3:
        ref.base_name = m3.group(1)
        return ref

    return ref


class SMC2Decoder:
    """SMC2 CDATA JSON → DecodedProgram

    解析 SMC2 XML 文件，提取 json group 中的 CDATA JSON 对象，
    映射为 Canonical Program IR。
    """

    def decode(self, xml_path: str) -> DecodedProgram:
        """Decode a single SMC2 XML file into DecodedProgram."""
        path = Path(xml_path)
        program = DecodedProgram(
            format='smc2',
            source_file=str(path),
        )
        if not path.exists():
            return program

        try:
            tree = ET.parse(str(path))
            root = tree.getroot()
        except Exception:
            return program

        file_name = path.name

        # Find json group
        json_group = None
        slwd_group = None
        for g in root:
            gname = g.get('name', '')
            if gname == 'json':
                json_group = g
            elif gname == 'slwd':
                slwd_group = g

        if json_group is None:
            return program

        entries = list(json_group)
        program_units = []
        var_decls_map = {}  # name → VariableDeclaration (dedup)

        for entry_idx, entry in enumerate(entries):
            cdata = entry.text
            if not cdata:
                continue
            entry_name = entry.get('name', f'entry_{entry_idx}')
            unit = self._decode_entry(cdata, entry_name, entry_idx, file_name,
                                       var_decls_map)
            if unit is not None:
                program_units.append(unit)

        program.program_units = program_units
        program.variable_declarations = list(var_decls_map.values())

        # Parse EC records from slwd group
        ec_records = []
        if slwd_group is not None:
            ec_records = self._decode_slwd_group(slwd_group, file_name)
        program.ec_records = ec_records

        program.raw_stats = {
            'total_entries': len(entries),
            'total_program_units': len(program_units),
            'total_elements': sum(len(u.elements) for u in program_units),
            'total_connections': sum(len(u.connections) for u in program_units),
            'total_variables': len(program.variable_declarations),
            'total_ec_records': len(ec_records),
        }
        return program

    def decode_files(self, xml_files: list) -> list:
        """Decode multiple SMC2 XML files. Returns list of DecodedProgram."""
        return [self.decode(f) for f in xml_files]

    def _decode_entry(self, cdata: str, entry_name: str, entry_idx: int,
                       file_name: str, var_decls_map: dict) -> ProgramUnit:
        """Decode one entry's CDATA into a ProgramUnit.

        CDATA may contain newline-separated JSON objects (rungs).
        Each rung becomes a network within the ProgramUnit.
        """
        objs = self._parse_cdata(cdata)
        if not objs:
            return None

        unit = ProgramUnit(
            name=entry_name,
            unit_type='file',
            source_file=file_name,
            source_location=SourceLocation(
                file=file_name, element_tag='entry'),
        )

        for rung_idx, obj in enumerate(objs):
            if not isinstance(obj, dict):
                continue
            network_id = f'{entry_idx}:{rung_idx}'
            self._decode_rung(obj, network_id, rung_idx, file_name, unit,
                               var_decls_map)

        return unit if unit.elements or unit.connections else None

    def _decode_slwd_group(self, slwd_group, file_name: str) -> list:
        """Decode the slwd group to extract ECRecord entries.

        The slwd group contains tab-separated variable declaration lines.
        Each line may have an EC= field with XML-like element comments:
            ++D=ARRAY[0..3000] OF BOOL\\tN=m_bOutput\\tG=VAR_EXTERNAL\\tEC=<ECs><EC EK="[106]" C="Y006.10_S11AML抚平气缸缩回" /></ECs>

        This method only extracts EC records. Variable declarations are
        already handled by the json group decoding.
        """
        ec_records = []
        for entry in slwd_group:
            cdata = entry.text
            if not cdata:
                continue
            for line in cdata.split('\n'):
                line = line.strip()
                if not line or not line.startswith('++'):
                    continue
                ec_records.extend(self._parse_slwd_line(line, file_name))
        return ec_records

    def _parse_slwd_line(self, line: str, file_name: str) -> list:
        """Parse a single slwd line and extract EC records if present.

        Format: ++D=TYPE\\tN=NAME\\tG=GROUP\\tEC=<ECs>...</ECs>
        EC entries: <EC EK="[index]" C="address_description" />
        """
        ec_records = []

        # Find EC= field
        ec_match = re.search(r'EC=(<ECs>.*?</ECs>)', line)
        if not ec_match:
            return ec_records

        ec_xml = ec_match.group(1)

        # Extract variable name from N= field
        n_match = re.search(r'N=(\S+)', line)
        owner_symbol = n_match.group(1) if n_match else ''

        # Parse individual EC entries
        # Format: <EC EK="[106]" C="Y006.10_S11AML抚平气缸缩回" />
        for ec_entry in re.finditer(r'<EC\s+EK="\[(\d+)\]"\s+C="([^"]*)"', ec_xml):
            index = int(ec_entry.group(1))
            raw_comment = ec_entry.group(2)

            # Split address and description (format: address_description)
            # Address is typically Y/X followed by digits.digits
            addr_match = re.match(r'([XY]\d+\.\d+)[_](.*)', raw_comment)
            if addr_match:
                address = addr_match.group(1)
                description = addr_match.group(2)
            else:
                address = ''
                description = raw_comment

            ec_records.append(ECRecord(
                owner_symbol=owner_symbol,
                index=index,
                address=address,
                description=description,
                raw_fields={'raw_comment': raw_comment},
                source_location=SourceLocation(
                    file=file_name, element_tag='slwd:EC'),
            ))

        return ec_records

    def _parse_cdata(self, cdata: str) -> list:
        """Parse CDATA as one or more JSON objects."""
        cdata = cdata.strip()
        if not cdata:
            return []
        # Try single JSON
        try:
            obj = json.loads(cdata)
            if isinstance(obj, list):
                return obj
            return [obj]
        except json.JSONDecodeError:
            pass
        # Try newline-separated
        objs = []
        for line in cdata.split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return objs

    def _decode_rung(self, obj: dict, network_id: str, rung_idx: int,
                      file_name: str, unit: ProgramUnit, var_decls_map: dict):
        """Decode a single rung (JSON object) into Canonical Elements."""
        cls_list = obj.get('CLs', [])
        cmt = obj.get('CMT', '')
        lri = obj.get('LRI')
        rri = obj.get('RRI')

        loc = SourceLocation(file=file_name, element_tag='rung')

        # Comment
        if cmt:
            comment = Comment(
                text=cmt,
                source_location=loc,
                element_id=f'cmt:{network_id}',
                network_id=network_id,
            )
            unit.elements.append(comment)

        # Build element_id map for this rung: raw_order → element_id
        # This ensures consistency between element decoding and connection building
        id_map = {}  # raw_order → element_id
        for raw_order, cl in enumerate(cls_list):
            if not isinstance(cl, dict):
                continue
            id_map[raw_order] = self._make_element_id(cl, network_id, raw_order)

        # Decode CLs elements
        for raw_order, cl in enumerate(cls_list):
            if not isinstance(cl, dict):
                continue
            elem = self._decode_cls_element(cl, network_id, file_name,
                                             var_decls_map, id_map[raw_order])
            if elem is None:
                continue
            if isinstance(elem, list):
                unit.elements.extend(elem)
            else:
                unit.elements.append(elem)

        # Build connections from X,Y coordinates and In/Out params
        connections = self._build_rung_connections(cls_list, network_id,
                                                    file_name, lri, rri, id_map)
        unit.connections.extend(connections)

    def _make_element_id(self, cl: dict, network_id: str,
                         raw_order: int) -> str:
        """Generate a deterministic element_id for a CLs element.

        If the element has an Ix field, use it directly.
        Otherwise, generate a synthetic ID:
            {network_id}/{raw_order}/{type}

        This ensures:
          - Same input always produces the same ID (no randomness)
          - Elements without Ix (HL, some LD) still participate in topology
          - IDs are unique within a ProgramUnit (network_id includes entry:rung)
        """
        ix = cl.get('Ix')
        if ix is not None:
            # Ix is only unique inside a rung. The global lookup and DefUse
            # engine require a unique element ID across this ProgramUnit.
            return f'{network_id}/Ix/{ix}'
        cl_type = cl.get('__type', 'unk')
        return f'{network_id}/{raw_order}/{cl_type}'

    def _decode_cls_element(self, cl: dict, network_id: str,
                             file_name: str, var_decls_map: dict,
                             element_id: str = ''):
        """Decode a single CLs element into Canonical Program Element(s)."""
        cl_type = cl.get('__type', '')
        loc = SourceLocation(
            file=file_name,
            local_id=element_id,
            element_tag=cl_type,
        )

        if cl_type == 'LD':
            return self._decode_ld(cl, loc, element_id, network_id,
                                    var_decls_map)
        if cl_type == 'ST':
            return self._decode_st(cl, loc, element_id, network_id,
                                    var_decls_map)
        if cl_type == 'F':
            return self._decode_f(cl, loc, element_id, network_id,
                                   var_decls_map)
        if cl_type == 'FB':
            return self._decode_f(cl, loc, element_id, network_id,
                                   var_decls_map)
        # HL (horizontal link), IST, JMP — not emitted as elements
        # but they DO get element_ids for topology participation
        return None

    def _decode_ld(self, cl: dict, loc: SourceLocation,
                    element_id: str, network_id: str,
                    var_decls_map: dict) -> VariableRef:
        """Decode LD (Load/contact) → VariableRef."""
        var = cl.get('Var', '')
        negated = cl.get('Not', False)
        ref = _parse_variable_ref(var, negated=negated, source_loc=loc,
                                   element_id=element_id, network_id=network_id)
        # Record variable declaration
        if var:
            self._record_var(var, var_decls_map, file_name=loc.file)
        return ref

    def _decode_st(self, cl: dict, loc: SourceLocation,
                    element_id: str, network_id: str,
                    var_decls_map: dict) -> Assignment:
        """Decode ST (Store/coil) → Assignment."""
        var = cl.get('Var', '')
        target = _parse_variable_ref(var, source_loc=loc,
                                      element_id=element_id,
                                      network_id=network_id)
        if var:
            self._record_var(var, var_decls_map, file_name=loc.file)
        return Assignment(
            target=target,
            source=None,  # Source resolved via topology
            source_location=loc,
            element_id=element_id,
            network_id=network_id,
        )

    def _decode_f(self, cl: dict, loc: SourceLocation,
                   element_id: str, network_id: str,
                   var_decls_map: dict):
        """Decode F (Function block) → Condition / Assignment / Call."""
        name = cl.get('Name', '')
        inputs = cl.get('In', [])
        outputs = cl.get('Out', [])

        # Comparison operators → Condition
        if name in SMC2_OPERATOR_MAP:
            return self._decode_comparison_f(
                name, inputs, loc, element_id, network_id, var_decls_map)

        # MOVE → Assignment
        if name == 'MOVE':
            return self._decode_move_f(
                inputs, outputs, loc, element_id, network_id, var_decls_map)

        # Timer functions → Timer
        if name in ('TON', 'TOF', 'TP', 'Timer'):
            return self._decode_timer_f(
                name, inputs, loc, element_id, network_id, var_decls_map)

        # Everything else → Call
        return self._decode_call_f(
            name, inputs, outputs, loc, element_id, network_id, var_decls_map)

    def _decode_comparison_f(self, name: str, inputs: list,
                              loc: SourceLocation, element_id: str,
                              network_id: str, var_decls_map: dict) -> Condition:
        """Decode comparison F block → Condition."""
        operator = SMC2_OPERATOR_MAP[name]
        operands = []
        for inp in inputs:
            if not isinstance(inp, dict):
                continue
            arg = inp.get('Arg', '')
            if arg in ('EN', 'ENO', ''):
                continue
            var = inp.get('Var', '')
            if not var:
                continue
            if _is_literal_value(var):
                operands.append(Literal(
                    value=_parse_literal_value(var),
                    raw_text=var,
                    source_location=loc,
                    network_id=network_id,
                ))
            else:
                operands.append(_parse_variable_ref(
                    var, source_loc=loc, network_id=network_id))
                self._record_var(var, var_decls_map, file_name=loc.file)
        return Condition(
            operator=operator,
            operands=operands,
            source_location=loc,
            element_id=element_id,
            network_id=network_id,
        )

    def _decode_move_f(self, inputs: list, outputs: list,
                        loc: SourceLocation, element_id: str,
                        network_id: str, var_decls_map: dict) -> Assignment:
        """Decode MOVE F block → Assignment."""
        # Source from first input with a Var value (skip EN)
        source = None
        for inp in inputs:
            if not isinstance(inp, dict):
                continue
            arg = inp.get('Arg', '')
            if arg in ('EN', 'ENO', ''):
                continue
            var = inp.get('Var', '')
            if not var:
                continue
            if _is_literal_value(var):
                source = Literal(
                    value=_parse_literal_value(var),
                    raw_text=var,
                    source_location=loc,
                    network_id=network_id,
                )
            else:
                source = _parse_variable_ref(
                    var, source_loc=loc, network_id=network_id)
                self._record_var(var, var_decls_map, file_name=loc.file)
            break

        # Target from first output with a Var value (skip ENO)
        target = None
        for out in outputs:
            if not isinstance(out, dict):
                continue
            arg = out.get('Arg', '')
            if arg in ('EN', 'ENO', ''):
                continue
            var = out.get('Var', '')
            if not var:
                continue
            target = _parse_variable_ref(
                var, source_loc=loc, network_id=network_id)
            self._record_var(var, var_decls_map, file_name=loc.file)
            break

        return Assignment(
            target=target,
            source=source,
            source_location=loc,
            element_id=element_id,
            network_id=network_id,
        )

    def _decode_timer_f(self, name: str, inputs: list,
                         loc: SourceLocation, element_id: str,
                         network_id: str, var_decls_map: dict) -> Timer:
        """Decode Timer F block → Timer."""
        pt = ''
        timer_type = name
        if name == 'Timer':
            timer_type = 'TON'  # Default
        for inp in inputs:
            if not isinstance(inp, dict):
                continue
            var = inp.get('Var', '')
            if var and (var.startswith('T#') or
                        re.match(r'^[\d.]+(ms|s|S|m|h)$', var)):
                pt = var
                break
        return Timer(
            timer_type=timer_type,
            preset=pt,
            source_location=loc,
            element_id=element_id,
            network_id=network_id,
        )

    def _decode_call_f(self, name: str, inputs: list, outputs: list,
                        loc: SourceLocation, element_id: str,
                        network_id: str, var_decls_map: dict) -> Call:
        """Decode generic F block → Call."""
        call_inputs = []
        call_outputs = []

        for inp in inputs:
            if not isinstance(inp, dict):
                continue
            arg = inp.get('Arg', '')
            if arg in ('EN', ''):
                continue
            var = inp.get('Var', '')
            if not var:
                continue
            if _is_literal_value(var):
                call_inputs.append({
                    'name': arg,
                    'value': Literal(
                        value=_parse_literal_value(var),
                        raw_text=var,
                        source_location=loc,
                        network_id=network_id,
                    )
                })
            else:
                call_inputs.append({
                    'name': arg,
                    'value': _parse_variable_ref(
                        var, source_loc=loc, network_id=network_id)
                })
                self._record_var(var, var_decls_map, file_name=loc.file)

        for out in outputs:
            if not isinstance(out, dict):
                continue
            arg = out.get('Arg', '')
            if arg in ('ENO', ''):
                continue
            var = out.get('Var', '')
            if not var:
                continue
            call_outputs.append({
                'name': arg,
                'value': _parse_variable_ref(
                    var, source_loc=loc, network_id=network_id)
            })
            self._record_var(var, var_decls_map, file_name=loc.file)

        # Instance name: use Ix or Name as instance
        instance_name = ''
        if element_id:
            instance_name = f'{name}_{element_id}'

        return Call(
            callee=name,
            instance_name=instance_name,
            inputs=call_inputs,
            outputs=call_outputs,
            source_location=loc,
            element_id=element_id,
            network_id=network_id,
        )

    def _build_rung_connections(self, cls_list: list, network_id: str,
                                 file_name: str, lri=None, rri=None,
                                 id_map: dict = None) -> list:
        """Build ProgramConnection list from CLs X,Y coordinates.

        SMC2 topology is defined by the 2D grid (X, Y):
          - X = series position (column, left to right)
          - Y = parallel branch (row, 0 = main branch)

        Connection types:
          - power_rail_in: LRI → first element on Y=0
          - power_rail_out: last element → RRI
          - series_chain: same Y, consecutive X (element output → next input)
          - parallel_branch: Y=0 element branches to Y=N at same X+1
          - branch_merge: Y=N branch merges back to Y=0 at X where Y=N ends

        IMPORTANT: In/Out parameter Ix values are NOT CLs element references.
        They are internal parameter indices. The actual variable values are
        already resolved into Canonical Element operands/targets by the decoder.
        Topology is purely through X,Y grid coordinates.

        Elements without Ix (HL, some LD) now get synthetic IDs from id_map
        and participate in topology. HL elements are still not emitted as
        Canonical Elements but their positions are used to bridge gaps in
        the series chain.
        """
        connections = []
        id_map = id_map or {}

        # Collect elements with their positions and element_ids
        # Use id_map for consistent element_id assignment
        # FIX v10.36: Include elements without X coordinate (e.g., common enable LD
        # like m_bAutoRun[3]) by assigning them implicit position (0, Y).
        # These elements are critical guards/common_enables that were previously
        # dropped from topology, causing all guards to be lost.
        positioned = []  # [(x, y, element_id, cl, raw_order)]
        for raw_order, cl in enumerate(cls_list):
            if not isinstance(cl, dict):
                continue
            x = cl.get('X')
            y = cl.get('Y', 0)
            if x is None:
                # Elements without X are implicit position (0, y)
                # These are typically the first LD (common enable) or HL links
                x = 0
            eid = id_map.get(raw_order, str(cl.get('Ix', raw_order)))
            positioned.append((x, y, eid, cl, raw_order))

        # Build position lookup: (x, y) → [element_id, ...]
        pos_map = {}
        for x, y, eid, cl, raw_order in positioned:
            key = (x, y)
            if key not in pos_map:
                pos_map[key] = []
            pos_map[key].append(eid)

        # Build element_id set for validity checking
        eid_set = {eid for _, _, eid, _, _ in positioned}

        # Power rail connections — LRI/RRI refer to raw Ix, not our
        # network-qualified canonical element ID.
        raw_ix_to_eid = {
            str(cl['Ix']): id_map[order]
            for order, cl in enumerate(cls_list)
            if isinstance(cl, dict) and cl.get('Ix') is not None and order in id_map
        }
        if lri is not None:
            lri_eid = raw_ix_to_eid.get(str(lri), str(lri))
            if lri_eid in eid_set:
                connections.append(ProgramConnection(
                    source_element_id=f'LRI:{network_id}',
                    target_element_id=lri_eid,
                    connection_type='power_rail_in',
                    network_id=network_id,
                    source_location=SourceLocation(
                        file=file_name, element_tag='LRI'),
                ))
        if rri is not None:
            rri_eid = raw_ix_to_eid.get(str(rri), str(rri))
            if rri_eid in eid_set:
                connections.append(ProgramConnection(
                    source_element_id=rri_eid,
                    target_element_id=f'RRI:{network_id}',
                    connection_type='power_rail_out',
                    network_id=network_id,
                    source_location=SourceLocation(
                        file=file_name, element_tag='RRI'),
                ))

        # Group by Y, sort by X within each Y
        by_y = {}
        for x, y, eid, cl, raw_order in positioned:
            if y not in by_y:
                by_y[y] = []
            by_y[y].append((x, eid, cl))

        for y in by_y:
            by_y[y].sort(key=lambda e: e[0])

        # Series chain: same Y, adjacent in sorted X order
        # HL elements without Ix are now included via synthetic IDs,
        # so the gap bridging is automatic — adjacent elements in the
        # sorted list are connected regardless of X gap.
        for y, elems in by_y.items():
            for i in range(len(elems) - 1):
                x1, eid1, cl1 = elems[i]
                x2, eid2, cl2 = elems[i + 1]
                connections.append(ProgramConnection(
                    source_element_id=eid1,
                    target_element_id=eid2,
                    connection_type='series_chain',
                    network_id=network_id,
                    source_location=SourceLocation(
                        file=file_name, element_tag=cl1.get('__type', '')),
                ))

        # Parallel branch and merge:
        # At each X on Y=0, if Y=1+ has elements at X+1, it's a branch.
        # When Y=N has no element at X+1 but Y=0 does, it's a merge.
        y_values = sorted(by_y.keys())
        if len(y_values) > 1:
            main_y = 0
            branch_ys = [y for y in y_values if y != main_y]

            for branch_y in branch_ys:
                branch_elems = by_y[branch_y]
                for i, (x, eid, cl) in enumerate(branch_elems):
                    next_x = x + 1
                    # Check if branch continues at next_x
                    if (next_x, branch_y) in pos_map:
                        continue  # Branch continues — series chain handles it
                    # Branch ends — merge to main branch
                    merge_target = None
                    if (next_x, main_y) in pos_map:
                        merge_target = pos_map[(next_x, main_y)][0]
                    elif (x, main_y) in pos_map:
                        merge_target = pos_map[(x, main_y)][0]
                    if merge_target is not None:
                        connections.append(ProgramConnection(
                            source_element_id=eid,
                            target_element_id=merge_target,
                            connection_type='branch_merge',
                            network_id=network_id,
                            source_location=SourceLocation(
                                file=file_name, element_tag=cl.get('__type', '')),
                        ))

            # Branch start: from main branch to first element of branch
            for branch_y in branch_ys:
                branch_elems = by_y[branch_y]
                if not branch_elems:
                    continue
                first_x, first_eid, first_cl = branch_elems[0]
                # Find main branch element at first_x - 1
                origin_x = first_x - 1
                if (origin_x, main_y) in pos_map:
                    origin_eid = pos_map[(origin_x, main_y)][0]
                    connections.append(ProgramConnection(
                        source_element_id=origin_eid,
                        target_element_id=first_eid,
                        connection_type='parallel_branch',
                        network_id=network_id,
                        source_location=SourceLocation(
                            file=file_name, element_tag='LD'),
                    ))

        return connections

    def _record_var(self, var_name: str, var_decls_map: dict,
                     file_name: str = ''):
        """Record a variable reference as a minimal VariableDeclaration."""
        if not var_name or var_name in var_decls_map:
            return
        # Extract base name (without array index)
        base = var_name
        m = re.match(r'^(\w+)', var_name)
        if m:
            base = m.group(1)
        var_decls_map[var_name] = VariableDeclaration(
            name=var_name,
            scope='',
            derived_type='',
            documentation='',
            at_address='',
            source_file=file_name,
        )
