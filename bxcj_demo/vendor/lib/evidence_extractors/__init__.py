#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/evidence_extractors/__init__.py — Evidence Extractor package

Evidence Extractor 职责:
  structured records / program IR → Entity + Relation

Evidence Extractor 不做:
  - 重新解析整个原始文件格式 (那是 Decoder 的事)
  - 决定下一步调用什么工具

可用 Extractor:
  - PLCopenEvidenceExtractor : DecodedProgram → Entity + Relation
  - HMIEvidenceExtractor     : HMI records → Entity + Relation
"""
from .plcopen_extractor import PLCopenEvidenceExtractor
from .hmi_extractor import HMIEvidenceExtractor

__all__ = ['PLCopenEvidenceExtractor', 'HMIEvidenceExtractor']
