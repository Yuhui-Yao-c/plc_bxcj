#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/evidence_extractors/ec_extractor.py — EC (Element Comment) Evidence Extractor

从 DecodedProgram.ec_records (ECRecord list) 创建 Entity + Relation。

职责:
  - 将 ECRecord 转成 verified relations
  - 创建 maps_to: PLC variable[index] → physical_address
  - 创建 documented_as: PLC variable[index] → description text
  - 保留 source evidence

不做:
  - 重新解析 SMC2 XML (Decoder 已完成)
  - 判断 Step / actor / operation 语义
  - 创建 FB parameter binding relations

创建的实体类型:
  - plc_variable (owner_symbol[index])
  - physical_address (Y/X address)
  - documentation_text (description)

创建的关系类型:
  - maps_to (plc_variable → physical_address, confidence=exact)
  - documented_as (plc_variable → documentation_text, confidence=exact)
"""
from __future__ import annotations

from ..evidence_model import Entity, Relation, EvidenceStore, make_entity_id
from ..canonical_ir import DecodedProgram, ECRecord


class ECEvidenceExtractor:
    """ECRecord list → Entity + Relation

    Generic extractor — no project-specific logic.
    Works with any DecodedProgram that has ec_records populated.
    """

    def extract(self, program: DecodedProgram,
                store: EvidenceStore = None) -> EvidenceStore:
        """Extract EC evidence from DecodedProgram.

        Args:
            program: DecodedProgram with ec_records populated
            store: Existing EvidenceStore to add to (creates new if None)

        Returns:
            EvidenceStore with EC entities and relations added
        """
        if store is None:
            store = EvidenceStore()

        for ecr in program.ec_records:
            if not ecr.owner_symbol or ecr.index < 0:
                continue
            self._extract_one(ecr, store)

        return store

    def _extract_one(self, ecr: ECRecord, store: EvidenceStore):
        """Extract entities and relations from a single ECRecord."""
        var_key = '{}[{}]'.format(ecr.owner_symbol, ecr.index)
        var_entity_id = make_entity_id('plc_variable', var_key)

        var_entity = Entity(
            entity_id=var_entity_id,
            entity_type='plc_variable',
            key=var_key,
            display_name=var_key,
        )
        store.add_entity(var_entity)

        source_detail = 'slwd:EC owner={} index={}'.format(
            ecr.owner_symbol, ecr.index)

        # maps_to: plc_variable → physical_address
        if ecr.address:
            addr_entity_id = make_entity_id('physical_address', ecr.address)
            addr_entity = Entity(
                entity_id=addr_entity_id,
                entity_type='physical_address',
                key=ecr.address,
                display_name=ecr.address,
            )
            store.add_entity(addr_entity)

            store.add_relation(Relation(
                source_id=var_entity_id,
                target_id=addr_entity_id,
                relation_type='maps_to',
                confidence='exact',
                evidence_source='smc2_ec',
                evidence_detail=source_detail,
            ))

        # documented_as: plc_variable → description text
        if ecr.description and ecr.description not in ('N/A', '#N/A', ''):
            doc_entity_id = make_entity_id('documentation_text', ecr.description)
            doc_entity = Entity(
                entity_id=doc_entity_id,
                entity_type='documentation_text',
                key=ecr.description[:200],
                display_name=ecr.description,
            )
            store.add_entity(doc_entity)

            store.add_relation(Relation(
                source_id=var_entity_id,
                target_id=doc_entity_id,
                relation_type='documented_as',
                confidence='exact',
                evidence_source='smc2_ec',
                evidence_detail=source_detail,
            ))
