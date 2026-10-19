#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/actor_evidence.py — Actor Identity Evidence (Part B).

Splits the former monolithic ActorIdentityHypothesis into 4 independent
relations, each with its own evidence level. The engineering_actor's
certainty is determined by Relation 1 alone (documentation), NOT by
the full IO/HMI chain.

Relations:
    Relation 1: program_actor → documented_as → engineering_actor
        Evidence: same-scope declaration documentation, DUT documentation,
                  exact source comment.
        This relation ALONE determines engineering_actor certainty.

    Relation 2: program_actor → controls → physical_device
        Evidence: FB call site binding (CL_In/CL_Out).

    Relation 3: physical_device → mapped_to → IO/address
        Evidence: physical address in section comments (X####/Y####).

    Relation 4: physical_device → labelled_as → HMI label
        Evidence: HMI alarm mapping, HMI component label.

Each relation is evaluated independently. A program_actor can have
Relation 1 validated (engineering_actor known) without Relation 3
(IO address unknown — e.g., axes use servo addresses, not bit I/O).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ============================================================
# Evidence Levels for Actor Relations
# ============================================================

AR_VALIDATED = 'validated'
AR_PLAUSIBLE = 'plausible'
AR_UNRESOLVED = 'unresolved'

VALID_AR_LEVELS = {AR_VALIDATED, AR_PLAUSIBLE, AR_UNRESOLVED}


# ============================================================
# Relation 1: program_actor → documented_as → engineering_actor
# ============================================================

@dataclass
class DocumentedAsRelation:
    """Relation 1: program_actor → documented_as → engineering_actor.

    This is the PRIMARY relation for description composition.
    If this is validated, engineering_actor is known regardless of
    whether IO addresses or HMI labels are found.

    Fields:
        program_actor      — e.g., "EM02V.Cy_Up1"
        engineering_actor  — e.g., "刻码除尘避位气缸"
        evidence_level     — validated / plausible / unresolved
        evidence_source    — "declaration_documentation" / "dut_documentation" / "source_comment"
        evidence_path      — List of evidence hops
        scope              — Declaration scope (e.g., "EM02_Variable")
        datatype           — e.g., "ST_CylinderP"
        actor_type         — e.g., "cylinder" / "axis" / "vacuum"
    """
    program_actor: str = ''
    engineering_actor: str = ''
    evidence_level: str = AR_UNRESOLVED
    evidence_source: str = ''
    evidence_path: list = field(default_factory=list)
    scope: str = ''
    datatype: str = ''
    actor_type: str = ''


# ============================================================
# Relation 2: program_actor → controls → physical_device
# ============================================================

@dataclass
class ControlsPhysicalDeviceRelation:
    """Relation 2: program_actor → controls → physical_device.

    Evidence: FB call site with CL_In/CL_Out parameter binding.

    Fields:
        program_actor       — e.g., "EM02V.Cy_Up1"
        fb_instance_name    — e.g., "Cy_Up1"
        fb_type             — e.g., "CATL_FB_CylinderCtr_HMX"
        fb_call_pou         — POU where the FB is called
        cl_in_binding       — e.g., "EM02V.Cy_Up1.In"
        cl_out_binding      — e.g., "EM02V.Cy_Up1.Out"
        evidence_level      — validated / plausible / unresolved
        evidence_source     — "fb_callsite_binding"
    """
    program_actor: str = ''
    fb_instance_name: str = ''
    fb_type: str = ''
    fb_call_pou: str = ''
    cl_in_binding: str = ''
    cl_out_binding: str = ''
    evidence_level: str = AR_UNRESOLVED
    evidence_source: str = ''


# ============================================================
# Relation 3: physical_device → mapped_to → IO/address
# ============================================================

@dataclass
class IOMappingRelation:
    """Relation 3: physical_device → mapped_to → IO/address.

    Evidence: Physical addresses in section comments near FB call.

    Fields:
        program_actor       — e.g., "EM02V.Cy_Up1"
        physical_addresses  — e.g., ["X070102", "X070101", "Y070112", "Y070111"]
        evidence_level      — validated / plausible / unresolved
        evidence_source     — "section_comment" / "alarm_mapping_comment"
    """
    program_actor: str = ''
    physical_addresses: list = field(default_factory=list)
    evidence_level: str = AR_UNRESOLVED
    evidence_source: str = ''


# ============================================================
# Relation 4: physical_device → labelled_as → HMI label
# ============================================================

@dataclass
class HMILabelRelation:
    """Relation 4: physical_device → labelled_as → HMI label.

    Evidence: HMI alarm mapping or HMI component label.

    Fields:
        program_actor       — e.g., "EM02V.Cy_Up1"
        hmi_alarm_mapping   — e.g., "H_Messages.Station[2].Object_Alarm[33]"
        hmi_label           — HMI display label (if found)
        evidence_level      — validated / plausible / unresolved
        evidence_source     — "alarm_mapping" / "hmi_component"
    """
    program_actor: str = ''
    hmi_alarm_mapping: str = ''
    hmi_label: str = ''
    evidence_level: str = AR_UNRESOLVED
    evidence_source: str = ''


# ============================================================
# Actor Identity Evidence (composite)
# ============================================================

@dataclass
class ActorIdentityEvidence:
    """Composite actor identity evidence with 4 independent relations.

    The engineering_actor certainty is determined by Relation 1 ALONE.
    Physical identity (Relations 2-4) is separate.

    Fields:
        program_actor         — e.g., "EM02V.Cy_Up1"
        relation1_documented  — Relation 1 (documentation)
        relation2_controls    — Relation 2 (FB call site)
        relation3_io_mapping  — Relation 3 (IO address)
        relation4_hmi_label   — Relation 4 (HMI label)
        engineering_actor     — From Relation 1 (empty if unresolved)
        engineering_certainty — validated / plausible / unresolved (from R1)
        actor_type            — cylinder / axis / vacuum / clamp / flag
        physical_identity     — validated / plausible / unresolved (from R2-4)
        weakest_link          — Description of the weakest relation
    """
    program_actor: str = ''
    relation1_documented: DocumentedAsRelation = None
    relation2_controls: ControlsPhysicalDeviceRelation = None
    relation3_io_mapping: IOMappingRelation = None
    relation4_hmi_label: HMILabelRelation = None

    @property
    def engineering_actor(self) -> str:
        if self.relation1_documented and self.relation1_documented.evidence_level == AR_VALIDATED:
            return self.relation1_documented.engineering_actor
        return ''

    @property
    def engineering_certainty(self) -> str:
        if not self.relation1_documented:
            return AR_UNRESOLVED
        return self.relation1_documented.evidence_level

    @property
    def actor_type(self) -> str:
        if self.relation1_documented:
            return self.relation1_documented.actor_type
        return ''

    @property
    def physical_identity(self) -> str:
        levels = []
        if self.relation2_controls:
            levels.append(self.relation2_controls.evidence_level)
        if self.relation3_io_mapping:
            levels.append(self.relation3_io_mapping.evidence_level)
        if self.relation4_hmi_label:
            levels.append(self.relation4_hmi_label.evidence_level)
        if not levels:
            return AR_UNRESOLVED
        if all(l == AR_VALIDATED for l in levels):
            return AR_VALIDATED
        if any(l == AR_VALIDATED for l in levels):
            return AR_PLAUSIBLE
        if any(l == AR_PLAUSIBLE for l in levels):
            return AR_PLAUSIBLE
        return AR_UNRESOLVED

    @property
    def weakest_link(self) -> str:
        if not self.relation1_documented or self.relation1_documented.evidence_level == AR_UNRESOLVED:
            return 'relation1_documentation_missing'
        if not self.relation2_controls:
            return 'relation2_fb_callsite_missing'
        if not self.relation3_io_mapping or self.relation3_io_mapping.evidence_level == AR_UNRESOLVED:
            return 'relation3_io_address_missing'
        if not self.relation4_hmi_label:
            return 'relation4_hmi_label_missing'
        return 'all_relations_present'

    def to_dict(self) -> dict:
        return {
            'program_actor': self.program_actor,
            'engineering_actor': self.engineering_actor,
            'engineering_certainty': self.engineering_certainty,
            'actor_type': self.actor_type,
            'physical_identity': self.physical_identity,
            'weakest_link': self.weakest_link,
            'relation1_documented': (
                self.relation1_documented.__dict__
                if self.relation1_documented else None),
            'relation2_controls': (
                self.relation2_controls.__dict__
                if self.relation2_controls else None),
            'relation3_io_mapping': (
                self.relation3_io_mapping.__dict__
                if self.relation3_io_mapping else None),
            'relation4_hmi_label': (
                self.relation4_hmi_label.__dict__
                if self.relation4_hmi_label else None),
        }


# ============================================================
# Actor Evidence Resolver
# ============================================================

class ActorEvidenceResolver:
    """Resolves actor identity evidence from multiple sources.

    Uses ScopedDeclarationResolver for Relation 1 (documentation),
    and existing actor_identity_hypotheses.json for Relations 2-4.
    """

    def __init__(self):
        self._declarations = {}
        self._actor_bridges = {}

    def load_scoped_declarations(self, declarations_json_path: str) -> int:
        """Load scoped declarations from ScopedDeclarationResolver output.

        This populates Relation 1 evidence.
        """
        with open(declarations_json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        entries = data.get('entries', [])
        for entry in entries:
            full_path = entry.get('full_path', '')
            if not full_path:
                continue
            self._declarations[full_path] = entry

        return len(self._declarations)

    def load_actor_bridges(self, actor_hypotheses_path: str) -> int:
        """Load existing actor bridge hypotheses.

        This populates Relations 2-4 evidence.
        """
        with open(actor_hypotheses_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        actors = data if isinstance(data, list) else data.get('actors', data.get('hypotheses', []))
        for actor in actors:
            program_actor = actor.get('program_actor', '')
            if not program_actor:
                continue
            self._actor_bridges[program_actor] = actor

        return len(self._actor_bridges)

    def resolve(self, program_actor: str) -> ActorIdentityEvidence:
        """Resolve all 4 relations for a program actor.

        Args:
            program_actor: e.g., "EM02V.Cy_Up1"

        Returns:
            ActorIdentityEvidence with all available relations
        """
        evidence = ActorIdentityEvidence(program_actor=program_actor)

        decl = self._declarations.get(program_actor)
        if decl and decl.get('documentation'):
            evidence.relation1_documented = DocumentedAsRelation(
                program_actor=program_actor,
                engineering_actor=decl['documentation'],
                evidence_level=AR_VALIDATED,
                evidence_source='declaration_documentation',
                evidence_path=[
                    'scope: {}'.format(decl.get('scope', '')),
                    'datatype: {}'.format(decl.get('datatype', '')),
                    'line: {}'.format(decl.get('source_location', '')),
                ],
                scope=decl.get('scope', ''),
                datatype=decl.get('datatype', ''),
                actor_type=decl.get('actor_type', ''),
            )
        elif decl:
            evidence.relation1_documented = DocumentedAsRelation(
                program_actor=program_actor,
                engineering_actor='',
                evidence_level=AR_PLAUSIBLE,
                evidence_source='declaration_without_documentation',
                scope=decl.get('scope', ''),
                datatype=decl.get('datatype', ''),
                actor_type=decl.get('actor_type', ''),
            )

        bridge = self._actor_bridges.get(program_actor)
        if bridge:
            if bridge.get('fb_instance_name') or bridge.get('fb_type'):
                evidence.relation2_controls = ControlsPhysicalDeviceRelation(
                    program_actor=program_actor,
                    fb_instance_name=bridge.get('fb_instance_name', ''),
                    fb_type=bridge.get('fb_type', ''),
                    fb_call_pou=bridge.get('fb_call_pou', ''),
                    cl_in_binding=bridge.get('cl_in_binding', ''),
                    cl_out_binding=bridge.get('cl_out_binding', ''),
                    evidence_level=AR_VALIDATED,
                    evidence_source='fb_callsite_binding',
                )

            addrs = bridge.get('physical_addresses', [])
            if addrs:
                evidence.relation3_io_mapping = IOMappingRelation(
                    program_actor=program_actor,
                    physical_addresses=addrs,
                    evidence_level=AR_VALIDATED,
                    evidence_source='section_comment',
                )

            hmi_map = bridge.get('hmi_alarm_mapping', '')
            if hmi_map:
                evidence.relation4_hmi_label = HMILabelRelation(
                    program_actor=program_actor,
                    hmi_alarm_mapping=hmi_map,
                    hmi_label=bridge.get('hmi_label', ''),
                    evidence_level=AR_VALIDATED,
                    evidence_source='alarm_mapping',
                )

        return evidence

    def resolve_all_for_project(self, project_actors: list) -> list:
        """Resolve evidence for all actors in a project.

        Args:
            project_actors: List of program_actor strings

        Returns:
            List of ActorIdentityEvidence
        """
        return [self.resolve(a) for a in project_actors]

    def get_statistics(self, evidences: list) -> dict:
        """Compute statistics for a list of evidences."""
        total = len(evidences)
        r1_validated = sum(1 for e in evidences
                          if e.engineering_certainty == AR_VALIDATED)
        r1_plausible = sum(1 for e in evidences
                          if e.engineering_certainty == AR_PLAUSIBLE)
        r1_unresolved = sum(1 for e in evidences
                           if e.engineering_certainty == AR_UNRESOLVED)
        r2_validated = sum(1 for e in evidences
                          if e.relation2_controls and
                          e.relation2_controls.evidence_level == AR_VALIDATED)
        r3_validated = sum(1 for e in evidences
                          if e.relation3_io_mapping and
                          e.relation3_io_mapping.evidence_level == AR_VALIDATED)
        r4_validated = sum(1 for e in evidences
                          if e.relation4_hmi_label and
                          e.relation4_hmi_label.evidence_level == AR_VALIDATED)

        return {
            'total_actors': total,
            'relation1_validated': r1_validated,
            'relation1_plausible': r1_plausible,
            'relation1_unresolved': r1_unresolved,
            'relation2_validated': r2_validated,
            'relation3_validated': r3_validated,
            'relation4_validated': r4_validated,
            'engineering_actor_coverage_pct': round(
                r1_validated / total * 100, 1) if total > 0 else 0,
        }
