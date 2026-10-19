#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/scoped_declaration_resolver.py — Scope-aware variable declaration resolver

PLCopen XML 中同名变量 (如 Cy_Clamp1) 在不同工站 POU 中语义完全不同
(支架夹爪气缸1 vs 夹胶气缸1 vs 刻码下料夹爪气缸)。全局符号名映射会混淆它们。
本解析器使用 **scope + symbol identity** 而非全局符号名来做解析。

解析策略:
    1. 行级扫描大型 PLCopen XML (避免 full DOM)
    2. 识别 <dataType name="EMxx_Variable"> / <dataType name="EMxx_Call"> 块
    3. 提取每个 <variable> 的 name / derived type / documentation
    4. 按 scope (dataType 名) 和 station (EMxx 前缀) 建立索引

full_path 约定:
    EMxx_Variable scope → full_path = "{station}V.{symbol}"  (如 EM02V.Cy_Up1)
    EMxx_Call     scope → full_path = "{station}C.{symbol}"  (如 EM02C.Cy_Up1)
    V = Variables 实例, C = Call 实例

CLI:
    python lib/scoped_declaration_resolver.py <xml_path> [--output <output_json>]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from typing import Optional


# ============================================================
# Actor type mapping
# ============================================================

DATATYPE_TO_ACTOR_TYPE = {
    # Process interface types (used in EMxx_Call)
    'ST_CylinderP': 'cylinder',
    'ST_AxisP': 'axis',
    'ST_Robot': 'robot',

    # Variable struct types (used in EMxx_Variable)
    'ST_Cylinder_06Var': 'cylinder',
    'ST_CylinderOut': 'cylinder',
    'ST_CylinderIn': 'cylinder',
    'ST_Cylinder_03HMIIn': 'cylinder',
    'ST_Cylinder_04HMIOut': 'cylinder',

    'ST_BaseAxis_08AxisVar': 'axis',
    'ST_AxisOut': 'axis',
    'ST_AxisIn': 'axis',
    'ST_AxisSim': 'axis',

    'ST_Sensor_03Var': 'sensor',
    'ST_Sensor_01In': 'sensor',
    'ST_Sensor_02Out': 'sensor',

    'ST_Vacuum_04Var': 'vacuum',

    'ST_Blow_03Var': 'blow',
    'ST_Blow_07HMIVar': 'blow',

    'ST_IOMotor_03Var': 'io_motor',
    'ST_IOMotor_07HMIVar': 'io_motor',

    'ST_Robot_09AxisStatus': 'robot',
    'ST_Robot_06Parameter': 'robot',
    'ST_Robot_05Var': 'robot',

    'ST_CyClamp_08Parameter': 'cylinder_clamp',
    'ST_CyClamp_05HVar': 'cylinder_clamp',
    'ST_CyClamp_04Var': 'cylinder_clamp',
}

# Prefix-based fallback (checked in order, longest prefix first)
_ACTOR_TYPE_PREFIXES = [
    ('ST_CyClamp', 'cylinder_clamp'),
    ('ST_Cylinder', 'cylinder'),
    ('ST_BaseAxis', 'axis'),
    ('ST_Axis', 'axis'),
    ('ST_Sensor', 'sensor'),
    ('ST_Vacuum', 'vacuum'),
    ('ST_Blow', 'blow'),
    ('ST_IOMotor', 'io_motor'),
    ('ST_Robot', 'robot'),
]


def derive_actor_type(datatype: str) -> str:
    """Derive actor type from a datatype name.

    Exact match first, then prefix match, then 'unknown'.
    """
    if not datatype:
        return 'unknown'
    if datatype in DATATYPE_TO_ACTOR_TYPE:
        return DATATYPE_TO_ACTOR_TYPE[datatype]
    for prefix, actor_type in _ACTOR_TYPE_PREFIXES:
        if datatype.startswith(prefix):
            return actor_type
    return 'unknown'


# ============================================================
# DeclarationEntry
# ============================================================

@dataclass
class DeclarationEntry:
    """A single variable declaration within a scoped dataType block."""
    symbol: str            # e.g., "Cy_Up1"
    scope: str             # e.g., "EM02_Variable" (the dataType name)
    station: str           # e.g., "EM02" (derived from scope)
    datatype: str          # e.g., "ST_CylinderP" or "ST_Cylinder_06Var"
    documentation: str     # e.g., "刻码除尘避位气缸"
    source_location: str   # e.g., "line:1115"
    full_path: str         # e.g., "EM02V.Cy_Up1" (Variable) or "EM02C.Cy_Up1" (Call)
    actor_type: str        # e.g., "cylinder" (derived from datatype)


# ============================================================
# ScopedDeclarationResolver
# ============================================================

# Regex patterns (pre-compiled for performance on 867K-line files)
_RE_DATATYPE_OPEN = re.compile(r'<dataType\s+name="([^"]+)"')
_RE_DATATYPE_CLOSE = re.compile(r'</dataType>')
_RE_VARIABLE_OPEN = re.compile(r'<variable\s+name="([^"]+)"')
_RE_VARIABLE_CLOSE = re.compile(r'</variable>')
_RE_DERIVED = re.compile(r'<derived\s+name="([^"]+)"')
_RE_XHTML = re.compile(r'<xhtml[^>]*>(.*?)</xhtml>', re.DOTALL)
_RE_BASE_TYPE = re.compile(
    r'<(BOOL|INT|DINT|UDINT|SINT|USINT|UINT|WORD|DWORD|LWORD|'
    r'REAL|LREAL|STRING|WSTRING|TIME|LTIME|DATE|DT|BYTE)\s*/?>'
)
# Matches EMxx_Variable / EMxx_Call exactly (rejects EM05_Variable1 etc.)
_RE_STATION_SCOPE = re.compile(r'^(EM\d+)_(Variable|Call)$')


class ScopedDeclarationResolver:
    """Resolve PLC variable declarations using scope + symbol identity.

    Same-name variables in different station POUs are kept separate.
    Resolution requires scope, station, or full_path to disambiguate.
    """

    def __init__(self):
        # symbol → list of DeclarationEntry (one per scope where it appears)
        self._entries: dict[str, list[DeclarationEntry]] = {}
        # full_path → DeclarationEntry (unique per V/C suffix)
        self._by_full_path: dict[str, DeclarationEntry] = {}
        # station → list of DeclarationEntry
        self._by_station: dict[str, list[DeclarationEntry]] = {}

    # --------------------------------------------------------
    # Loading
    # --------------------------------------------------------

    def load_from_plcopen_xml(self, xml_path: str) -> int:
        """Parse PLCopen XML and extract all variable declarations.

        Uses line-by-line scanning with a lightweight state machine.
        Returns count of entries loaded.
        """
        current_scope: Optional[str] = None
        current_station: Optional[str] = None
        in_target_scope = False

        # Variable accumulation
        in_variable = False
        var_name: Optional[str] = None
        var_start_line = 0
        var_lines: list[str] = []

        count = 0

        with open(xml_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                # --- dataType open ---
                m_dt = _RE_DATATYPE_OPEN.search(line)
                if m_dt:
                    dt_name = m_dt.group(1)
                    m_ss = _RE_STATION_SCOPE.match(dt_name)
                    if m_ss:
                        current_scope = dt_name
                        current_station = m_ss.group(1)
                        in_target_scope = True
                    else:
                        current_scope = dt_name
                        in_target_scope = False
                    continue

                # --- dataType close ---
                if _RE_DATATYPE_CLOSE.search(line):
                    # Finalize any pending variable before leaving scope
                    if in_variable and in_target_scope and var_name is not None:
                        entry = self._build_entry(
                            var_name, current_scope, current_station,
                            var_start_line, var_lines
                        )
                        if entry:
                            self._add_entry(entry)
                            count += 1
                    in_variable = False
                    var_name = None
                    var_lines = []
                    current_scope = None
                    current_station = None
                    in_target_scope = False
                    continue

                if not in_target_scope:
                    continue

                # --- variable open ---
                if not in_variable:
                    m_var = _RE_VARIABLE_OPEN.search(line)
                    if m_var:
                        in_variable = True
                        var_name = m_var.group(1)
                        var_start_line = line_num
                        var_lines = [line]
                        # Self-closing on same line?
                        if _RE_VARIABLE_CLOSE.search(line):
                            entry = self._build_entry(
                                var_name, current_scope, current_station,
                                var_start_line, var_lines
                            )
                            if entry:
                                self._add_entry(entry)
                                count += 1
                            in_variable = False
                            var_name = None
                            var_lines = []
                        continue
                else:
                    # Inside variable — accumulate lines
                    var_lines.append(line)
                    if _RE_VARIABLE_CLOSE.search(line):
                        if var_name is not None:
                            entry = self._build_entry(
                                var_name, current_scope, current_station,
                                var_start_line, var_lines
                            )
                            if entry:
                                self._add_entry(entry)
                                count += 1
                        in_variable = False
                        var_name = None
                        var_lines = []

        return count

    def _build_entry(
        self, symbol: str, scope: str, station: str,
        start_line: int, lines: list[str]
    ) -> Optional[DeclarationEntry]:
        """Parse accumulated variable lines into a DeclarationEntry."""
        text = '\n'.join(lines)

        # Extract derived type
        m_derived = _RE_DERIVED.search(text)
        datatype = m_derived.group(1) if m_derived else ''

        # Fallback: base type (BOOL, INT, etc.)
        if not datatype:
            m_base = _RE_BASE_TYPE.search(text)
            if m_base:
                datatype = m_base.group(1)

        # Skip entries with no type info (shouldn't happen in EMxx blocks)
        if not datatype:
            return None

        # Extract documentation
        doc = ''
        m_xhtml = _RE_XHTML.search(text)
        if m_xhtml:
            doc = m_xhtml.group(1).strip()

        # Build full_path: V suffix for Variable, C suffix for Call
        scope_suffix = 'V' if scope.endswith('_Variable') else 'C'
        full_path = '{}{}.{}'.format(station, scope_suffix, symbol)

        # Derive actor type
        actor_type = derive_actor_type(datatype)

        return DeclarationEntry(
            symbol=symbol,
            scope=scope,
            station=station,
            datatype=datatype,
            documentation=doc,
            source_location='line:{}'.format(start_line),
            full_path=full_path,
            actor_type=actor_type,
        )

    def _add_entry(self, entry: DeclarationEntry) -> None:
        """Register an entry in all indexes."""
        # symbol → entries
        self._entries.setdefault(entry.symbol, []).append(entry)
        # full_path → entry (first one wins if collision)
        if entry.full_path not in self._by_full_path:
            self._by_full_path[entry.full_path] = entry
        # station → entries
        self._by_station.setdefault(entry.station, []).append(entry)

    # --------------------------------------------------------
    # Resolution
    # --------------------------------------------------------

    def resolve(
        self, symbol: str, scope: str = None, station: str = None
    ) -> Optional[DeclarationEntry]:
        """Resolve a symbol to its declaration entry.

        Resolution order:
        1. If symbol looks like a full_path (contains '.'), direct lookup
        2. If symbol + scope given, lookup by (symbol, scope)
        3. If symbol + station given, lookup by (symbol, station)
           (prefers Variable scope over Call scope)
        4. If only symbol given:
           - If exactly one entry exists, return it
           - If multiple entries exist, return None (ambiguous)

        Returns None if not found or ambiguous.
        """
        # 1. Full path detection
        if '.' in symbol:
            entry = self.resolve_full_path(symbol)
            if entry:
                return entry
            # Fall through to other methods (symbol part after last '.')
            symbol = symbol.rsplit('.', 1)[-1]

        entries = self._entries.get(symbol, [])
        if not entries:
            return None

        # 2. By scope
        if scope:
            for e in entries:
                if e.scope == scope:
                    return e
            return None

        # 3. By station (prefer Variable scope, then Call)
        if station:
            for e in entries:
                if e.station == station and e.scope.endswith('_Variable'):
                    return e
            for e in entries:
                if e.station == station and e.scope.endswith('_Call'):
                    return e
            return None

        # 4. Only symbol — return if unambiguous
        if len(entries) == 1:
            return entries[0]
        return None

    def resolve_full_path(self, full_path: str) -> Optional[DeclarationEntry]:
        """Resolve by full path like 'EM12V.Cy_Block1_2' or 'EM02C.Cy_Up1'."""
        return self._by_full_path.get(full_path)

    # --------------------------------------------------------
    # Query helpers
    # --------------------------------------------------------

    def get_all_for_station(self, station: str) -> list[DeclarationEntry]:
        """Get all declarations for a station."""
        return list(self._by_station.get(station, []))

    def get_all_for_scope(self, scope: str) -> list[DeclarationEntry]:
        """Get all declarations for a specific scope (dataType name)."""
        return [e for entries in self._entries.values() for e in entries if e.scope == scope]

    def get_ambiguous_symbols(self) -> dict[str, list[dict]]:
        """Return all symbols that have multiple entries (same name, different scope).

        Returns {symbol: [{scope, station, full_path, documentation, actor_type}, ...]}.
        """
        result = {}
        for symbol, entries in self._entries.items():
            if len(entries) > 1:
                result[symbol] = [
                    {
                        'scope': e.scope,
                        'station': e.station,
                        'full_path': e.full_path,
                        'documentation': e.documentation,
                        'actor_type': e.actor_type,
                        'datatype': e.datatype,
                    }
                    for e in entries
                ]
        return result

    def get_symbols(self) -> list[str]:
        """Return all unique symbol names."""
        return list(self._entries.keys())

    def get_stations(self) -> list[str]:
        """Return all station names."""
        return list(self._by_station.keys())

    def get_scopes(self) -> list[str]:
        """Return all scope (dataType) names that contributed entries."""
        scopes = set()
        for entries in self._entries.values():
            for e in entries:
                scopes.add(e.scope)
        return sorted(scopes)

    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    def get_statistics(self) -> dict:
        """Return statistics about loaded declarations."""
        all_entries = [e for entries in self._entries.values() for e in entries]
        ambiguous = self.get_ambiguous_symbols()

        # Actor type distribution
        actor_dist: dict[str, int] = {}
        for e in all_entries:
            actor_dist[e.actor_type] = actor_dist.get(e.actor_type, 0) + 1

        # Scope distribution
        scope_dist: dict[str, int] = {}
        for e in all_entries:
            scope_dist[e.scope] = scope_dist.get(e.scope, 0) + 1

        # Station distribution
        station_dist: dict[str, int] = {}
        for e in all_entries:
            station_dist[e.station] = station_dist.get(e.station, 0) + 1

        # Entries with documentation
        with_doc = sum(1 for e in all_entries if e.documentation)

        return {
            'total_entries': len(all_entries),
            'unique_symbols': len(self._entries),
            'ambiguous_symbols': len(ambiguous),
            'ambiguous_symbol_names': sorted(ambiguous.keys()),
            'stations': sorted(self._by_station.keys()),
            'station_count': len(self._by_station),
            'scopes': sorted(scope_dist.keys()),
            'scope_distribution': dict(sorted(scope_dist.items())),
            'station_distribution': dict(sorted(station_dist.items())),
            'actor_type_distribution': dict(
                sorted(actor_dist.items(), key=lambda x: -x[1])
            ),
            'entries_with_documentation': with_doc,
            'entries_without_documentation': len(all_entries) - with_doc,
            'full_path_index_size': len(self._by_full_path),
        }

    # --------------------------------------------------------
    # Serialization
    # --------------------------------------------------------

    def to_dict_list(self) -> list[dict]:
        """Convert all entries to a list of dicts (for JSON serialization)."""
        all_entries = [e for entries in self._entries.values() for e in entries]
        all_entries.sort(key=lambda e: (e.station, e.scope, e.symbol))
        return [asdict(e) for e in all_entries]

    def save_to_json(self, output_path: str) -> None:
        """Save all declarations to a JSON file."""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        data = {
            'statistics': self.get_statistics(),
            'declarations': self.to_dict_list(),
        }
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# CLI
# ============================================================

def _print_statistics(stats: dict) -> None:
    """Print statistics in a human-readable format."""
    print('=' * 60)
    print('ScopedDeclarationResolver — Statistics')
    print('=' * 60)
    print('  Total declarations:      {}'.format(stats['total_entries']))
    print('  Unique symbols:          {}'.format(stats['unique_symbols']))
    print('  Ambiguous symbols:       {} (same name, different scope)'.format(
        stats['ambiguous_symbols']))
    print('  Stations covered:        {} → {}'.format(
        stats['station_count'], ', '.join(stats['stations'])))
    print('  Entries with docs:       {} / {}'.format(
        stats['entries_with_documentation'], stats['total_entries']))
    print('  Full-path index size:    {}'.format(stats['full_path_index_size']))

    print()
    print('  Scope distribution:')
    for scope, cnt in stats['scope_distribution'].items():
        print('    {:30s} {}'.format(scope, cnt))

    print()
    print('  Station distribution:')
    for station, cnt in stats['station_distribution'].items():
        print('    {:10s} {}'.format(station, cnt))

    print()
    print('  Actor type distribution:')
    for actor_type, cnt in stats['actor_type_distribution'].items():
        print('    {:20s} {}'.format(actor_type, cnt))

    if stats['ambiguous_symbols'] > 0:
        print()
        print('  Ambiguous symbols (first 30):')
        for sym in stats['ambiguous_symbol_names'][:30]:
            print('    {}'.format(sym))
        remaining = stats['ambiguous_symbols'] - 30
        if remaining > 0:
            print('    ... and {} more'.format(remaining))

    print('=' * 60)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Resolve PLC variable declarations by scope + symbol identity.'
    )
    parser.add_argument(
        'xml_path',
        help='Path to PLCopen XML file'
    )
    parser.add_argument(
        '--output', '-o',
        default=None,
        help='Output JSON path (default: <project>/output/scoped_declarations.json)'
    )
    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Suppress statistics output'
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.xml_path):
        print('Error: XML file not found: {}'.format(args.xml_path), file=sys.stderr)
        return 1

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        # Default: <xml_dir>/../output/scoped_declarations.json
        # (source/ → ../output/ = project/output/)
        xml_dir = os.path.dirname(os.path.abspath(args.xml_path))
        project_dir = os.path.dirname(xml_dir)
        output_path = os.path.join(project_dir, 'output', 'scoped_declarations.json')

    resolver = ScopedDeclarationResolver()
    count = resolver.load_from_plcopen_xml(args.xml_path)

    resolver.save_to_json(output_path)

    if not args.quiet:
        stats = resolver.get_statistics()
        _print_statistics(stats)
        print()
        print('JSON saved to: {}'.format(output_path))

    return 0


if __name__ == '__main__':
    sys.exit(main())
