#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/evidence_extractors/cmt_actor_extractor.py — CMT-Actor Evidence Extractor

Extracts evidence relations from CMT (rung comments) that reference
array-indexed variables like Vacuum[N] and Cylinder[N].

This is the formal integration of the probe-discovered CMT mapping
capability into the Evidence Extractor framework.

Responsibilities:
    - Scan DecodedProgram Comment elements for actor variable references
    - Create documented_as relations: Vacuum[N] → CMT text
    - Distinguish write context (coil/assignment) from read context
    - Record association quality (exact_direct vs context_supported)

Does NOT:
    - Hardcode specific Vacuum/Cylinder indices or names
    - Make project-specific semantic judgments
    - Create Step/actor/operation relations

Created entity types:
    - plc_variable (Vacuum[N], Cylinder[N], etc.)
    - documentation_text (CMT text)

Created relation types:
    - documented_as (plc_variable → documentation_text)
    - cmt_referenced_in (plc_variable → pou/rung context)

The extractor is generic — it works with any array-indexed variable
pattern, not just Vacuum/Cylinder. The pattern is configurable via
the extractor_config parameter.
"""
from __future__ import annotations

import re
from typing import List, Dict, Any
from dataclasses import dataclass, field

from ..evidence_model import Entity, Relation, EvidenceStore, make_entity_id
from ..canonical_ir import DecodedProgram, Comment, Assignment, VariableRef


@dataclass
class CMTActorEvidence:
    """A single CMT-actor association evidence record."""

    pou_name: str = ''
    network_id: str = ''
    cmt_text: str = ''
    actor_type: str = ''        # e.g. 'Vacuum', 'Cylinder'
    actor_index: int = -1
    is_write: bool = False      # True if actor is a coil/assignment target
    is_read: bool = False       # True if actor is a contact/read
    variable_ref: str = ''      # Full variable reference (e.g. "Vacuum[19].HMIStrOut")
    association_quality: str = ''  # exact_direct, context_supported, ambiguous


class CMTActorExtractor:
    """Extract CMT-actor evidence from DecodedProgram.

    Generic extractor — works with any array-indexed variable pattern.
    The actor patterns are configurable, not hardcoded.

    Config parameters (in extractor_config):
        actor_patterns: list of regex patterns for actor variable names
                        Default: [r'Vacuum\\[(\\d+)\\]', r'Cylinder\\[(\\d+)\\]']
        min_cmt_length: minimum CMT text length to process (default: 2)
        max_cmt_length: maximum CMT text length (default: 200)
    """

    DEFAULT_ACTOR_PATTERNS = [
        r'Vacuum\[(\d+)\]',
        r'Cylinder\[(\d+)\]',
    ]

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.actor_patterns = self.config.get(
            'actor_patterns', self.DEFAULT_ACTOR_PATTERNS)
        self.min_cmt_length = self.config.get('min_cmt_length', 2)
        self.max_cmt_length = self.config.get('max_cmt_length', 200)

    def extract(self, program: DecodedProgram,
                store: EvidenceStore = None) -> EvidenceStore:
        """Extract CMT-actor evidence from DecodedProgram.

        Args:
            program: DecodedProgram with Comment elements and
                     Assignment/VariableRef elements in program units
            store: Existing EvidenceStore to add to

        Returns:
            EvidenceStore with CMT-actor entities and relations
        """
        if store is None:
            store = EvidenceStore()

        for unit in program.program_units:
            self._extract_from_unit(unit, store)

        return store

    def _extract_from_unit(self, unit, store: EvidenceStore):
        """Extract CMT-actor evidence from a single ProgramUnit.

        Strategy:
            1. Build a map: network_id -> Comment text
            2. Build a map: network_id -> [(var_name, is_write)]
               by scanning all elements for VariableRef in the same network
            3. For each network with a Comment, check if any variable
               references match actor patterns
            4. Create documented_as relation between the actor variable
               and the CMT text

        The key insight: CMT text itself may NOT contain "Vacuum[19]".
        Instead, Vacuum[19] appears in the LD/ST elements of the same
        rung. We need to associate the CMT with all variable references
        in the same rung.
        """
        # Build network_id -> Comment text
        comments_by_network = {}

        # Build network_id -> list of (var_name, is_write)
        # We scan all elements and extract variable references
        vars_by_network = {}  # network_id -> [(var_name, is_write)]

        for elem in unit.elements:
            network_id = getattr(elem, 'network_id', '')
            if not network_id:
                continue

            if isinstance(elem, Comment):
                text = elem.text or ''
                if (len(text) >= self.min_cmt_length
                        and len(text) <= self.max_cmt_length):
                    comments_by_network[network_id] = text
                continue

            # Extract variable references from this element
            # Assignment: target is a write
            if isinstance(elem, Assignment):
                target = elem.target
                if target and hasattr(target, 'name') and target.name:
                    vars_by_network.setdefault(network_id, []).append(
                        (target.name, True))
                # Source can be a VariableRef (read)
                source = elem.source
                if source and hasattr(source, 'name') and source.name:
                    vars_by_network.setdefault(network_id, []).append(
                        (source.name, False))

            # Condition: operands are reads
            if hasattr(elem, 'operands') and elem.operands:
                for op in elem.operands:
                    if hasattr(op, 'name') and op.name:
                        vars_by_network.setdefault(network_id, []).append(
                            (op.name, False))

            # Call: inputs are reads, outputs are writes
            if hasattr(elem, 'inputs') and elem.inputs:
                for inp in elem.inputs:
                    if hasattr(inp, 'name') and inp.name:
                        vars_by_network.setdefault(network_id, []).append(
                            (inp.name, False))
            if hasattr(elem, 'outputs') and elem.outputs:
                for outp in elem.outputs:
                    if hasattr(outp, 'name') and outp.name:
                        vars_by_network.setdefault(network_id, []).append(
                            (outp.name, True))

            # VariableRef standalone (contact in ladder)
            if isinstance(elem, VariableRef):
                if elem.name:
                    vars_by_network.setdefault(network_id, []).append(
                        (elem.name, elem.negated if hasattr(elem, 'negated') else False))

        # Process each network with a comment
        for network_id, cmt_text in comments_by_network.items():
            network_vars = vars_by_network.get(network_id, [])

            # Find actor references in the variables of this network
            actor_refs = set()
            write_actors = set()
            read_actors = set()

            for var_name, is_write in network_vars:
                for pattern in self.actor_patterns:
                    m = re.search(pattern, var_name)
                    if m:
                        actor_type = pattern.split(r'\[')[0].replace('\\', '')
                        actor_idx = int(m.group(1))
                        actor_refs.add((actor_type, actor_idx))
                        if is_write:
                            write_actors.add((actor_type, actor_idx))
                        else:
                            read_actors.add((actor_type, actor_idx))

            if not actor_refs:
                continue

            # Create evidence for each actor
            for actor_type, actor_idx in actor_refs:
                is_write = (actor_type, actor_idx) in write_actors
                is_read = (actor_type, actor_idx) in read_actors

                quality = 'exact_direct' if is_write else (
                    'context_supported' if is_read else 'ambiguous')

                # Create variable entity
                var_key = '{}[{}]'.format(actor_type, actor_idx)
                var_entity_id = make_entity_id('plc_variable', var_key)

                var_entity = Entity(
                    entity_id=var_entity_id,
                    entity_type='plc_variable',
                    key=var_key,
                    display_name=var_key,
                )
                store.add_entity(var_entity)

                # Create documentation entity
                doc_key = cmt_text[:200]
                doc_entity_id = make_entity_id('documentation_text', doc_key)

                doc_entity = Entity(
                    entity_id=doc_entity_id,
                    entity_type='documentation_text',
                    key=doc_key,
                    display_name=cmt_text[:200],
                )
                store.add_entity(doc_entity)

                # Create documented_as relation
                source_detail = 'cmt_rung: pou={} network={} quality={} is_write={}'.format(
                    unit.name, network_id, quality, is_write)

                store.add_relation(Relation(
                    source_id=var_entity_id,
                    target_id=doc_entity_id,
                    relation_type='documented_as',
                    confidence='exact' if quality == 'exact_direct' else 'structural',
                    evidence_source='cmt_actor_extractor',
                    evidence_detail=source_detail,
                ))

    def _process_comment(self, cmt_text: str, network_id: str,
                         pou_name: str,
                         writes: List, reads: List,
                         store: EvidenceStore):
        """Process a single CMT comment for actor references."""

        # Find all actor references in the CMT text
        actor_refs = []
        for pattern in self.actor_patterns:
            for m in re.finditer(pattern, cmt_text):
                actor_type = pattern.split(r'\[')[0]
                actor_refs.append((actor_type, int(m.group(1))))

        if not actor_refs:
            return

        # Also check write/read variable lists for actor references
        write_actors = set()
        for var_name, _ in writes:
            for pattern in self.actor_patterns:
                m = re.search(pattern, var_name)
                if m:
                    actor_type = pattern.split(r'\[')[0]
                    write_actors.add((actor_type, int(m.group(1))))

        read_actors = set()
        for var_name, _ in reads:
            for pattern in self.actor_patterns:
                m = re.search(pattern, var_name)
                if m:
                    actor_type = pattern.split(r'\[')[0]
                    read_actors.add((actor_type, int(m.group(1))))

        # For each actor found in CMT, create evidence
        for actor_type, actor_idx in set(actor_refs):
            is_write = (actor_type, actor_idx) in write_actors
            is_read = (actor_type, actor_idx) in read_actors

            # Determine association quality
            if is_write:
                quality = 'exact_direct'
            elif is_read:
                quality = 'context_supported'
            else:
                quality = 'ambiguous'

            # Create variable entity
            var_key = '{}[{}]'.format(actor_type, actor_idx)
            var_entity_id = make_entity_id('plc_variable', var_key)

            var_entity = Entity(
                entity_id=var_entity_id,
                entity_type='plc_variable',
                key=var_key,
                display_name=var_key,
            )
            store.add_entity(var_entity)

            # Create documentation entity
            doc_entity_id = make_entity_id('documentation_text', cmt_text[:200])

            doc_entity = Entity(
                entity_id=doc_entity_id,
                entity_type='documentation_text',
                key=cmt_text[:200],
                display_name=cmt_text[:200],
            )
            store.add_entity(doc_entity)

            # Create documented_as relation
            source_detail = 'cmt_rung: pou={} network={} quality={} is_write={}'.format(
                pou_name, network_id, quality, is_write)

            store.add_relation(Relation(
                source_id=var_entity_id,
                target_id=doc_entity_id,
                relation_type='documented_as',
                confidence='exact' if quality == 'exact_direct' else 'structural',
                evidence_source='cmt_actor_extractor',
                evidence_detail=source_detail,
            ))

            # Also create a cmt_referenced_in relation for traceability
            pou_entity_id = make_entity_id('pou', pou_name)
            pou_entity = Entity(
                entity_id=pou_entity_id,
                entity_type='pou',
                key=pou_name,
                display_name=pou_name,
            )
            store.add_entity(pou_entity)

            store.add_relation(Relation(
                source_id=var_entity_id,
                target_id=pou_entity_id,
                relation_type='cmt_referenced_in',
                confidence='structural',
                evidence_source='cmt_actor_extractor',
                evidence_detail='CMT "{}" references {} in {}'.format(
                    cmt_text[:40], var_key, pou_name),
            ))
