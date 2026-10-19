#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/timing_boundary_resolver.py — Timing Boundary Resolver (Program Primitive)

Resolves timing boundaries (start/end expressions) for engineering actions
from multiple evidence sources in PLC source code.

Evidence sources (by priority):
  1. explicit_action_done — ActionDone[idx] := end_expr in ST code
  2. explicit_process_total — bEM_Stop in ST code
  3. step_next_state — topology-sliced direct transition (StepLogicSlicer)
  4. step_exit — NOT(current_step) fallback

General patterns (no project-specific rules):
  - ActionStart[idx] := <step_identity_expr>  (start condition = step identity)
  - ActionDone[idx] := <end_condition_expr>   (end condition = timing boundary)
  - bEM<N>_Start := <start_expr>              (process total start)
  - bEM<N>_Stop := <stop_expr>                (process total end)
  - State variable transition: MOVE/ST assignment to state variable

The resolver uses state_variable config to parse step identities from
ActionStart expressions. This is format-independent — works for
Process_steps, AutoStep, m_nNextAutoStep, etc.

Output: TimingBoundary per step with:
  - start_expression, end_expression
  - boundary_kind (explicit_action_done, step_next_state, step_exit, etc.)
  - provenance (source file, pattern, evidence)
  - confidence (high/medium/low)
  - alternatives (other candidates considered)
  - unresolved_reason (if boundary cannot be determined)
"""
from __future__ import annotations

import re
import os
import glob
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TimingBoundary:
    """Timing boundary for a single engineering action."""
    step_identity: dict = field(default_factory=dict)
    # {state_variable, station, thread_idx, step_value}

    start_expression: str = ''
    end_expression: str = ''

    boundary_kind: str = ''
    # explicit_action_done, explicit_process_total,
    # step_next_state, step_exit, compound_condition, unresolved

    provenance: str = ''
    # Source: 'action_done_st_code', 'bem_st_code',
    #         'topology_slice', 'fallback_not_step'

    confidence: str = ''
    # high (explicit source code), medium (topology), low (fallback)

    alternatives: list = field(default_factory=list)
    # Other end_condition candidates

    unresolved_reason: str = ''

    action_index: int = -1
    # ActionStart/Done index, if from explicit source

    parallel_actions: list = field(default_factory=list)
    # For steps with multiple parallel ActionStart/Done

    bem_index: int = -1
    # bEM index, if from process_total

    source_file: str = ''

    def to_dict(self) -> dict:
        return {
            'step_identity': self.step_identity,
            'start_expression': self.start_expression,
            'end_expression': self.end_expression,
            'boundary_kind': self.boundary_kind,
            'provenance': self.provenance,
            'confidence': self.confidence,
            'alternatives': self.alternatives,
            'unresolved_reason': self.unresolved_reason,
            'action_index': self.action_index,
            'parallel_actions': self.parallel_actions,
            'bem_index': self.bem_index,
            'source_file': self.source_file,
        }


class TimingBoundaryResolver:
    """Resolves timing boundaries from PLC source code evidence.

    General approach:
    1. Extract ActionStart/Done pairs from ST code in all source files
    2. Extract bEM_Start/Stop from ST code
    3. Load topology-sliced transitions from step_logic_slices
    4. For each step, resolve end_tag by priority

    No project-specific rules. Uses state_variable config to parse
    step identities from ActionStart expressions.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.state_vars = config.get('state_variables', [])
        if not self.state_vars:
            single = config.get('step_var_name', '')
            if single:
                self.state_vars = [{'name': single}]

        self.state_var_names = [sv['name'] for sv in self.state_vars]
        extract_pat = config.get('step_var_extract_pattern') or r'([A-Z]{2}\d+)V?\.(\w+)\[(\d+)\]'
        self.step_var_extract_re = re.compile(extract_pat)

        # General pattern for AutoStep style (ST01.AutoStep[2]=110)
        self.autostep_extract_re = re.compile(
            r'(\w+)\.(\w+)\[(\d+)\]\s*=\s*(?:INT#)?(\d+)')

        # Build state var regex for matching
        if self.state_var_names:
            sv_pattern = '|'.join(re.escape(v) for v in self.state_var_names)
            self.state_var_re = re.compile(sv_pattern)
        else:
            self.state_var_re = None

    def resolve_from_st_code(self, source_files: list,
                             step_logic_slices: list = None) -> dict:
        """Extract ActionStart/Done and bEM from ST code, resolve boundaries.

        Args:
            source_files: list of XML file paths
            step_logic_slices: list of slice dicts (from step_logic_slices.json)

        Returns:
            dict: {
                'boundaries': {(station, thread, step): [TimingBoundary, ...]},
                'bem_boundaries': {(station, thread, bem_idx): TimingBoundary},
                'fallback_transitions': {(station, thread, step): [(target_thread, target_value), ...]},
            }
        """
        # 1. Extract ActionStart/Done from ST code
        action_pairs = self._extract_action_start_done(source_files)

        # 2. Extract bEM from ST code
        bem_pairs = self._extract_bem(source_files)

        # 3. Build transition map from slices (fallback)
        # Normalize keys: for state vars with empty station (SMC2/Mylar),
        # use 'Station' as the key to match resolve_all_steps normalization.
        slice_transitions = {}
        if step_logic_slices:
            for s in step_logic_slices:
                si = s.get('step_identity', {})
                station = si.get('station', '')
                thread = si.get('thread_idx', -1)
                step_val = si.get('step_value', -1)
                state_var = si.get('state_variable', '')
                # Normalize: SMC2 state vars have empty station
                if not station and state_var.startswith('m_n'):
                    station = 'Station'
                key = (station, thread, step_val)
                trans = s.get('transitions', [])
                targets = []
                for t in trans:
                    ts = t.get('target_step', {})
                    tv = ts.get('value', -1)
                    tt = ts.get('thread', thread)
                    if tv >= 0:
                        targets.append((tt, tv, t.get('provenance', 'topology_only')))
                if targets:
                    slice_transitions[key] = targets

        # 4. Resolve boundaries
        boundaries = defaultdict(list)
        for idx in sorted(action_pairs.keys()):
            starts = action_pairs[idx]['starts']
            dones = action_pairs[idx]['dones']
            source_file = action_pairs[idx]['source_file']

            max_len = max(len(starts), len(dones))
            for i in range(max_len):
                s = starts[i] if i < len(starts) else ''
                d = dones[i] if i < len(dones) else ''

                if not s:
                    continue

                step_id = self._parse_step_identity(s)
                if step_id is None:
                    continue

                key = (step_id['station'], step_id['thread_idx'],
                       step_id['step_value'])

                if d:
                    boundary_kind = self._classify_end_expression(d)
                    confidence = 'high'
                    provenance = 'action_done_st_code'
                else:
                    # No ActionDone — try slice transition
                    targets = slice_transitions.get(key, [])
                    if targets:
                        direct = [(tt, tv) for tt, tv, _ in targets
                                  if tv != step_id['step_value'] and tv >= 0]
                        if direct:
                            tt, tv = direct[0]
                            d = self._format_step_expr(
                                step_id['state_variable'],
                                step_id['station'], tt, tv)
                            boundary_kind = 'step_next_state'
                            confidence = 'medium'
                            provenance = 'topology_slice'
                        else:
                            d = self._format_not_step(step_id)
                            boundary_kind = 'step_exit'
                            confidence = 'low'
                            provenance = 'fallback_not_step'
                    else:
                        d = self._format_not_step(step_id)
                        boundary_kind = 'step_exit'
                        confidence = 'low'
                        provenance = 'fallback_not_step'

                tb = TimingBoundary(
                    step_identity=step_id,
                    start_expression=s,
                    end_expression=d,
                    boundary_kind=boundary_kind,
                    provenance=provenance,
                    confidence=confidence,
                    action_index=idx,
                    source_file=source_file,
                )

                # Add alternatives from slice transitions
                targets = slice_transitions.get(key, [])
                if targets:
                    tb.alternatives = [
                        {'target_thread': tt, 'target_value': tv,
                         'source': 'topology_slice'}
                        for tt, tv, _ in targets
                        if tv != step_id['step_value']
                    ]

                boundaries[key].append(tb)

        # Handle parallel actions: if multiple boundaries for same key,
        # keep them as parallel_actions
        for key, tbs in boundaries.items():
            if len(tbs) > 1:
                for tb in tbs:
                    tb.parallel_actions = [
                        {'action_index': t.action_index,
                         'start': t.start_expression,
                         'end': t.end_expression}
                        for t in tbs if t.action_index != tb.action_index
                    ]

        # 5. Resolve bEM boundaries
        bem_boundaries = {}
        for idx in sorted(bem_pairs.keys()):
            starts = bem_pairs[idx]['starts']
            stops = bem_pairs[idx]['stops']
            source_file = bem_pairs[idx]['source_file']

            for i in range(max(len(starts), len(stops))):
                s = starts[i] if i < len(starts) else ''
                e = stops[i] if i < len(stops) else ''

                if not s and not e:
                    continue

                step_id = None
                if s:
                    step_id = self._parse_step_identity(s)
                if step_id is None and e:
                    step_id = self._parse_step_identity(e)

                if step_id is None:
                    continue

                key = (step_id['station'], step_id['thread_idx'], idx)
                bem_boundaries[key] = TimingBoundary(
                    step_identity=step_id,
                    start_expression=s,
                    end_expression=e,
                    boundary_kind='explicit_process_total',
                    provenance='bem_st_code',
                    confidence='high',
                    bem_index=idx,
                    source_file=source_file,
                )

        return {
            'boundaries': dict(boundaries),
            'bem_boundaries': bem_boundaries,
            'slice_transitions': slice_transitions,
        }

    def resolve_all_steps(self, step_list: list, source_files: list,
                          step_logic_slices: list = None) -> list:
        """Resolve timing boundaries for ALL steps (not just those with ActionDone).

        Args:
            step_list: list of step dicts with step_identity
            source_files: XML source files
            step_logic_slices: topology slices

        Returns:
            list of TimingBoundary (one per step, with parallel_actions if applicable)
        """
        resolution = self.resolve_from_st_code(source_files, step_logic_slices)
        action_boundaries = resolution['boundaries']
        bem_boundaries = resolution['bem_boundaries']
        slice_transitions = resolution['slice_transitions']

        # General principle: if no ActionStart/Done definitions exist anywhere
        # in the source code, the program uses implicit (dwell) timing.
        # In that case, step_exit (NOT step) is the correct timing semantics,
        # NOT step_next_state, even if a single transition target exists.
        # This is because without explicit action completion definitions,
        # the timing measures "how long the step stays active" (dwell),
        # not "when the step transitions to a specific next value".
        has_explicit_timing = len(action_boundaries) > 0

        all_boundaries = []

        for step in step_list:
            si = step.get('step_identity', step)
            station = si.get('station', '')
            thread = si.get('thread_idx', -1)
            step_val = si.get('step_value', -1)
            state_var = si.get('state_variable', '')

            if thread < 0 or step_val < 0:
                continue

            # Skip non-production state variables
            if state_var in ('Init_Steps', 'm_nNextPowerUpStep',
                             'm_nNextAutoStep_Up', 'm_nNextAutoStep_load_Up'):
                continue

            # For SMC2 format, station may be empty
            if not station and state_var.startswith('m_n'):
                station = 'Station'

            if not station:
                continue

            key = (station, thread, step_val)

            if key in action_boundaries:
                # Has explicit ActionDone — use it (may have parallel actions)
                for tb in action_boundaries[key]:
                    all_boundaries.append(tb)
            else:
                # No ActionDone — use slice transitions or fallback
                targets = slice_transitions.get(key, [])
                start_expr = self._format_step_expr(state_var, station, thread, step_val)

                if targets:
                    direct = [(tt, tv) for tt, tv, _ in targets
                              if tv != step_val and tv >= 0]
                    if direct:
                        unique_targets = set((tt, tv) for tt, tv in direct)
                        tt, tv = direct[0]

                        if len(unique_targets) > 1:
                            # Multiple distinct targets — cannot determine
                            # single direct successor. Use step_exit with
                            # ambiguity note. This is NOT the same as
                            # verified_normal_next_state.
                            end_expr = self._format_not_step(
                                {'state_variable': state_var,
                                 'station': station,
                                 'thread_idx': thread,
                                 'step_value': step_val})
                            boundary_kind = 'step_exit'
                            confidence = 'medium'
                            provenance = 'topology_slice_ambiguous'
                        elif tv == 0:
                            # Target is 0 (idle/stop) — state exit to idle
                            end_expr = self._format_not_step(
                                {'state_variable': state_var,
                                 'station': station,
                                 'thread_idx': thread,
                                 'step_value': step_val})
                            boundary_kind = 'step_exit'
                            confidence = 'medium'
                            provenance = 'topology_slice'
                        else:
                            # Single unambiguous direct successor — this is
                            # a verified normal next state transition.
                            # Use it as the end_tag regardless of whether
                            # explicit ActionDone exists.
                            tt, tv = direct[0]
                            end_expr = self._format_step_expr(
                                state_var, station, tt, tv)
                            boundary_kind = 'verified_normal_next_state'
                            confidence = 'medium'
                            provenance = 'topology_slice'
                    else:
                        end_expr = self._format_not_step(
                            {'state_variable': state_var,
                             'station': station,
                             'thread_idx': thread,
                             'step_value': step_val})
                        boundary_kind = 'step_exit'
                        confidence = 'low'
                        provenance = 'fallback_not_step'
                else:
                    end_expr = self._format_not_step(
                        {'state_variable': state_var,
                         'station': station,
                         'thread_idx': thread,
                         'step_value': step_val})
                    boundary_kind = 'step_exit'
                    confidence = 'low'
                    provenance = 'fallback_not_step'
                    if not targets:
                        pass  # no transitions found in topology slice

                tb = TimingBoundary(
                    step_identity={
                        'state_variable': state_var,
                        'station': station,
                        'thread_idx': thread,
                        'step_value': step_val,
                    },
                    start_expression=start_expr,
                    end_expression=end_expr,
                    boundary_kind=boundary_kind,
                    provenance=provenance,
                    confidence=confidence,
                    alternatives=[
                        {'target_thread': tt, 'target_value': tv,
                         'source': 'topology_slice'}
                        for tt, tv, _ in targets
                        if tv != step_val
                    ],
                )
                all_boundaries.append(tb)

        return all_boundaries

    # ============================================================
    # ST code extraction (general patterns)
    # ============================================================

    def _extract_action_start_done(self, source_files: list) -> dict:
        """Extract ActionStart[idx]/ActionDone[idx] pairs from ST code.

        General pattern (not project-specific):
          ActionStart[idx] := <expr>;
          ActionDone[idx] := <expr>;

        Returns:
            {idx: {'starts': [str], 'dones': [str], 'source_file': str}}
        """
        pairs = {}

        for fpath in source_files:
            if not os.path.exists(fpath):
                continue
            with open(fpath, encoding='utf-8', errors='replace') as f:
                content = f.read()

            st_code = self._extract_st_code(content)
            if not st_code.strip():
                continue

            fname = os.path.basename(fpath)

            starts = re.findall(
                r'ActionStart\[(\d+)\]\s*:=\s*([^;]+);', st_code)
            dones = re.findall(
                r'ActionDone\[(\d+)\]\s*:=\s*([^;]+);', st_code)

            starts_by_idx = defaultdict(list)
            for idx, expr in starts:
                starts_by_idx[int(idx)].append(expr.strip())
            dones_by_idx = defaultdict(list)
            for idx, expr in dones:
                dones_by_idx[int(idx)].append(expr.strip())

            for idx in sorted(set(list(starts_by_idx.keys()) +
                                 list(dones_by_idx.keys()))):
                if idx not in pairs:
                    pairs[idx] = {
                        'starts': [], 'dones': [], 'source_file': fname
                    }
                pairs[idx]['starts'].extend(starts_by_idx.get(idx, []))
                pairs[idx]['dones'].extend(dones_by_idx.get(idx, []))
                if not pairs[idx]['source_file']:
                    pairs[idx]['source_file'] = fname

        return pairs

    def _extract_bem(self, source_files: list) -> dict:
        """Extract bEM<N>_Start/bEM<N>_Stop from ST code.

        General pattern:
          bEM<N>_Start := <expr>;
          bEM<N>_Stop := <expr>;

        Returns:
            {idx: {'starts': [str], 'stops': [str], 'source_file': str}}
        """
        pairs = {}

        for fpath in source_files:
            if not os.path.exists(fpath):
                continue
            with open(fpath, encoding='utf-8', errors='replace') as f:
                content = f.read()

            st_code = self._extract_st_code(content)
            if not st_code.strip():
                continue

            fname = os.path.basename(fpath)

            starts = re.findall(
                r'bEM(\d+)_Start\s*:=\s*([^;]+);', st_code)
            stops = re.findall(
                r'bEM(\d+)_Stop\s*:=\s*([^;]+);', st_code)

            starts_by_idx = defaultdict(list)
            for idx, expr in starts:
                starts_by_idx[int(idx)].append(expr.strip())
            stops_by_idx = defaultdict(list)
            for idx, expr in stops:
                stops_by_idx[int(idx)].append(expr.strip())

            for idx in sorted(set(list(starts_by_idx.keys()) +
                                 list(stops_by_idx.keys()))):
                if idx not in pairs:
                    pairs[idx] = {
                        'starts': [], 'stops': [], 'source_file': fname
                    }
                pairs[idx]['starts'].extend(starts_by_idx.get(idx, []))
                pairs[idx]['stops'].extend(stops_by_idx.get(idx, []))

        return pairs

    def _extract_st_code(self, content: str) -> str:
        """Extract and unescape ST code from PLCopen XML content."""
        st_blocks = re.findall(
            r'<STCode[^>]*>(.*?)</STCode>', content, re.DOTALL)
        st_code = ''
        for block in st_blocks:
            t = block
            for a, b in [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'),
                         ('&quot;', '"'), ('&apos;', "'")]:
                t = t.replace(a, b)
            st_code += t + '\n'
        return st_code

    # ============================================================
    # Step identity parsing (format-independent)
    # ============================================================

    def _parse_step_identity(self, expr: str) -> dict:
        """Parse step identity from an expression.

        Handles multiple formats:
          EM01V.Process_steps[2]=130  (转接片)
          ST01.AutoStep[2]=110        (超声)
          m_nNextAutoStep[2]=INT#10   (Mylar)

        Returns None if expression doesn't match any state variable pattern.
        """
        expr = expr.strip()

        # Try Process_steps format: EM01V.Process_steps[2]=130
        m = self.step_var_extract_re.search(expr)
        if m and m.lastindex >= 3:
            var_name = m.group(2)
            if self.state_var_re and self.state_var_re.search(var_name):
                return {
                    'state_variable': var_name,
                    'station': m.group(1),
                    'thread_idx': int(m.group(3)),
                    'step_value': self._extract_step_value(expr),
                }

        # Try AutoStep format: ST01.AutoStep[2]=110
        m = self.autostep_extract_re.search(expr)
        if m:
            var_name = m.group(2)
            if self.state_var_re and self.state_var_re.search(var_name):
                return {
                    'state_variable': var_name,
                    'station': m.group(1),
                    'thread_idx': int(m.group(3)),
                    'step_value': int(m.group(4)),
                }

        # Try m_nNextAutoStep format: m_nNextAutoStep[2]=INT#10
        if self.state_var_re:
            for sv_name in self.state_var_names:
                pat = re.compile(
                    re.escape(sv_name) + r'\[(\d+)\]\s*=\s*(?:INT#)?(\d+)')
                m = pat.search(expr)
                if m:
                    return {
                        'state_variable': sv_name,
                        'station': '',
                        'thread_idx': int(m.group(1)),
                        'step_value': int(m.group(2)),
                    }

        # Fallback: try any <word>.<word>[<num>]=<num> pattern
        m = re.search(r'(\w+)\.(\w+)\[(\d+)\]\s*=\s*(?:INT#)?(\d+)', expr)
        if m:
            var_name = m.group(2)
            if self.state_var_re and self.state_var_re.search(var_name):
                return {
                    'state_variable': var_name,
                    'station': m.group(1),
                    'thread_idx': int(m.group(3)),
                    'step_value': int(m.group(4)),
                }

        return None

    def _extract_step_value(self, expr: str) -> int:
        """Extract step value from expression like 'EM01V.Process_steps[2]=130'."""
        m = re.search(r'=\s*(?:INT#)?(\d+)', expr)
        return int(m.group(1)) if m else -1

    def _classify_end_expression(self, expr: str) -> str:
        """Classify the boundary kind from end expression.

        Types:
          - explicit_action_done: ActionDone[idx] expression
          - compound_condition: contains AND/OR
          - step_next_state: simple <state_var>=<value>
          - step_exit: NOT(state_var)
          - physical_completion: contains PosOK/AbsEx/Signal etc.
        """
        expr = expr.strip()

        if expr.startswith('NOT'):
            if 'ActionStart' in expr:
                return 'explicit_action_done'
            return 'step_exit'

        if ' AND ' in expr or ' OR ' in expr:
            return 'compound_condition'

        # Check if it's a simple state transition
        if self._parse_step_identity(expr):
            return 'step_next_state'

        # Check for physical completion signals
        if any(kw in expr for kw in ['PosOK', 'AbsEx', 'Signal', 'PosDone',
                                      'InPlace', 'M_Act', 'Clamping']):
            return 'physical_completion'

        return 'explicit_action_done'

    def _format_step_expr(self, state_var: str, station: str,
                          thread: int, step_val: int) -> str:
        """Format a step expression string."""
        if state_var == 'Process_steps':
            return f'{station}V.Process_steps[{thread}]={step_val}'
        elif state_var == 'AutoStep':
            return f'{station}.AutoStep[{thread}]={step_val}'
        elif state_var == 'm_nNextAutoStep':
            return f'm_nNextAutoStep[{thread}]=INT#{step_val}'
        elif state_var:
            if station:
                return f'{station}V.{state_var}[{thread}]={step_val}'
            return f'{state_var}[{thread}]={step_val}'
        return ''

    def _format_not_step(self, step_id: dict) -> str:
        """Format NOT(step) expression."""
        expr = self._format_step_expr(
            step_id.get('state_variable', ''),
            step_id.get('station', ''),
            step_id.get('thread_idx', -1),
            step_id.get('step_value', -1))
        return f'NOT ({expr})' if expr else ''
