#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semantic/evidence_model.py — 统一语义证据模型

设计目标:
  - 不管证据来自 PLC documentation、HMI、SMC2 EC、FB 参数、POU/station metadata，
    最终都进入同一套 Entity + Relation 表示。
  - 不依赖平台分支（汇川/信捷/威纶通...）。
  - 关系有可信度等级 + 证据来源，便于 Resolver 合并多源证据 + 标记冲突。

实体类型 (entity_type) — 按实际工程抽象，可扩展:
  - plc_variable        : PLC 程序中的变量（如 m_bOutput[0], EM01V.Cy_Up1, CY1_XV）
  - actuator            : 物理执行机构（气缸/电机/轴/阀门等）
  - physical_actuator   : 物理执行机构聚合实体（command + feedback 配对）
  - fb_instance         : FB 调用实例
  - fb_type             : FB 类型定义（如 FB_RMctrl, FB_Cylinder）
  - parameter           : FB 参数（如 Q_气缸工位, In.Astro_R）
  - physical_address    : 物理地址（如 Y110513, %QX0.0）
  - hmi_component       : HMI 组件（按钮/指示灯/数值显示等）
  - hmi_label           : HMI 显示文本
  - documentation_text  : PLC 变量 documentation 中的文本
  - station_thread      : 工站/线程/机构
  - position            : 位置/姿态（如 取料位, 焊接位）
  - comment             : 程序注释文本
  - pou                 : POU 程序组织单元
  - action_word         : 动作词（如 压紧/松开/上升/下降）

关系类型 (relation_type) — 表达实体间的语义连接:
  - documented_as       : plc_variable -[documented_as]-> documentation_text
  - maps_to             : plc_variable -[maps_to]-> physical_address
                          (通过 AT 绑定/EC 映射/io_table 等方式)
  - labelled_as         : physical_address -[labelled_as]-> hmi_label
                          (HMI 二进制中 Y 地址对应的中文标签)
  - belongs_to          : plc_variable -[belongs_to]-> station_thread / pou
                          (变量归属哪个工站/POU)
  - input_of / output_of: parameter -[input_of/output_of]-> fb_instance
                          (FB 参数关系)
  - refers_to           : parameter -[refers_to]-> plc_variable
                          (FB 参数引用的变量，如 Q_气缸工位 → m_bOutput[0])
  - position_of         : position -[position_of]-> actuator
                          (位置属于哪个执行机构)
  - action_of           : action_word -[action_of]-> actuator
                          (动作属于哪个执行机构，如 压紧→气缸)
  - named_as            : pou -[named_as]-> station_thread
                          (POU 名字揭示工站，如 EM01_机台上芯工位 → EM01)
  - binds_to            : hmi_component -[binds_to]-> plc_variable
                          (HMI 组件绑定 PLC 变量)
  - calls               : pou -[calls]-> fb_instance
                          (POU 调用 FB 实例)
  - instance_of         : fb_instance -[instance_of]-> fb_type/datatype
                          (FB 实例是某 FB 类型的实例)
  - input_param         : fb_instance -[input_param]-> plc_variable
                          (FB 实例的输入参数绑定)
  - output_param        : fb_instance -[output_param]-> plc_variable
                          (FB 实例的输出参数绑定)
  - command_of          : plc_variable -[command_of]-> physical_actuator
                          (变量是某执行机构的命令信号)
  - feedback_of         : plc_variable -[feedback_of]-> physical_actuator
                          (变量是某执行机构的反馈信号)
  - controls            : hmi_component -[controls]-> physical_actuator
                          (HMI 组件控制某执行机构)
  - used_by             : datatype -[used_by]-> plc_variable/pou
                          (dataType/FB类型 被某变量/POU 引用)

证据可信度 (confidence):
  - 'exact'             : 直接映射（如 EC 中 m_bOutput[N]→Y地址, HMI Y→中文）
  - 'structural'        : 结构推断（如 FB 参数 → m_bOutput[N] 通过 XML CLs）
  - 'contextual'        : 上下文推断（如 documentation "压紧1压紧" → actuator=压紧1, action=压紧）
  - 'llm_inferred'      : LLM 推断（最后手段）

不定义"HMI > documentation > comment" 的固定优先级。
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Entity:
    """实体 — 一个唯一的语义对象

    entity_id 格式: '{type}:{scope_path}:{key}'
      - scope_path 为空时退化为 '{type}:{key}' (全局实体，如全局变量)
      - scope_path 示例: 'pou:EM09_焊后除尘模组' / 'datatype:sRMOut'
      - 同名变量在不同作用域 = 不同实体（避免串线）

    key: 该实体在该 scope 下的标识（变量名、Y地址等）
    """
    entity_id: str            # 唯一 id
    entity_type: str          # 见上方 entity_type 枚举
    key: str                  # 该实体在该类型下的标识
    display_name: str = ''    # 可读名称
    scope: str = ''           # 作用域路径（如 'pou:EM09' / 'datatype:sRMOut'）

    def to_dict(self) -> dict:
        return {
            'entity_id': self.entity_id,
            'entity_type': self.entity_type,
            'key': self.key,
            'display_name': self.display_name,
            'scope': self.scope,
        }


@dataclass
class Relation:
    """关系 — 两个实体间的语义连接（有方向）"""
    source_id: str            # 源实体 id
    target_id: str            # 目标实体 id
    relation_type: str        # 见上方 relation_type 枚举
    confidence: str = 'structural'  # exact | structural | contextual | llm_inferred
    evidence_source: str = ''       # 证据来源描述（如 'plc_documentation', 'hmi_binary', 'smc2_ec'）
    evidence_detail: str = ''       # 具体证据（如 XML 行号/字段值）

    def to_dict(self) -> dict:
        return {
            'source_id': self.source_id,
            'target_id': self.target_id,
            'relation_type': self.relation_type,
            'confidence': self.confidence,
            'evidence_source': self.evidence_source,
            'evidence_detail': self.evidence_detail,
        }


@dataclass
class EvidenceStore:
    """证据存储 — 所有 Entity + Relation 的集合

    实体身份: entity_id = '{type}:{scope}:{key}' (scope 可空)
    同名变量在不同作用域 = 不同实体，避免串线。

    提供:
      - get_entity(entity_id)
      - find_entities(entity_type, key=None, scope=None)
      - find_by_key(entity_type, key) — 跨 scope 查找同 key 的所有实体
      - find_relations_from(entity_id, relation_type=None)
      - find_relations_to(entity_id, relation_type=None)
      - find_path(source_id, target_type) — BFS 找路径
    """
    entities: dict = field(default_factory=dict)  # {entity_id: Entity}
    relations: list = field(default_factory=list)  # [Relation]
    # 全局 key 索引: {(entity_type, key): [entity_id, ...]}
    # 用于跨 scope 查找同名实体
    _key_index: dict = field(default_factory=dict)

    def add_entity(self, entity: Entity) -> str:
        """添加实体（去重 by entity_id），返回 entity_id"""
        if entity.entity_id not in self.entities:
            self.entities[entity.entity_id] = entity
            # 更新 key 索引
            idx_key = (entity.entity_type, entity.key)
            if idx_key not in self._key_index:
                self._key_index[idx_key] = []
            if entity.entity_id not in self._key_index[idx_key]:
                self._key_index[idx_key].append(entity.entity_id)
        return entity.entity_id

    def add_relation(self, relation: Relation):
        """添加关系（不去重 — 同一对实体可有多种关系或多条证据）"""
        self.relations.append(relation)

    def get_entity(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

    def find_entities(self, entity_type: str, key: str = None,
                      scope: str = None) -> list:
        """按类型 + 可选 key/scope 查询实体"""
        results = []
        for e in self.entities.values():
            if e.entity_type != entity_type:
                continue
            if key is not None and e.key != key:
                continue
            if scope is not None and e.scope != scope:
                continue
            results.append(e)
        return results

    def find_by_key(self, entity_type: str, key: str) -> list:
        """跨 scope 查找同 key 的所有实体

        用于 ActionResolver 在不知道 scope 时找候选变量。
        返回所有同名实体，由 Resolver 选最佳。
        """
        idx_key = (entity_type, key)
        ids = self._key_index.get(idx_key, [])
        return [self.entities[eid] for eid in ids if eid in self.entities]

    def find_relations_from(self, entity_id: str,
                            relation_type: str = None) -> list:
        """从 entity 出发的关系"""
        results = []
        for r in self.relations:
            if r.source_id != entity_id:
                continue
            if relation_type and r.relation_type != relation_type:
                continue
            results.append(r)
        return results

    def find_relations_to(self, entity_id: str,
                          relation_type: str = None) -> list:
        """指向 entity 的关系"""
        results = []
        for r in self.relations:
            if r.target_id != entity_id:
                continue
            if relation_type and r.relation_type != relation_type:
                continue
            results.append(r)
        return results

    def find_path(self, source_id: str, target_entity_type: str,
                  max_depth: int = 5) -> list:
        """BFS 从 source_id 找到 target_entity_type 实体的最短路径

        Returns:
            [entity_id_1, relation_1, entity_id_2, relation_2, ..., target_entity_id]
            或 [] 表示无路径
        """
        if source_id not in self.entities:
            return []

        # BFS
        visited = {source_id}
        queue = [(source_id, [source_id])]

        while queue:
            current_id, path = queue.pop(0)
            current = self.entities.get(current_id)
            if not current:
                continue

            # 检查是否到达目标类型（除起点外）
            if current.entity_type == target_entity_type and len(path) > 1:
                return path

            if len(path) // 2 >= max_depth:
                continue

            # 扩展 — 既看 outgoing 也看 incoming（关系无向遍历）
            for r in self.relations:
                if r.source_id == current_id:
                    next_id = r.target_id
                elif r.target_id == current_id:
                    next_id = r.source_id
                else:
                    continue
                if next_id in visited:
                    continue
                visited.add(next_id)
                new_path = path + [r.to_dict(), next_id]
                queue.append((next_id, new_path))

        return []

    def stats(self) -> dict:
        """统计信息"""
        type_counts = {}
        for e in self.entities.values():
            type_counts[e.entity_type] = type_counts.get(e.entity_type, 0) + 1
        rel_counts = {}
        for r in self.relations:
            rel_counts[r.relation_type] = rel_counts.get(r.relation_type, 0) + 1
        return {
            'total_entities': len(self.entities),
            'total_relations': len(self.relations),
            'entities_by_type': type_counts,
            'relations_by_type': rel_counts,
        }

    def to_dict(self) -> dict:
        return {
            'entities': {eid: e.to_dict() for eid, e in self.entities.items()},
            'relations': [r.to_dict() for r in self.relations],
        }

    def save(self, path: str):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


# ============================================================
# 辅助函数 — 构造标准 entity_id
# ============================================================

def make_entity_id(entity_type: str, key: str, scope: str = '') -> str:
    """构造标准 entity_id

    Args:
        entity_type: 实体类型
        key: 实体在该 scope 下的标识
        scope: 作用域路径（如 'pou:EM09_焊后除尘模组' / 'datatype:sRMOut'）
               为空表示全局实体（如全局变量、物理地址、HMI 标签）

    Returns:
        'entity_type:scope:key' (有 scope) 或 'entity_type:key' (无 scope)
    """
    if scope:
        # scope 中的冒号替换为 / 避免与分隔符冲突
        safe_scope = scope.replace(':', '/')
        return f'{entity_type}:{safe_scope}:{key}'
    return f'{entity_type}:{key}'


def parse_entity_id(entity_id: str) -> tuple:
    """解析 entity_id → (entity_type, scope, key)

    Returns:
        (entity_type, scope, key) — scope 可能为空
    """
    parts = entity_id.split(':', 1)
    if len(parts) < 2:
        return (entity_id, '', '')
    entity_type = parts[0]
    rest = parts[1]
    # rest 可能是 'key' 或 'scope/key'
    if '/' in rest:
        # scope/key — 找最后一个 / 分割
        idx = rest.rfind('/')
        scope = rest[:idx].replace('/', ':')
        key = rest[idx + 1:]
    else:
        scope = ''
        key = rest
    return (entity_type, scope, key)


# ============================================================
# 辅助函数 — 从 JSON 加载 EvidenceStore
# ============================================================

def load_store(path: str) -> EvidenceStore:
    """从 JSON 文件加载 EvidenceStore。

    统一的 load_store 实现，所有工具应使用此函数。
    替代之前在 describe_steps.py / command_hypothesis.py /
    build_alias_hypotheses.py / build_semantic_spec.py 中的重复实现。
    """
    import json
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    store = EvidenceStore()
    for eid, e_dict in data.get('entities', {}).items():
        e = Entity(
            entity_id=e_dict['entity_id'],
            entity_type=e_dict['entity_type'],
            key=e_dict['key'],
            display_name=e_dict.get('display_name', ''),
            scope=e_dict.get('scope', ''),
        )
        store.entities[eid] = e
        idx_key = (e.entity_type, e.key)
        if idx_key not in store._key_index:
            store._key_index[idx_key] = []
        store._key_index[idx_key].append(eid)
    for r_dict in data.get('relations', []):
        store.relations.append(Relation(
            source_id=r_dict['source_id'],
            target_id=r_dict['target_id'],
            relation_type=r_dict['relation_type'],
            confidence=r_dict.get('confidence', 'structural'),
            evidence_source=r_dict.get('evidence_source', ''),
            evidence_detail=r_dict.get('evidence_detail', ''),
        ))
    return store
