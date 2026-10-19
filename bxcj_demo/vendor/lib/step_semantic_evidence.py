#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/step_semantic_evidence.py — Mylar CMT 注释解析器 (源生步序语义证据)

从 steps_raw.json 的 comment 字段中提取源生 (source-native) 步序语义证据。
这些注释直接来自 PLC 源码的 CMT 字段, 不是 LLM 推断的。

注释格式 (Mylar 风格):
  - "N-text"       : 如 "120-真空开启", "10-记忆判断"        → n_text
  - "步N-text"     : 如 "步170-复位测试按钮"                  → bu_text
  - "===名称==="   : 段落分隔符, 非步序语义                    → section
  - 纯文本         : 如 "多张检测开启..."                      → plain_text
  - 多行           : 如 "10-记忆判断\\r\\n100 取料完成启用"
                     解析首行, 其余行作为 additional_context
  - 空字符串       : 跳过                                     → empty

Evidence level = "exact": 注释是源码 CMT 字段的直接拷贝, 换一台设备仍成立。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ============================================================
# 数据模型
# ============================================================

@dataclass
class StepSemanticEvidence:
    """单条步序的源生语义证据 (来自 CMT comment 字段)。

    Fields:
        state_domain        : 状态变量名 (如 "m_nNextAutoStep")
        thread              : 线程/通道号 (int)
        step_value          : 步序值 (int)
        source_comment      : 原始注释全文 (未修改)
        parsed_step_number  : 从注释中解析出的步号 (int | None)
                              n_text/bu_text 有值; plain_text 为 None
        semantic_text       : 清洗后的语义文本 (不含步号前缀)
        evidence_level      : "exact" (源生注释)
        source_location     : 来源定位串 (如 "steps_raw.json[thread=29/step=120]")
    """
    state_domain: str
    thread: int
    step_value: int
    source_comment: str
    parsed_step_number: Optional[int]
    semantic_text: str
    evidence_level: str
    source_location: str

    def to_dict(self) -> dict:
        return {
            'state_domain': self.state_domain,
            'thread': self.thread,
            'step_value': self.step_value,
            'source_comment': self.source_comment,
            'parsed_step_number': self.parsed_step_number,
            'semantic_text': self.semantic_text,
            'evidence_level': self.evidence_level,
            'source_location': self.source_location,
        }


# ============================================================
# CMT 注释解析器
# ============================================================

class CMTCommentParser:
    """Mylar 风格 CMT 注释解析器。

    将 steps_raw.json 中每条 comment 解析为结构化语义信息。
    解析是确定性的 (纯正则 + 字符串处理), 不涉及 LLM 推断。
    """

    # "N-text" : 数字 + 可选空格 + 半角/全角连字符 + 可选空格 + 文本
    N_TEXT_PATTERN = re.compile(r'^(\d+)\s*[-—]\s*(.+)$')

    # "步N-text" : 步前缀 + 数字 + 可选空格 + 半角/全角连字符 + 可选空格 + 文本
    BU_TEXT_PATTERN = re.compile(r'^步(\d+)\s*[-—]\s*(.+)$')

    # 段落分隔符 : 如 "===取料热熔流程==="
    SECTION_PATTERN = re.compile(r'^=+.+=+$')

    # source_steps 引用串 : "thread=29/step=110"
    _STEP_REF_PATTERN = re.compile(r'thread=(\d+)/step=(\d+)')

    # ----------------------------------------------------------
    # 单条注释解析
    # ----------------------------------------------------------
    def parse_comment(self, comment: str) -> dict:
        """解析单条 CMT 注释。

        Returns dict with:
            parsed_step_number : int | None  — 解析出的步号
            semantic_text      : str         — 清洗后的语义文本
            comment_type       : str         — n_text / bu_text / section
                                                / plain_text / empty
            is_valid           : bool        — semantic_text 非空且有意义时为 True
            additional_context : list[str]   — 多行注释中首行之后的附加上下文
            section_name       : str | None  — 段落分隔符的内部名称 (仅 section 类型)
        """
        result = {
            'parsed_step_number': None,
            'semantic_text': '',
            'comment_type': 'empty',
            'is_valid': False,
            'additional_context': [],
            'section_name': None,
        }

        if comment is None:
            return result
        raw = comment if isinstance(comment, str) else str(comment)
        if not raw.strip():
            return result

        # 拆行: 兼容 \r\n / \n / \r
        lines = re.split(r'\r\n|\r|\n', raw)
        lines = [ln for ln in lines]
        first_line = lines[0].strip() if lines else ''
        additional = [ln.strip() for ln in lines[1:] if ln.strip()]

        # 1) 段落分隔符
        if self.SECTION_PATTERN.match(first_line):
            inner = first_line.strip('=').strip()
            result['comment_type'] = 'section'
            result['semantic_text'] = inner
            result['section_name'] = inner
            result['additional_context'] = additional
            result['is_valid'] = False  # 分隔符不是步序语义
            return result

        # 2) "步N-text"
        m = self.BU_TEXT_PATTERN.match(first_line)
        if m:
            text = self._clean_text(m.group(2))
            result['comment_type'] = 'bu_text'
            result['parsed_step_number'] = int(m.group(1))
            result['semantic_text'] = text
            result['additional_context'] = additional
            result['is_valid'] = bool(text)
            return result

        # 3) "N-text"
        m = self.N_TEXT_PATTERN.match(first_line)
        if m:
            text = self._clean_text(m.group(2))
            result['comment_type'] = 'n_text'
            result['parsed_step_number'] = int(m.group(1))
            result['semantic_text'] = text
            result['additional_context'] = additional
            result['is_valid'] = bool(text)
            return result

        # 4) 纯文本 — semantic_text 为清洗后的全文
        text = self._clean_text(raw)
        result['comment_type'] = 'plain_text'
        result['semantic_text'] = text
        result['additional_context'] = []  # 全文已在 semantic_text 中
        result['is_valid'] = bool(text)
        return result

    # ----------------------------------------------------------
    # 批量解析 steps_raw
    # ----------------------------------------------------------
    def parse_steps(self, steps_raw: list) -> list[StepSemanticEvidence]:
        """解析 steps_raw.json 中的全部步序。

        Returns:
            list[StepSemanticEvidence] — 仅包含 is_valid=True 的步序
            (empty / section 被跳过)
        """
        results: list[StepSemanticEvidence] = []
        for step in steps_raw:
            if not isinstance(step, dict):
                continue
            comment = step.get('comment', '') or ''
            parsed = self.parse_comment(comment)
            if not parsed['is_valid']:
                continue

            thread = step.get('thread_idx')
            step_value = step.get('step_value')
            state_domain = step.get('state_variable', '')
            if thread is None or step_value is None:
                continue

            evidence = StepSemanticEvidence(
                state_domain=state_domain,
                thread=int(thread),
                step_value=int(step_value),
                source_comment=comment,
                parsed_step_number=parsed['parsed_step_number'],
                semantic_text=parsed['semantic_text'],
                evidence_level='exact',
                source_location=(
                    'steps_raw.json[thread={}/step={}]'.format(
                        thread, step_value)
                ),
            )
            results.append(evidence)
        return results

    # ----------------------------------------------------------
    # 内部工具
    # ----------------------------------------------------------
    @staticmethod
    def _clean_text(text: str) -> str:
        """清洗语义文本: 去除首尾空白, 将内部换行/回车转为单个空格, 合并连续空白。"""
        if not text:
            return ''
        # 换行/回车 → 空格
        cleaned = re.sub(r'[\r\n]+', ' ', text)
        # 合并连续空白
        cleaned = re.sub(r'\s+', ' ', cleaned)
        return cleaned.strip()


# ============================================================
# 查找表构建
# ============================================================

def build_step_semantic_lookup(steps_raw_path: str) -> dict:
    """构建查找表: (thread, step_value) → StepSemanticEvidence。

    仅包含有有效语义文本 (is_valid=True) 的步序。

    Args:
        steps_raw_path: steps_raw.json 文件路径

    Returns:
        dict : {(thread:int, step_value:int): StepSemanticEvidence}
    """
    with open(steps_raw_path, 'r', encoding='utf-8') as f:
        steps_raw = json.load(f)

    parser = CMTCommentParser()
    evidences = parser.parse_steps(steps_raw)

    lookup: dict = {}
    for ev in evidences:
        key = (ev.thread, ev.step_value)
        lookup[key] = ev
    return lookup


# ============================================================
# 与 FinalTimingRow 集成
# ============================================================

def _get_source_steps(row) -> list:
    """从 FinalTimingRowFact 对象或 dict 中取 source_steps。"""
    if hasattr(row, 'source_steps'):
        return getattr(row, 'source_steps', None) or []
    if isinstance(row, dict):
        return row.get('source_steps', []) or []
    return []


def get_step_semantic_for_row(row, lookup: dict) -> Optional[StepSemanticEvidence]:
    """为一条 FinalTimingRow 取得其源生步序语义证据。

    通过 row.source_steps 中的 "thread=N/step=M" 引用串查找。
    返回第一条命中的证据; 若全部未命中则返回 None。

    Args:
        row    : FinalTimingRowFact 对象 或 dict (含 source_steps 字段)
        lookup : build_step_semantic_lookup() 返回的查找表

    Returns:
        StepSemanticEvidence | None
    """
    source_steps = _get_source_steps(row)
    parser = CMTCommentParser()
    for ref in source_steps:
        if not isinstance(ref, str):
            continue
        m = parser._STEP_REF_PATTERN.search(ref)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)))
        ev = lookup.get(key)
        if ev is not None:
            return ev
    return None


# ============================================================
# 序列化辅助
# ============================================================

def save_evidence_json(evidences: list[StepSemanticEvidence], path: str,
                       metadata: dict = None):
    """将证据列表保存为 JSON 文件。

    Args:
        evidences : StepSemanticEvidence 列表
        path      : 输出文件路径
        metadata  : 可选的元信息 dict (写入 "metadata" 字段)
    """
    out = {
        'metadata': metadata or {},
        'evidence': [ev.to_dict() for ev in evidences],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
