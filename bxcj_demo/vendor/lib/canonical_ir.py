#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/canonical_ir.py — Canonical Program Element IR

格式无关的最小程序表示。所有 Decoder (PLCopen / SMC2 / ST) 输出同一批
Canonical Program Element，Program Primitive 只消费 Canonical Element，
不再接触原始文件格式。

设计原则:
  - 最小够用: 只表达三类工程实际需要的程序事实
  - 格式无关: PLCopen contact/coil/block 和 SMC2 F/CLs 都映射到同一批类型
  - 不含语义判断: 不出现 "step" / "actor" / "station" 等概念
  - 可序列化: 每个元素有 to_dict() / from_dict()
  - 保留拓扑: 每个 Element 携带 element_id / network_id，
              ProgramUnit 携带 connections 列表，ProgramConnection 表达有向边

元素类型 (element_type):
  program_unit      — 源码作用域 (POU / action / file)
  variable_ref      — 变量引用 (含限定路径 + 数组索引)
  literal           — 字面量值
  condition         — 比较/条件 (EQ/NE/GT/LT/GE/LE)
  assignment        — 赋值 (target := source)
  call              — 函数/FB 调用
  timer             — 定时器 (TON/TOF/TP)
  comment           — 注释
  branch            — 分支/嵌套结构
  raw_fragment      — 原始代码片段 (ST 代码等)
  program_connection — 有向连接 (source_element_id → target_element_id)

拓扑字段 (所有 Element 共有):
  element_id   — Element 在 ProgramUnit 内的唯一标识
                 (PLCopen: localId; SMC2: Ix; ST: 生成序号)
  network_id   — 网络/Rung 标识 (PLCopen: 无; SMC2: rung 序号; ST: 无)

ProgramConnection:
  source_element_id — 起点 Element 的 element_id
  target_element_id — 终点 Element 的 element_id
  connection_type   — wire / param_binding / parallel_branch / series_chain /
                      power_rail_in / power_rail_out
  source_param      — 参数名 (FB param_binding 时使用，如 "EN", "In1")
  target_param      — 参数名
  network_id        — 所属网络/Rung

PLCopen 映射:
  contact         → variable_ref (negated flag)
  coil            → assignment (target=variable_ref)
  block[EQ]       → condition (operator=EQ, operands=[variable_ref, literal])
  block[MOVE]     → assignment (target=variable_ref, source=literal)
  block[EXECUTE]  → raw_fragment (language=st)
  block[TON/TOF/TP] → timer
  block[other]    → call
  comment         → comment
  inVariable/outVariable → variable_ref (作为 input/output)
  connection@refLocalId → program_connection (source=refLocalId, target=localId)

SMC2 映射:
  LD            → variable_ref (Not=true → negated)
  ST            → assignment (target=variable_ref)
  F Name="="    → condition (operator=EQ, operands from In[1].Var, In[2].Var)
  F Name="MOVE" → assignment (target=Out[1].Var, source=In[1].Var)
  F Name=">="   → condition (operator=GE)
  F Name=other  → call (inputs from In[], outputs from Out[])
  CMT           → comment
  X,Y 坐标       → program_connection (series: 同 Y 连续 X; parallel: 同 X 不同 Y)
  In/Out 参数    → program_connection (connection_type=param_binding)

ST 代码映射:
  IF...THEN       → condition + branch
  :=              → assignment
  FB(...)         → call
  TON(...)        → timer
  (* comment *)   → comment
  raw text        → raw_fragment
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ============================================================
# Source Location
# ============================================================

@dataclass
class SourceLocation:
    """源码定位 — 追溯到原始文件的具体位置。"""
    file: str = ''
    line: int = 0
    column: int = 0
    local_id: str = ''       # PLCopen localId
    element_tag: str = ''    # Original XML tag (for debugging)

    def to_dict(self) -> dict:
        return {
            'file': self.file,
            'line': self.line,
            'column': self.column,
            'local_id': self.local_id,
            'element_tag': self.element_tag,
        }


# ============================================================
# Variable Flow Edge (data flow edge — distinct from topology edge)
# ============================================================

@dataclass
class VariableFlowEdge:
    """Data flow edge — variable definition → variable use.

    Distinct from ProgramConnection (topology edge). Represents the flow
    of data through variable assignment/read, potentially across rungs
    or POUs.

    Topology edges (wire, series_chain, parallel_branch) express physical
    wiring within a single rung/network. VariableFlowEdge expresses
    logical data flow: when a coil writes variable X in rung A and a
    contact reads X in rung B, an implicit data flow edge connects them.

    Fields:
        variable_identity   — Canonical base name (for matching)
        variable_expression — Full expression text (e.g. "Step_100_Active")
        source_element_id   — Element that defines/writes the variable
        source_network_id   — Network/rung of the definition
        source_unit_name    — ProgramUnit name of the definition
        source_file         — Source file
        target_element_id   — Element that uses/reads the variable
        target_network_id   — Network/rung of the use
        target_unit_name    — ProgramUnit name of the use
        target_file         — Source file
        flow_type           — assignment_def / call_output_def /
                              variable_ref_use / condition_operand_use /
                              call_input_use / timer_input_use
        scope               — same_unit / cross_unit_same_file / cross_file
        resolution_status   — resolved / ambiguous / conservative_skip
        confidence          — 0.0 to 1.0
        evidence            — Additional evidence dict
    """
    element_type: str = 'variable_flow_edge'
    variable_identity: str = ''
    variable_expression: str = ''

    source_element_id: str = ''
    source_network_id: str = ''
    source_unit_name: str = ''
    source_file: str = ''

    target_element_id: str = ''
    target_network_id: str = ''
    target_unit_name: str = ''
    target_file: str = ''

    flow_type: str = ''
    scope: str = ''
    resolution_status: str = ''
    confidence: float = 1.0
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'variable_identity': self.variable_identity,
            'variable_expression': self.variable_expression,
            'source_element_id': self.source_element_id,
            'source_network_id': self.source_network_id,
            'source_unit_name': self.source_unit_name,
            'source_file': self.source_file,
            'target_element_id': self.target_element_id,
            'target_network_id': self.target_network_id,
            'target_unit_name': self.target_unit_name,
            'target_file': self.target_file,
            'flow_type': self.flow_type,
            'scope': self.scope,
            'resolution_status': self.resolution_status,
            'confidence': self.confidence,
            'evidence': self.evidence,
        }


# ============================================================
# Program Connection (topology edge)
# ============================================================

@dataclass
class ProgramConnection:
    """程序连接 — 两个程序元素之间的有向拓扑边。

    保留原始程序中的连接关系，使 Primitive 能够判断:
      - Condition 如何控制 Action (series chain)
      - AND/OR 如何连接 (parallel branch)
      - FB 参数如何绑定 (param_binding)
      - branch 如何形成

    不含语义判断 — 只是拓扑事实。
    """
    element_type: str = 'program_connection'
    source_element_id: str = ''
    target_element_id: str = ''
    connection_type: str = ''    # wire, param_binding, parallel_branch,
                                 # series_chain, power_rail_in, power_rail_out
    source_param: str = ''       # FB 参数名 (e.g. "EN", "In1", "Out")
    target_param: str = ''
    network_id: str = ''         # 所属 network/rung
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'source_element_id': self.source_element_id,
            'target_element_id': self.target_element_id,
            'connection_type': self.connection_type,
            'source_param': self.source_param,
            'target_param': self.target_param,
            'network_id': self.network_id,
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Topology Mixin — common fields for all program elements
# ============================================================
#
# 每个 Program Element 携带 element_id 和 network_id 字段，
# 用于建立 ProgramConnection 的端点引用。
# element_id 在 ProgramUnit 内唯一:
#   - PLCopen: 来自 @localId
#   - SMC2: 来自 CLs 内的 Ix 字段
#   - ST: 生成序号
# network_id 标识 Element 所属的 rung/network:
#   - PLCopen: 通常为空 (rung 信息不在 XML 中显式表达)
#   - SMC2: rung 在 entry 中的序号
#   - ST: 通常为空


# ============================================================
# Variable Reference
# ============================================================

@dataclass
class VariableRef:
    """变量引用 — 限定路径 + 可选数组索引。

    Examples:
        EM06V.Process_steps[0]     → base_name=EM06V, path=[Process_steps], index=0
        EM06V.Cy_Clamp1.In.Auto_R  → base_name=EM06V, path=[Cy_Clamp1, In, Auto_R]
        Cell_Info[6,2].Finish      → base_name=Cell_Info, path=[Finish], index=(6,2)
        Reset[2]                    → base_name=Reset, path=[], index=2
    """
    element_type: str = 'variable_ref'
    element_id: str = ''      # Unique within ProgramUnit (localId / Ix)
    network_id: str = ''      # Network/rung identifier
    name: str = ''           # Full expression text (e.g. "EM06V.Process_steps[0]")
    base_name: str = ''      # Root variable (e.g. EM06V)
    path: list = field(default_factory=list)   # Member path (e.g. ['Process_steps'])
    index: Any = None        # int, tuple, or None
    negated: bool = False    # For negated contacts
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'name': self.name,
            'base_name': self.base_name,
            'path': self.path,
            'index': self.index,
            'negated': self.negated,
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Literal Value
# ============================================================

@dataclass
class Literal:
    """字面量值 — 数字、布尔、类型前缀值。"""
    element_type: str = 'literal'
    element_id: str = ''
    network_id: str = ''
    value: Any = None        # Parsed value (int, str, bool)
    raw_text: str = ''       # Original text (e.g. "INT#100", "T#3S", "TRUE")
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'value': self.value,
            'raw_text': self.raw_text,
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Condition / Comparison
# ============================================================

@dataclass
class Condition:
    """比较/条件 — EQ/NE/GT/LT/GE/LE 块或 ST IF 条件。"""
    element_type: str = 'condition'
    element_id: str = ''
    network_id: str = ''
    operator: str = ''       # EQ, NE, GT, LT, GE, LE
    operands: list = field(default_factory=list)  # List of VariableRef / Literal
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'operator': self.operator,
            'operands': [o.to_dict() if hasattr(o, 'to_dict') else o for o in self.operands],
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Assignment
# ============================================================

@dataclass
class Assignment:
    """赋值 — coil 或 MOVE 块。

    target: 被写入的变量引用
    source: 被赋值的值 (VariableRef 或 Literal)
    storage: coil 存储类型 — 'none' (普通线圈), 'set' (置位), 'reset' (复位)
             MOVE 块始终为 'none' (赋值语义)
    """
    element_type: str = 'assignment'
    element_id: str = ''
    network_id: str = ''
    target: VariableRef = None
    source: Any = None       # VariableRef or Literal
    storage: str = 'none'    # 'none', 'set', 'reset'
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'target': self.target.to_dict() if self.target else None,
            'source': self.source.to_dict() if self.source and hasattr(self.source, 'to_dict') else self.source,
            'storage': self.storage,
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Function / FB Call
# ============================================================

@dataclass
class Call:
    """函数/FB 调用 — block with typeName (non-EQ/MOVE/EXECUTE/timer)。"""
    element_type: str = 'call'
    element_id: str = ''
    network_id: str = ''
    callee: str = ''             # FB type name or function name
    instance_name: str = ''      # Instance name (for FB)
    inputs: list = field(default_factory=list)   # [{name, value: VariableRef|Literal}]
    outputs: list = field(default_factory=list)  # [{name, value: VariableRef|Literal}]
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        def _conv_param(p):
            return {
                'name': p.get('name', ''),
                'value': p['value'].to_dict() if p.get('value') and hasattr(p['value'], 'to_dict') else p.get('value'),
            }
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'callee': self.callee,
            'instance_name': self.instance_name,
            'inputs': [_conv_param(p) for p in self.inputs],
            'outputs': [_conv_param(p) for p in self.outputs],
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Timer
# ============================================================

@dataclass
class Timer:
    """定时器 — TON/TOF/TP。"""
    element_type: str = 'timer'
    element_id: str = ''
    network_id: str = ''
    timer_type: str = ''     # TON, TOF, TP
    preset: str = ''         # PT value raw text (e.g. "T#3S")
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'timer_type': self.timer_type,
            'preset': self.preset,
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Comment
# ============================================================

@dataclass
class Comment:
    """注释元素。"""
    element_type: str = 'comment'
    element_id: str = ''
    network_id: str = ''
    text: str = ''
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'text': self.text,
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Branch / Nested Structure
# ============================================================

@dataclass
class Branch:
    """分支/嵌套结构 — LD 并行分支、ST IF/CASE 块。"""
    element_type: str = 'branch'
    element_id: str = ''
    network_id: str = ''
    branch_type: str = ''    # parallel, alternative, if_then, case
    condition: Any = None    # Condition for conditional branches
    elements: list = field(default_factory=list)  # Nested ProgramElements
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'branch_type': self.branch_type,
            'condition': self.condition.to_dict() if self.condition and hasattr(self.condition, 'to_dict') else self.condition,
            'elements': [e.to_dict() if hasattr(e, 'to_dict') else e for e in self.elements],
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Raw Code Fragment
# ============================================================

@dataclass
class RawFragment:
    """原始代码片段 — ST 代码、未解析的文本块。"""
    element_type: str = 'raw_fragment'
    element_id: str = ''
    network_id: str = ''
    language: str = ''       # st, ld, fbd, text
    text: str = ''           # Raw code text
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'language': self.language,
            'text': self.text,
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Program Unit (source scope)
# ============================================================

@dataclass
class ProgramUnit:
    """源码作用域 — 一个 POU、action 或独立文件。"""
    element_type: str = 'program_unit'
    element_id: str = ''
    network_id: str = ''
    name: str = ''           # POU/action name
    unit_type: str = ''      # pou, action, datatype, file
    source_file: str = ''    # Source file path
    elements: list = field(default_factory=list)  # ProgramElements in this unit
    connections: list = field(default_factory=list)  # [ProgramConnection] — topology edges
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'element_type': self.element_type,
            'element_id': self.element_id,
            'network_id': self.network_id,
            'name': self.name,
            'unit_type': self.unit_type,
            'source_file': self.source_file,
            'elements': [e.to_dict() if hasattr(e, 'to_dict') else e for e in self.elements],
            'connections': [c.to_dict() if hasattr(c, 'to_dict') else c for c in self.connections],
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Decoded Program — Decoder output container
# ============================================================

@dataclass
class DecodedProgram:
    """Decoder 输出 — 包含 ProgramUnits + VariableDeclarations + VariableFlowEdges + ECRecords.

    VariableDeclarations 是结构化变量声明记录 (name, scope, datatype,
    documentation, at_address)，供 Evidence Extractor 使用。
    ProgramUnits 包含 Canonical Program Elements，供 Primitive 使用。
    VariableFlowEdges 包含跨 rung/POU 的变量数据流边，供 StepLogicSlicer 使用。
    ECRecords 包含元素注释 (EC) 记录，供 EC Evidence Extractor 使用。
    """
    format: str = ''         # plcopen_xml, smc2, st, unknown
    source_file: str = ''
    program_units: list = field(default_factory=list)     # [ProgramUnit]
    variable_declarations: list = field(default_factory=list)  # [dict]
    variable_flow_edges: list = field(default_factory=list)    # [VariableFlowEdge]
    ec_records: list = field(default_factory=list)             # [ECRecord]
    raw_stats: dict = field(default_factory=dict)         # Decoder statistics

    def to_dict(self) -> dict:
        return {
            'format': self.format,
            'source_file': self.source_file,
            'program_units': [u.to_dict() for u in self.program_units],
            'variable_declarations': [
                vd.to_dict() if hasattr(vd, 'to_dict') else vd
                for vd in self.variable_declarations
            ],
            'variable_flow_edges': [
                e.to_dict() if hasattr(e, 'to_dict') else e
                for e in self.variable_flow_edges
            ],
            'ec_records': [
                ecr.to_dict() if hasattr(ecr, 'to_dict') else ecr
                for ecr in self.ec_records
            ],
            'raw_stats': self.raw_stats,
        }

    def save(self, path: str):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


# ============================================================
# Variable Declaration Record (decoded, not yet Entity)
# ============================================================

@dataclass
class VariableDeclaration:
    """变量声明记录 — Decoder 输出的结构化变量信息。

    这是 Decoder 对 <variable> 元素的结构化解码结果。
    Evidence Extractor 读取此记录创建 Entity + Relation。
    """
    name: str = ''
    scope: str = ''          # e.g. "pou:EM09_焊后除尘模组"
    derived_type: str = ''   # e.g. "ST_Cylinder_06Var"
    documentation: str = ''  # Documentation text
    at_address: str = ''     # AT binding address
    source_file: str = ''

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'scope': self.scope,
            'derived_type': self.derived_type,
            'documentation': self.documentation,
            'at_address': self.at_address,
            'source_file': self.source_file,
        }


# ============================================================
# EC Record (Element Comment — decoded, not yet Relation)
# ============================================================

@dataclass
class ECRecord:
    """元素注释记录 — Decoder 输出的结构化 EC 信息。

    EC (Element Comment) 是 PLC 变量数组中每个索引的地址和描述注释。
    Decoder 只负责解出 ECRecord，不创建 maps_to Relation。
    由通用 EC Evidence Extractor 将 ECRecord 转成 Relation。

    Fields:
        owner_symbol    — 拥有此 EC 的变量名 (e.g., 'm_bOutput')
        index           — 数组索引 (e.g., 106)
        address         — 物理地址 (e.g., 'Y006.10')
        description     — 描述文本 (e.g., 'S11AML抚平气缸缩回')
        raw_fields      — 原始字段字典 (保留所有原始字段)
        source_location — 源码定位
    """
    owner_symbol: str = ''
    index: int = -1
    address: str = ''
    description: str = ''
    raw_fields: dict = field(default_factory=dict)
    source_location: SourceLocation = None

    def to_dict(self) -> dict:
        return {
            'owner_symbol': self.owner_symbol,
            'index': self.index,
            'address': self.address,
            'description': self.description,
            'raw_fields': self.raw_fields,
            'source_location': self.source_location.to_dict() if self.source_location else None,
        }


# ============================================================
# Element type registry for deserialization
# ============================================================

ELEMENT_TYPE_MAP = {
    'variable_ref': VariableRef,
    'literal': Literal,
    'condition': Condition,
    'assignment': Assignment,
    'call': Call,
    'timer': Timer,
    'comment': Comment,
    'branch': Branch,
    'raw_fragment': RawFragment,
    'program_unit': ProgramUnit,
    'program_connection': ProgramConnection,
    'variable_flow_edge': VariableFlowEdge,
    'ec_record': ECRecord,
}


def element_from_dict(d: dict):
    """Deserialize a dict back to a Canonical Program Element."""
    etype = d.get('element_type', '')
    if etype not in ELEMENT_TYPE_MAP:
        return d  # Unknown type, return as-is

    cls = ELEMENT_TYPE_MAP[etype]
    kwargs = {}
    for k, v in d.items():
        if k == 'source_location' and v:
            kwargs[k] = SourceLocation(**v)
        elif k == 'operands' and isinstance(v, list):
            kwargs[k] = [element_from_dict(o) if isinstance(o, dict) else o for o in v]
        elif k == 'target' and isinstance(v, dict):
            kwargs[k] = element_from_dict(v)
        elif k == 'source' and isinstance(v, dict):
            kwargs[k] = element_from_dict(v)
        elif k == 'elements' and isinstance(v, list):
            kwargs[k] = [element_from_dict(e) if isinstance(e, dict) else e for e in v]
        elif k == 'connections' and isinstance(v, list):
            kwargs[k] = [element_from_dict(c) if isinstance(c, dict) else c for c in v]
        elif k == 'inputs' and isinstance(v, list):
            kwargs[k] = [{'name': p.get('name', ''), 'value': element_from_dict(p['value']) if isinstance(p.get('value'), dict) else p.get('value')} for p in v]
        elif k == 'outputs' and isinstance(v, list):
            kwargs[k] = [{'name': p.get('name', ''), 'value': element_from_dict(p['value']) if isinstance(p.get('value'), dict) else p.get('value')} for p in v]
        else:
            kwargs[k] = v

    # Filter to only valid constructor args
    valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
    filtered = {k: v for k, v in kwargs.items() if k in valid_fields}
    return cls(**filtered)
