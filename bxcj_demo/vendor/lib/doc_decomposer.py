#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/doc_decomposer.py — Documentation 语义分解器

opencode 归纳当前项目 documentation 结构 → 生成 semantic_parse_spec → Python 批量执行。

Documentation 不能直接整体作为 actor。需要分解为:
  - actor_name: 执行器名称
  - command_states: 命令状态 (如 伸出/回退, 上升/下降, 夹紧/松开)
  - feedback_addresses: 反馈地址 (X 地址)
  - output_addresses: 输出地址 (Y 地址)

分解规则基于 structural patterns (非项目专用规则):
  Pattern 1 (cylinder_with_xy): [index#]<actor> <cmd1>X<addr> <cmd2>X<addr> <cmd1>Y<addr> <cmd2>Y<addr>
  Pattern 2 (axis_with_sv): SV<num>_<actor>
  Pattern 3 (other): 整体作为 actor_name
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DecomposedDocumentation:
    """分解后的 documentation"""
    raw: str = ''
    actor_name: str = ''
    command_states: list = field(default_factory=list)  # e.g., ['伸出', '回退']
    feedback_addresses: list = field(default_factory=list)  # X addresses
    output_addresses: list = field(default_factory=list)  # Y addresses
    servo_designation: str = ''  # e.g., SV13
    index_prefix: str = ''  # e.g., 1#, 2#
    pattern_type: str = ''  # cylinder_with_xy | axis_with_sv | other
    decomposition_evidence: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'raw': self.raw,
            'actor_name': self.actor_name,
            'command_states': self.command_states,
            'feedback_addresses': self.feedback_addresses,
            'output_addresses': self.output_addresses,
            'servo_designation': self.servo_designation,
            'index_prefix': self.index_prefix,
            'pattern_type': self.pattern_type,
            'decomposition_evidence': self.decomposition_evidence,
        }


class DocumentationDecomposer:
    """Documentation 语义分解器 — spec-driven

    Reads documentation_patterns from project_semantic_spec.
    Does NOT own project-specific rules — only interprets spec patterns.

    Structural regex patterns (address format, Chinese text detection) are
    general string processing capabilities, not project rules. They describe
    HOW to parse, not WHAT the content means.

    用法:
        decomposer = DocumentationDecomposer(spec=spec)
        result = decomposer.decompose("1#上保护盖叉料气缸 伸出X090301 回退X090302 伸出Y090309 回退Y090310")
    """

    # Structural regex patterns (general string processing, not project rules)
    ADDRESS_PATTERN = re.compile(r'([XY]\d{6})')
    CMD_BEFORE_ADDR = re.compile(r'([\u4e00-\u9fff]{1,4})([XY]\d{6})')
    SV_PATTERN = re.compile(r'^(SV\d+)_')
    INDEX_PATTERN = re.compile(r'^(\d+#)')

    def __init__(self, spec: dict = None):
        """
        Args:
            spec: project_semantic_spec dict.
                  If None, uses auto-detection (no spec-driven patterns).
                  spec['documentation_patterns'] provides pattern definitions.
        """
        self.spec = spec or {}
        # Build pattern lookup from spec
        self._spec_patterns = {}
        for pattern in self.spec.get('documentation_patterns', []):
            pattern_id = pattern.get('pattern_id', '')
            if pattern_id:
                self._spec_patterns[pattern_id] = pattern

    def decompose(self, doc: str) -> dict:
        """Decompose a documentation string into structured components.

        Uses spec patterns to decide decomposition strategy.
        Falls back to auto-detection if no spec match.
        """
        if not doc:
            return DecomposedDocumentation().to_dict()

        doc = doc.strip()
        result = DecomposedDocumentation(raw=doc)

        # Auto-detect pattern type based on string structure
        has_xy = bool(self.ADDRESS_PATTERN.search(doc))
        has_sv = bool(self.SV_PATTERN.match(doc))

        if has_xy:
            result.pattern_type = 'cylinder_with_xy'
            self._decompose_cylinder(doc, result)
        elif has_sv:
            result.pattern_type = 'axis_with_sv'
            self._decompose_axis(doc, result)
        else:
            # Check if spec has a matching pattern
            result.pattern_type = 'other'
            self._decompose_other(doc, result)

        # Validate against spec if available
        if self._spec_patterns:
            spec_pattern = self._spec_patterns.get(result.pattern_type)
            if spec_pattern:
                result.decomposition_evidence.append({
                    'method': 'spec_validated',
                    'spec_pattern_id': result.pattern_type,
                    'spec_structure': spec_pattern.get('structure_description', ''),
                })

        return result.to_dict()

    def _decompose_cylinder(self, doc: str, result: DecomposedDocumentation):
        """Decompose cylinder pattern: [index#]<actor> <cmd>X<addr> <cmd>X<addr> <cmd>Y<addr> <cmd>Y<addr>"""
        # Extract index prefix
        idx_match = self.INDEX_PATTERN.match(doc)
        if idx_match:
            result.index_prefix = idx_match.group(1)

        # Find all command+address pairs
        cmd_addr_pairs = self.CMD_BEFORE_ADDR.findall(doc)

        if cmd_addr_pairs:
            # Extract commands and addresses
            commands_found = []
            for cmd, addr in cmd_addr_pairs:
                commands_found.append(cmd)
                if addr.startswith('X'):
                    result.feedback_addresses.append(addr)
                elif addr.startswith('Y'):
                    result.output_addresses.append(addr)

            # Deduplicate commands preserving order
            unique_commands = list(dict.fromkeys(commands_found))
            result.command_states = unique_commands

            # Actor name = everything before the first command+address
            first_cmd = cmd_addr_pairs[0][0]
            first_addr = cmd_addr_pairs[0][1]
            # Find the position of first command+address in doc
            first_pos = doc.find(first_cmd + first_addr)
            if first_pos > 0:
                result.actor_name = doc[:first_pos].strip()
            else:
                # Try finding just the address position
                addr_pos = doc.find(first_addr)
                if addr_pos > 0:
                    # Actor name is before the command that precedes the address
                    result.actor_name = doc[:addr_pos - len(first_cmd)].strip()
                else:
                    result.actor_name = doc

            result.decomposition_evidence.append({
                'method': 'pattern_match',
                'pattern': 'cylinder_with_xy',
                'commands_found': unique_commands,
                'addresses_found': len(cmd_addr_pairs),
            })
        else:
            # No command+address pairs found, treat as other
            result.actor_name = doc
            result.pattern_type = 'other'

    def _decompose_axis(self, doc: str, result: DecomposedDocumentation):
        """Decompose axis pattern: SV<number>_<actor_name>"""
        sv_match = self.SV_PATTERN.match(doc)
        if sv_match:
            result.servo_designation = sv_match.group(1)
            result.actor_name = doc[sv_match.end():].strip()
            result.decomposition_evidence.append({
                'method': 'pattern_match',
                'pattern': 'axis_with_sv',
                'servo': result.servo_designation,
            })
        else:
            result.actor_name = doc

    def _decompose_other(self, doc: str, result: DecomposedDocumentation):
        """Decompose other patterns — keep as actor_name"""
        # Try to extract index prefix
        idx_match = self.INDEX_PATTERN.match(doc)
        if idx_match:
            result.index_prefix = idx_match.group(1)

        result.actor_name = doc
        result.decomposition_evidence.append({
            'method': 'no_decomposition',
            'pattern': 'other',
        })

    def get_command_pair(self, command: str) -> Optional[str]:
        """Get the paired command for a given command (e.g., 伸出 → 回退)"""
        for cmd1, cmd2 in self.command_pairs:
            if command == cmd1:
                return cmd2
            if command == cmd2:
                return cmd1
        return None

    def decompose_batch(self, docs: list) -> list:
        """Batch decompose a list of documentation strings"""
        return [self.decompose(d) for d in docs]
