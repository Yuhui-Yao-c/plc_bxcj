#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/evidence_levels.py — Evidence 等级定义

5 级 evidence level (严格区分来源强度):

  exact_source_fact
    直接来自确定源码或 HMI 事实。
    例如: command member documentation 明确写"下压气缸上升"
         HMI exact address label
         PLC documentation exact actuator 名称
    特征: 换一台设备仍然成立 (源码/HMI 本身就是这样写的)

  validated_structural_semantic
    不是源码直接文字, 但存在独立正证据验证。
    要求:
      - 有 supporting samples (consistent > 0)
      - supporting evidence 与 hypothesis discovery 不完全同源
      - 无明显 conflict
      - 跨多个 scope/station 稳定 (hold-out 验证通过)
    特征: 换一台设备需要重新发现和验证该规律

  plausible_structural_hypothesis
    结构上合理, 但缺少独立正证据。
    典型: consistent=0, conflict=0, unverifiable=N
    禁止将这类语义称为 validated。

  contextual_semantic
    依赖 station context / comment / surrounding step / naming convention 得到。
    例如: 从 command member 名字 _R 推断 direction=retract
    特征: 弱于 plausible, 依赖上下文

  unresolved
    没有足够证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Evidence level constants
EXACT_SOURCE_FACT = 'exact_source_fact'
VALIDATED_STRUCTURAL_SEMANTIC = 'validated_structural_semantic'
PLAUSIBLE_STRUCTURAL_HYPOTHESIS = 'plausible_structural_hypothesis'
CONTEXTUAL_SEMANTIC = 'contextual_semantic'
UNRESOLVED = 'unresolved'

# Ordered by strength (strongest first)
EVIDENCE_LEVEL_ORDER = [
    EXACT_SOURCE_FACT,
    VALIDATED_STRUCTURAL_SEMANTIC,
    PLAUSIBLE_STRUCTURAL_HYPOTHESIS,
    CONTEXTUAL_SEMANTIC,
    UNRESOLVED,
]


def is_stronger_or_equal(level_a: str, level_b: str) -> bool:
    """Check if level_a is stronger than or equal to level_b"""
    if level_a not in EVIDENCE_LEVEL_ORDER or level_b not in EVIDENCE_LEVEL_ORDER:
        return False
    return EVIDENCE_LEVEL_ORDER.index(level_a) <= EVIDENCE_LEVEL_ORDER.index(level_b)


def stronger_level(level_a: str, level_b: str) -> str:
    """Return the stronger of two evidence levels"""
    if is_stronger_or_equal(level_a, level_b):
        return level_a
    return level_b


def weakest_level(level_a: str, level_b: str) -> str:
    """Return the weaker of two evidence levels (for chain confidence)"""
    if not is_stronger_or_equal(level_a, level_b):
        return level_a
    return level_b


@dataclass
class FieldEvidenceV2:
    """单个字段的证据 (v2 — 带 evidence_level)

    每个字段必须回答:
      - value: 值是什么
      - evidence_level: 这个值有多强的证据 (5 级)
      - evidence: 具体证据列表
      - resolution_method: 通过什么方法得到
      - validator_result: 验证结果 (如果有)
      - chain_weakest_link: 如果该字段依赖推理链, 记录链中最弱环节
    """
    value: Any = None
    resolved: bool = False
    evidence_level: str = UNRESOLVED
    evidence: list = field(default_factory=list)
    resolution_method: str = ''
    validator_result: dict = field(default_factory=dict)
    chain_weakest_link: str = ''  # 记录推理链中最弱环节的 evidence_level

    def set_exact(self, value: Any, evidence: list, method: str, validator_result: dict = None):
        """Set as exact_source_fact"""
        self.value = value
        self.resolved = True
        self.evidence_level = EXACT_SOURCE_FACT
        self.evidence = evidence
        self.resolution_method = method
        self.validator_result = validator_result or {}

    def set_validated(self, value: Any, evidence: list, method: str, validator_result: dict = None):
        """Set as validated_structural_semantic"""
        self.value = value
        self.resolved = True
        self.evidence_level = VALIDATED_STRUCTURAL_SEMANTIC
        self.evidence = evidence
        self.resolution_method = method
        self.validator_result = validator_result or {}

    def set_plausible(self, value: Any, evidence: list, method: str, validator_result: dict = None):
        """Set as plausible_structural_hypothesis"""
        self.value = value
        self.resolved = True
        self.evidence_level = PLAUSIBLE_STRUCTURAL_HYPOTHESIS
        self.evidence = evidence
        self.resolution_method = method
        self.validator_result = validator_result or {}

    def set_contextual(self, value: Any, evidence: list, method: str, validator_result: dict = None):
        """Set as contextual_semantic"""
        self.value = value
        self.resolved = True
        self.evidence_level = CONTEXTUAL_SEMANTIC
        self.evidence = evidence
        self.resolution_method = method
        self.validator_result = validator_result or {}

    def set_unresolved(self):
        """Set as unresolved"""
        self.resolved = False
        self.evidence_level = UNRESOLVED

    def apply_weakest_link(self, other_level: str, link_description: str = ''):
        """Apply weakest-link rule: downgrade evidence_level if chain has weaker link.

        chain confidence cannot exceed weakest semantic link.
        """
        if other_level == UNRESOLVED or not other_level:
            return
        if not is_stronger_or_equal(self.evidence_level, other_level):
            # other_level is weaker — downgrade
            self.evidence_level = other_level
            if link_description:
                self.chain_weakest_link = link_description

    def to_dict(self) -> dict:
        return {
            'value': self.value,
            'resolved': self.resolved,
            'evidence_level': self.evidence_level,
            'evidence': self.evidence,
            'resolution_method': self.resolution_method,
            'validator_result': self.validator_result,
            'chain_weakest_link': self.chain_weakest_link,
        }


# Evidence level → description mapping for reporting
EVIDENCE_LEVEL_DESCRIPTIONS = {
    EXACT_SOURCE_FACT: '直接来自源码/HMI事实 (换设备仍成立)',
    VALIDATED_STRUCTURAL_SEMANTIC: '结构规律+独立正证据+hold-out验证 (换设备需重新发现)',
    PLAUSIBLE_STRUCTURAL_HYPOTHESIS: '结构合理但缺独立正证据 (consistent=0, conflict=0)',
    CONTEXTUAL_SEMANTIC: '上下文/命名约定推断 (弱于plausible)',
    UNRESOLVED: '证据不足',
}
