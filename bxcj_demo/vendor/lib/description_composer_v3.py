#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/description_composer_v3.py — Description Composition V3 (Part E).

Priority order for description composition:
    1. validated engineering_actor (from ScopedDeclarationResolver / Relation 1)
    2. source-native step semantic (from CMT comments / StepSemanticEvidence)
    3. validated operation (from command pattern analysis)
    4. target/position (from evidence)
    5. program actor fallback (last resort)

Example transformations:
    EM02V.Cy_Up1 + Auto_V + documentation="刻码除尘避位气缸"
        → "刻码除尘避位气缸伸出"  (not "EM02V.Cy_Up1伸出")

    Mylar step 120, CMT="120-真空开启"
        → "真空开启"  (direct source-native semantic)

    EM12V.Axis_Z1 + no documentation
        → "EM12V.Axis_Z1移动"  (fallback to program actor)
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ============================================================
# Operation Extraction
# ============================================================

OPERATION_PATTERNS = {
    'Auto_V': '伸出',
    'Auto_R': '缩回',
    'Auto_Out': '伸出',
    'Auto_Back': '缩回',
    'Auto_Up': '上升',
    'Auto_Down': '下降',
    'Auto_Open': '打开',
    'Auto_Close': '关闭',
    'Auto_Clamp': '夹紧',
    'Auto_Unclamp': '松开',
    'Auto_Lock': '锁紧',
    'Auto_Unlock': '解锁',
    'Auto_Start': '启动',
    'Auto_Stop': '停止',
    'Auto_Forward': '前进',
    'Auto_Backward': '后退',
    'Clamp_Auto_V': '夹紧',
    'Clamp_Auto_R': '松开',
    'Press_Auto_V': '压紧',
    'Press_Auto_R': '松开',
    'Home_Auto': '回原点',
    'JogCW': '正转',
    'JogCCW': '反转',
    'Location_Auto': '移动',
}


def extract_operation(action_str: str) -> str:
    """Extract operation from action string.

    Args:
        action_str: e.g., "EM02V.Cy_Up1.In.Auto_V"

    Returns:
        Operation string, e.g., "伸出"
    """
    if not action_str:
        return ''

    for pattern, operation in OPERATION_PATTERNS.items():
        if pattern in action_str:
            return operation

    if 'Auto_V' in action_str or '.In.V' in action_str:
        return '动作'
    if 'Auto_R' in action_str or '.In.R' in action_str:
        return '复位'

    return ''


# ============================================================
# Description Quality V3 (Part F)
# ============================================================

DQ_STRONG = 'strong'
DQ_USABLE = 'usable'
DQ_WEAK = 'weak'
DQ_UNRESOLVED = 'unresolved'

VALID_DESCRIPTION_QUALITY = {DQ_STRONG, DQ_USABLE, DQ_WEAK, DQ_UNRESOLVED}


@dataclass
class DescriptionV3:
    """V3 description with evidence tracking.

    Fields:
        composed_text       — Final description text
        engineering_actor   — From Relation 1 (validated documentation)
        step_semantic       — From source-native CMT/StepDescription
        operation           — From command pattern
        program_actor       — Fallback actor name
        evidence_sources    — List of evidence sources used
        quality             — strong / usable / weak / unresolved
        quality_reason      — Why this quality was assigned
    """
    composed_text: str = ''
    engineering_actor: str = ''
    step_semantic: str = ''
    operation: str = ''
    program_actor: str = ''
    evidence_sources: list = field(default_factory=list)
    quality: str = DQ_UNRESOLVED
    quality_reason: str = ''

    def to_dict(self) -> dict:
        return {
            'composed_text': self.composed_text,
            'engineering_actor': self.engineering_actor,
            'step_semantic': self.step_semantic,
            'operation': self.operation,
            'program_actor': self.program_actor,
            'evidence_sources': self.evidence_sources,
            'quality': self.quality,
            'quality_reason': self.quality_reason,
        }


def classify_quality_v3(engineering_actor: str,
                        step_semantic: str,
                        operation: str,
                        operation_evidence: str = '',
                        program_actor: str = '') -> tuple:
    """Classify description quality (Part F redefinition).

    strong:
        - engineering_actor validated AND
        - operation validated/source-supported
    usable:
        - engineering_actor validated AND operation plausible
        OR
        - source-native Step description exact (step_semantic non-empty)
    weak:
        - only program_actor
        - naming inference
    unresolved:
        - key semantics all missing

    Returns:
        (quality, reason) tuple
    """
    has_eng_actor = bool(engineering_actor and engineering_actor.strip())
    has_step_semantic = bool(step_semantic and step_semantic.strip())
    has_operation = bool(operation and operation.strip())

    if has_step_semantic:
        return DQ_USABLE, 'source_native_step_semantic'

    if has_eng_actor and has_operation:
        if operation_evidence in ('validated', 'source_supported', 'pattern_matched'):
            return DQ_STRONG, 'engineering_actor_validated_and_operation_supported'
        return DQ_USABLE, 'engineering_actor_validated_and_operation_plausible'

    if has_eng_actor:
        return DQ_USABLE, 'engineering_actor_validated_operation_missing'

    if has_operation and program_actor:
        return DQ_WEAK, 'program_actor_with_operation'

    if program_actor:
        return DQ_WEAK, 'program_actor_only'

    return DQ_UNRESOLVED, 'no_evidence'


def compose_description_v3(
    engineering_actor: str = '',
    step_semantic: str = '',
    operation: str = '',
    program_actor: str = '',
    operation_evidence: str = '',
) -> DescriptionV3:
    """Compose description following V3 priority order.

    Priority:
        1. validated engineering_actor + operation
        2. source-native step semantic (CMT/StepDescription)
        3. program_actor + operation (fallback)

    Args:
        engineering_actor: From Relation 1 (e.g., "刻码除尘避位气缸")
        step_semantic: From CMT/StepDescription (e.g., "真空开启")
        operation: From command pattern (e.g., "伸出")
        program_actor: Fallback (e.g., "EM02V.Cy_Up1")
        operation_evidence: Evidence level for operation

    Returns:
        DescriptionV3 object
    """
    desc = DescriptionV3(
        engineering_actor=engineering_actor,
        step_semantic=step_semantic,
        operation=operation,
        program_actor=program_actor,
    )

    parts = []
    sources = []

    if step_semantic and step_semantic.strip():
        desc.composed_text = step_semantic.strip()
        sources.append('source_native_step_semantic')
        desc.evidence_sources = sources
        desc.quality, desc.quality_reason = classify_quality_v3(
            engineering_actor, step_semantic, operation,
            operation_evidence, program_actor)
        return desc

    if engineering_actor and engineering_actor.strip():
        parts.append(engineering_actor.strip())
        sources.append('relation1_documentation')
        if operation and operation.strip():
            parts.append(operation.strip())
            sources.append('operation_pattern')
        desc.composed_text = ''.join(parts)
        desc.evidence_sources = sources
        desc.quality, desc.quality_reason = classify_quality_v3(
            engineering_actor, step_semantic, operation,
            operation_evidence, program_actor)
        return desc

    if program_actor and program_actor.strip():
        actor_display = program_actor.strip()
        parts.append(actor_display)
        sources.append('program_actor_fallback')
        if operation and operation.strip():
            parts.append(operation.strip())
            sources.append('operation_pattern')
        desc.composed_text = ''.join(parts)
        desc.evidence_sources = sources
        desc.quality, desc.quality_reason = classify_quality_v3(
            engineering_actor, step_semantic, operation,
            operation_evidence, program_actor)
        return desc

    desc.composed_text = ''
    desc.evidence_sources = []
    desc.quality, desc.quality_reason = classify_quality_v3(
        '', '', '', '', '')
    return desc


def compose_composite_description_v3(descriptions: list) -> str:
    """Compose composite description from multiple V3 descriptions.

    All actions are listed, separated by '，'.
    No "等N个动作" abbreviation.
    """
    texts = [d.composed_text for d in descriptions if d.composed_text]
    if not texts:
        return ''
    if len(texts) == 1:
        return texts[0]
    return '，'.join(texts)


def classify_composite_quality_v3(descriptions: list) -> tuple:
    """Classify quality of a composite description.

    The composite quality is the WEAKEST of its members.
    """
    if not descriptions:
        return DQ_UNRESOLVED, 'no_descriptions'

    qualities = [d.quality for d in descriptions if d.composed_text]
    if not qualities:
        return DQ_UNRESOLVED, 'all_empty'

    quality_order = {DQ_STRONG: 3, DQ_USABLE: 2, DQ_WEAK: 1, DQ_UNRESOLVED: 0}
    weakest = min(qualities, key=lambda q: quality_order.get(q, 0))

    counts = {}
    for q in qualities:
        counts[q] = counts.get(q, 0) + 1

    return weakest, 'weakest_of_{}_descriptions: {}'.format(len(qualities), counts)
