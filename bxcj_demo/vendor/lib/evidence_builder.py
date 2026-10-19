#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/evidence_builder.py — Unified evidence store builder

使用 Decoder + Evidence Extractor 架构构建 EvidenceStore:

  raw resource → Decoder → structured records / Canonical IR
                                ↓
                    Evidence Extractor → Entity + Relation

替代旧的 lib/extractors.py build_evidence_store()。
旧函数保留为兼容入口，新代码应使用本模块。
"""
from __future__ import annotations

from pathlib import Path

from .evidence_model import EvidenceStore
from .decoders import PLCopenDecoder, SMC2Decoder, HMIDecoder
from .evidence_extractors import PLCopenEvidenceExtractor, HMIEvidenceExtractor


def _is_smc2_xml(path: Path) -> bool:
    """Detect SMC2 format by header sniffing."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(4096)
    except Exception:
        return False
    return '<smc2' in head.lower() or ('type="json"' in head and '<group' in head)


def build_evidence_store(plc_path: str, hmi_path: str | None = None,
                          extractor_config: dict = None) -> EvidenceStore:
    """从 PLC 源码 (+ 可选 HMI) 构建 EvidenceStore。

    Args:
        plc_path: PLC 源码路径 (文件或目录)
        hmi_path: HMI 路径 (可选, 文件或目录)
        extractor_config: Evidence Extractor 配置 (station patterns 等)

    Returns:
        EvidenceStore
    """
    store = EvidenceStore()
    extractor_config = extractor_config or {}

    # Resolve PLC path
    plc = Path(plc_path) if plc_path else None
    if plc and plc.is_dir():
        xmls = sorted(plc.rglob('*.xml'))
        plc = xmls[0] if xmls else None

    if plc and plc.is_file():
        if _is_smc2_xml(plc):
            _build_smc2(str(plc), store, extractor_config)
        else:
            _build_plcopen(str(plc), store, extractor_config)

    if hmi_path:
        hmi_file = _resolve_hmi_path(hmi_path)
        if hmi_file:
            _build_hmi(hmi_file, store, extractor_config)

    return store


def _build_plcopen(xml_path: str, store: EvidenceStore,
                    config: dict):
    """PLCopen XML → Decoder → Evidence Extractor → Store"""
    decoder = PLCopenDecoder()
    decoded = decoder.decode(xml_path)

    extractor = PLCopenEvidenceExtractor(config=config)
    extractor.extract(decoded, store)


def _build_hmi(hmi_path: str, store: EvidenceStore, config: dict):
    """HMI binary → Decoder → Evidence Extractor → Store"""
    decoder = HMIDecoder()
    hmi_records = decoder.decode(hmi_path)

    extractor = HMIEvidenceExtractor(config=config)
    extractor.extract(hmi_records, store)


def _build_smc2(xml_path: str, store: EvidenceStore, config: dict):
    """SMC2 CDATA JSON → Decoder → Evidence Extractor → Store.

    Uses SMC2Decoder to produce Canonical IR, then PLCopenEvidenceExtractor
    (which works on DecodedProgram regardless of source format).
    """
    decoder = SMC2Decoder()
    decoded = decoder.decode(xml_path)

    # Reuse PLCopenEvidenceExtractor — it consumes DecodedProgram,
    # which is format-agnostic. Station patterns may differ; config
    # controls the patterns.
    extractor = PLCopenEvidenceExtractor(config=config)
    extractor.extract(decoded, store)


def _resolve_hmi_path(hmi_path: str) -> str | None:
    """Resolve HMI path (file or directory → find MT8000 project)."""
    p = Path(hmi_path)
    if p.is_file():
        return str(p)
    if p.is_dir():
        return HMIDecoder.find_hmi_project(hmi_path)
    return None
