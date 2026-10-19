#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/build_step_graph.py — Build StepGraph from Canonical IR + DefUse + Slicer

完整流水线:
  DecodedProgram → DefUse → Step Anchors → StepLogicSlicer → steps_raw → StepGraph

用法:
    python tools/build_step_graph.py <project_dir> [--config <config.json>]

输出:
    output/steps_raw.json
    output/step_graph.json
    output/step_logic_slices.json
"""
import sys
import json
import re
from pathlib import Path
from collections import defaultdict, Counter

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.decoders.smc2_decoder import SMC2Decoder
from lib.decoders.plcopen_decoder import PLCopenDecoder
from lib.def_use_analyzer import DefUseAnalyzer
from lib.step_logic_slicer import StepLogicSlicer, find_step_anchors
from lib.step_graph import steps_raw_to_step_graph, StepGraph
from lib.boolean_expr import bool_expr_to_string
from lib.primitives_canonical import discover_steps_from_canonical, extract_step_logic_from_canonical


def detect_format(project_dir: Path) -> str:
    """Detect source format from source directory."""
    source_dir = project_dir / 'source'
    if source_dir.exists():
        for f in source_dir.glob('*.xml'):
            try:
                with open(f, 'r', encoding='utf-8', errors='ignore') as fh:
                    header = fh.read(2000)
                    if '<smc2' in header:
                        return 'smc2'
                    if 'plcopen' in header.lower() or '<project' in header.lower():
                        return 'plcopen'
            except Exception:
                pass
    return 'plcopen'


def load_config(project_dir: Path) -> dict:
    """Load extract_config.json or return defaults."""
    config_path = project_dir / 'output' / 'extract_config.json'
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        # Flatten primitive_params
        pp = config.get('primitive_params', {})
        for k, v in pp.items():
            if k not in config:
                config[k] = v
        # Flatten discovery_strategies
        strategies = [s['name'] for s in config.get('discovery_strategies', [])]
        if strategies:
            config['strategies'] = strategies
        return config

    # Default config for Mylar
    return {
        'state_variables': [
            {'name': 'm_nNextAutoStep'},
            {'name': 'm_nNextPowerUpStep'},
            {'name': 'm_nNextAutoStep_Up'},
            {'name': 'm_nNextAutoStep_load_Up'},
        ],
        'step_var_extract_pattern': r'(\w+)\[(\d+)\]',
        'eq_block_type': 'EQ',
    }


def decode_sources(project_dir: Path, config: dict) -> list:
    """Decode actual supplied source files; fail on missing/empty inputs.

    SMC2 export files usually use a .smc2 extension, even though the
    serialized contents are XML. A zero-program decode is not a valid
    empty PLC project.
    """
    source_format = config.get('source_decoder', detect_format(project_dir))
    source_dir = project_dir / 'source'
    if source_format in ('smc2', 'SMC2Decoder'):
        decoder = SMC2Decoder()
        source_files = sorted(set(source_dir.glob('*.smc2')) | set(source_dir.glob('*.xml')))
    elif source_format in ('plcopen', 'plcopen_xml', 'PLCopenDecoder'):
        decoder = PLCopenDecoder()
        split_dir = config.get('split_dir', 'MySplitProject/Actions')
        actions_dir = project_dir / split_dir
        if actions_dir.exists() and any(actions_dir.glob('*.xml')):
            source_files = sorted(actions_dir.glob('*.xml'))
        else:
            source_files = sorted(source_dir.glob('*.xml'))
    else:
        raise ValueError(f'Unsupported source_decoder: {source_format!r}')
    if not source_files:
        raise FileNotFoundError(f'No {source_format} source files found under {source_dir}')
    programs = [decoder.decode(str(f)) for f in source_files]
    empty = [str(f) for f, dp in zip(source_files, programs) if not dp.program_units]
    if empty:
        raise ValueError(f'Decoder returned no program units for: {empty}')
    return programs


def slices_to_steps_raw(slices: list) -> list:
    """Convert StepLogicSlice list to steps_raw.json format."""
    steps_raw = []
    for s in slices:
        # Extract guard variable names
        guards = []
        for g in s.guards:
            label = ('NOT ' + g.variable) if g.negated else g.variable
            if label and label not in guards:
                guards.append(label)

        # Extract common enables
        common_enables = []
        for ce in s.common_enables:
            label = ('NOT ' + ce.variable) if ce.negated else ce.variable
            if label and label not in common_enables:
                common_enables.append(label)

        # Extract actions
        actions = []
        for a in s.actions:
            if a.variable and a.variable not in actions:
                actions.append(a.variable)

        # Extract calls
        calls = [{'name': c.callee} for c in s.calls]

        # Extract timers
        timers = [{'name': t.timer_type, 'pt': t.preset} for t in s.timers]

        # Extract transitions with boolean_expr
        transitions = []
        for t in s.transitions:
            ts = t.target_step or {}
            transition = {
                'target_thread': ts.get('thread', -1),
                'target_value': ts.get('value', 0),
                'source': 'topology_slice',
            }
            # Add condition from boolean_expr
            if t.boolean_expr:
                condition_str = _bool_dict_to_string(t.boolean_expr)
                if condition_str:
                    transition['condition'] = condition_str
            # Propagate provenance from evidence path
            prov = 'topology_only'
            for step in (t.evidence_path or []):
                if 'variable_flow' in step.get('edge_type', ''):
                    status = step.get('resolution_status', 'ambiguous')
                    if status == 'ambiguous':
                        prov = 'ambiguous'
                        break
                    elif prov == 'topology_only':
                        prov = 'validated_dataflow'
            if prov != 'topology_only':
                transition['provenance'] = prov
            transitions.append(transition)

        step = {
            'state_variable': s.step_identity.get('state_variable', ''),
            'station': s.step_identity.get('station', ''),
            'thread_idx': s.step_identity.get('thread_idx', 0),
            'step_value': s.step_identity.get('step_value', 0),
            'comment': s.comment or '',
            'actions': actions,
            'guards': guards,
            'common_enables': common_enables,
            'transitions': transitions,
            'timers': timers,
            'calls': calls,
            'st_code': [],
            'source_file': s.source_file,
            'source_type': 'topology_slice',
            'logic_resolution_status': 'fully_resolved',
            'evidence': {
                'anchor_id': s.anchor_element_id,
                'network_id': s.anchor_network_id,
                'unit_name': s.anchor_unit_name,
            },
        }
        steps_raw.append(step)

    return steps_raw


def merge_step_universe_with_slices(step_candidates: list, base_logic: list,
                                     slices: list) -> list:
    """Merge complete Step Universe with sliced logic.

    Ensures ALL discovered steps appear as StepGraph nodes, even if they
    have no EQ anchor (st_code, comment_only steps).

    Logic resolution status:
      - fully_resolved: EQ-anchored, slicer produced guards/actions/transitions
      - partially_resolved: EQ-anchored but slicer found partial logic
      - no_eq_anchor: st_code or comment_only step, no topology slicing
    """
    # Build slice lookup by step identity
    slice_map = {}
    for s in slices:
        key = (
            s.step_identity.get('state_variable', ''),
            s.step_identity.get('station', ''),
            s.step_identity.get('thread_idx', 0),
            s.step_identity.get('step_value', 0),
        )
        slice_map[key] = s

    # Build base_logic lookup
    logic_map = {}
    for l in base_logic:
        key = (
            l.get('state_variable', ''),
            l.get('station', ''),
            l.get('thread_idx', 0),
            l.get('step_value', l.get('value', 0)),
        )
        logic_map[key] = l

    # Build step_candidates lookup (deduplicated)
    seen_keys = set()
    merged = []

    for cand in step_candidates:
        key = (
            cand.get('state_variable', ''),
            cand.get('station', ''),
            cand.get('thread_idx', 0),
            cand.get('value', cand.get('step_value', 0)),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)

        source_type = cand.get('source_type', 'unknown')
        slc = slice_map.get(key)
        base = logic_map.get(key)

        if slc is not None:
            # Use slicer results (richest)
            step = _slice_to_step_raw(slc)
            # Determine resolution status
            has_guards = bool(slc.guards)
            has_actions = bool(slc.actions)
            has_transitions = bool(slc.transitions)
            if has_guards and has_actions and has_transitions:
                step['logic_resolution_status'] = 'fully_resolved'
            elif has_guards or has_actions or has_transitions:
                step['logic_resolution_status'] = 'partially_resolved'
            else:
                step['logic_resolution_status'] = 'partially_resolved'
            merged.append(step)
        elif base is not None:
            # Use base logic (from extract_step_logic_from_canonical)
            step = dict(base)
            step['logic_resolution_status'] = 'no_eq_anchor'
            step['source_type'] = source_type
            merged.append(step)
        else:
            # Minimal step — only discovery evidence
            step = {
                'state_variable': cand.get('state_variable', ''),
                'station': cand.get('station', ''),
                'thread_idx': cand.get('thread_idx', 0),
                'step_value': cand.get('value', cand.get('step_value', 0)),
                'comment': '',
                'actions': [],
                'guards': [],
                'common_enables': [],
                'transitions': [],
                'timers': [],
                'calls': [],
                'st_code': [],
                'source_file': cand.get('source_file', ''),
                'source_type': source_type,
                'logic_resolution_status': 'no_eq_anchor',
                'evidence': cand.get('evidence', {}),
            }
            merged.append(step)

    return merged


def _slice_to_step_raw(s) -> dict:
    """Convert a single StepLogicSlice to step_raw dict."""
    guards = []
    for g in s.guards:
        label = ('NOT ' + g.variable) if g.negated else g.variable
        if label and label not in guards:
            guards.append(label)

    common_enables = []
    for ce in s.common_enables:
        label = ('NOT ' + ce.variable) if ce.negated else ce.variable
        if label and label not in common_enables:
            common_enables.append(label)

    actions = []
    for a in s.actions:
        if a.variable and a.variable not in actions:
            actions.append(a.variable)

    calls = [{'name': c.callee} for c in s.calls]
    timers = [{'name': t.timer_type, 'pt': t.preset} for t in s.timers]

    transitions = []
    for t in s.transitions:
        ts = t.target_step or {}
        transition = {
            'target_thread': ts.get('thread', -1),
            'target_value': ts.get('value', 0),
            'source': 'topology_slice',
        }
        if t.boolean_expr:
            condition_str = _bool_dict_to_string(t.boolean_expr)
            if condition_str:
                transition['condition'] = condition_str
        # Propagate provenance (weakest-link)
        if hasattr(t, 'provenance') and t.provenance in ('ambiguous', 'validated_dataflow'):
            transition['provenance'] = t.provenance
        transitions.append(transition)

    return {
        'state_variable': s.step_identity.get('state_variable', ''),
        'station': s.step_identity.get('station', ''),
        'thread_idx': s.step_identity.get('thread_idx', 0),
        'step_value': s.step_identity.get('step_value', 0),
        'comment': s.comment or '',
        'actions': actions,
        'guards': guards,
        'common_enables': common_enables,
        'transitions': transitions,
        'timers': timers,
        'calls': calls,
        'st_code': [],
        'source_file': s.source_file,
        'source_type': 'topology_slice',
        'logic_resolution_status': 'fully_resolved',
        'evidence': {
            'anchor_id': s.anchor_element_id,
            'network_id': s.anchor_network_id,
            'unit_name': s.anchor_unit_name,
        },
    }


def _bool_dict_to_string(d: dict) -> str:
    """Convert boolean_expr dict to string."""
    if not d:
        return ''
    etype = d.get('expr_type', '')
    if etype == 'atom':
        if d.get('comparison'):
            comp = d['comparison']
            ops = comp.get('operands', [])
            op = comp.get('operator', '')
            neg = 'NOT ' if d.get('negated') else ''
            return f'{neg}{op}({", ".join(str(o) for o in ops)})'
        var = d.get('variable', '')
        neg = 'NOT ' if d.get('negated') else ''
        return f'{neg}{var}' if var else ''
    elif etype == 'not':
        inner = _bool_dict_to_string(d.get('operand', {}))
        return f'NOT ({inner})' if inner else ''
    elif etype == 'and':
        parts = [_bool_dict_to_string(c) for c in d.get('children', [])]
        parts = [p for p in parts if p]
        return ' AND '.join(parts) if parts else ''
    elif etype == 'or':
        parts = [_bool_dict_to_string(c) for c in d.get('children', [])]
        parts = [p for p in parts if p]
        return ' OR '.join(parts) if parts else ''
    elif etype == 'unknown':
        return ''
    return ''


def report_statistics(steps_raw: list, graph: StepGraph, config: dict,
                      slices: list = None):
    """Report comprehensive statistics."""
    print('\n' + '=' * 60)
    print('STEP GRAPH STATISTICS')
    print('=' * 60)

    # Basic counts
    total = len(steps_raw) or 1
    print(f'\nStep Universe: {total}')
    print(f'StepGraph Nodes: {graph.step_count()}')
    print(f'Unique edges: {graph.unique_edge_count()}')
    print(f'Total transitions: {graph.transition_count()}')

    # Resolution status
    print('\n--- Logic Resolution Status ---')
    status_counts = Counter(s.get('logic_resolution_status', 'unknown') for s in steps_raw)
    for st, count in status_counts.most_common():
        print(f'  {st}: {count} ({100*count/total:.1f}%)')

    # Source type
    print('\n--- Source Type ---')
    st_counts = Counter(s.get('source_type', 'unknown') for s in steps_raw)
    for st, count in st_counts.most_common():
        print(f'  {st}: {count}')

    # Domain distribution
    print('\n--- State Domains ---')
    domains = graph.steps_by_domain()
    for d, count in sorted(domains.items(), key=lambda x: -x[1]):
        print(f'  {d}: {count}')

    # State variable distribution
    print('\n--- State Variables ---')
    sv_counts = Counter(s['state_variable'] for s in steps_raw)
    for sv, count in sv_counts.most_common():
        print(f'  {sv}: {count}')

    # Coverage metrics (denominator = full Step Universe)
    print('\n--- Coverage (denominator = Step Universe) ---')
    has_guards = sum(1 for s in steps_raw if s.get('guards'))
    has_actions = sum(1 for s in steps_raw if s.get('actions'))
    has_transitions = sum(1 for s in steps_raw if s.get('transitions'))
    has_calls = sum(1 for s in steps_raw if s.get('calls'))
    has_timers = sum(1 for s in steps_raw if s.get('timers'))
    has_comment = sum(1 for s in steps_raw if s.get('comment'))
    has_condition = sum(1 for s in steps_raw
                        for t in s.get('transitions', [])
                        if t.get('condition'))

    print(f'  with guards: {has_guards}/{total} ({100*has_guards/total:.1f}%)')
    print(f'  with actions: {has_actions}/{total} ({100*has_actions/total:.1f}%)')
    print(f'  with transitions: {has_transitions}/{total} ({100*has_transitions/total:.1f}%)')
    print(f'  with calls: {has_calls}/{total} ({100*has_calls/total:.1f}%)')
    print(f'  with timers: {has_timers}/{total} ({100*has_timers/total:.1f}%)')
    print(f'  with comment: {has_comment}/{total} ({100*has_comment/total:.1f}%)')
    print(f'  transitions with condition: {has_condition}')

    # Ambiguous flow dependent facts
    amb_trans = sum(1 for s in steps_raw
                    for t in s.get('transitions', [])
                    if t.get('provenance') == 'ambiguous')
    print(f'  ambiguous-flow-dependent transitions: {amb_trans}')

    # Edge statistics
    print('\n--- Edge Statistics ---')
    total_outgoing = sum(len(s.get('transitions', [])) for s in steps_raw)
    avg_out = total_outgoing / total if total else 0
    print(f'  Total outgoing: {total_outgoing}')
    print(f'  Unique edges: {graph.unique_edge_count()}')
    print(f'  Avg out-degree: {avg_out:.2f}')

    # Terminal vs Unresolved Dangling
    print('\n--- Terminal vs Unresolved Dangling ---')
    dangling = graph.dangling_targets()
    terminal_edges = sum(1 for d in dangling
                         if d['to'].split('|')[-1].lstrip('-').isdigit()
                         and int(d['to'].split('|')[-1]) == 0)
    unresolved_dangling = len(dangling) - terminal_edges
    print(f'  Terminal edges (target=0): {terminal_edges}')
    print(f'  Unresolved dangling edges: {unresolved_dangling}')
    print(f'  Total: {len(dangling)}')

    # Station distribution
    print('\n--- Stations ---')
    stations = graph.steps_by_station()
    for st, count in sorted(stations.items(), key=lambda x: -x[1])[:10]:
        print(f'  {st or "(empty)"}: {count}')

    # Provenance-layered coverage (from slices)
    if slices:
        print('\n--- Provenance-Layered Coverage (individual facts) ---')
        for fact_key, label in [('guards', 'Guard'), ('actions', 'Action'),
                                ('transitions', 'Transition')]:
            prov_counts = Counter()
            for s in slices:
                for f in s.get(fact_key, []):
                    prov = f.get('provenance', 'topology_only')
                    if prov == 'topology_only':
                        prov_counts['topology_exact'] += 1
                    elif prov == 'validated_dataflow':
                        prov_counts['validated_dataflow'] += 1
                    elif prov == 'ambiguous':
                        prov_counts['ambiguous_dataflow_dependent'] += 1
                    else:
                        prov_counts[prov] += 1
            fact_total = sum(prov_counts.values()) or 1
            print(f'  {label} ({fact_total} total):')
            for cat in ['topology_exact', 'validated_dataflow',
                        'ambiguous_dataflow_dependent']:
                c = prov_counts.get(cat, 0)
                print(f'    {cat}: {c} ({100*c/fact_total:.1f}%)')


def classify_dangling(steps_raw: list, graph: StepGraph) -> list:
    """Classify dangling targets into categories.

    Categories:
      - terminal_state: target value is 0 (idle/stop/sentinel) — NOT a failure
      - other_domain: target belongs to a different state variable domain
      - target_missing_thread: target thread doesn't exist at all
      - step_discovery_missing: target thread exists but specific value not discovered
      - dead_logic: transition from a step that is itself unreachable
      - unresolved: cannot classify
    """
    dangling = graph.dangling_targets()

    # Build step universe lookup: (state_variable, thread_idx) → set of step_values
    sv_thread_values = defaultdict(set)
    sv_threads = defaultdict(set)
    for s in graph.steps:
        sv_thread_values[(s.state_variable, s.thread_idx)].add(s.step_value)
        sv_threads[s.state_variable].add(s.thread_idx)

    classifications = []
    for d in dangling:
        from_key = d['from']
        to_key = d['to']

        # Parse target key
        parts = to_key.split('|')
        target_sv = parts[0] if len(parts) > 0 else ''
        target_station = parts[1] if len(parts) > 1 else ''
        target_thread = int(parts[2]) if len(parts) > 2 and parts[2].lstrip('-').isdigit() else -1
        target_value = int(parts[3]) if len(parts) > 3 and parts[3].lstrip('-').isdigit() else 0

        # Parse source key
        src_parts = from_key.split('|')
        source_sv = src_parts[0] if src_parts else ''

        category = 'unresolved'
        reason = ''

        if target_value == 0:
            category = 'terminal_state'
            reason = 'target value is 0 (idle/stop/sentinel)'

        elif source_sv and source_sv != target_sv:
            category = 'other_domain'
            reason = f'cross-domain transition: {source_sv} -> {target_sv}'

        elif target_sv and target_thread >= 0:
            if target_thread not in sv_threads.get(target_sv, set()):
                category = 'target_missing_thread'
                reason = f'thread {target_thread} not found in {target_sv}'
            else:
                known_values = sv_thread_values.get((target_sv, target_thread), set())
                category = 'step_discovery_missing'
                reason = f'value {target_value} not in {target_sv}[{target_thread}] (known: {sorted(known_values)[:5]}...)'
        else:
            category = 'unresolved'
            reason = f'cannot classify: sv={target_sv} thread={target_thread} value={target_value}'

        classifications.append({
            'from': from_key,
            'to': to_key,
            'target_sv': target_sv,
            'target_thread': target_thread,
            'target_value': target_value,
            'category': category,
            'reason': reason,
        })

    return classifications


def main():
    args = sys.argv[1:]
    if not args:
        print('用法: python tools/build_step_graph.py <project_dir> [--config <config.json>]')
        sys.exit(1)

    project_dir = Path(args[0])
    output_dir = project_dir / 'output'
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(project_dir)
    config_path = None
    i = 1
    while i < len(args):
        if args[i] == '--config' and i + 1 < len(args):
            config_path = args[i + 1]
            i += 2
        else:
            i += 1

    if config_path:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        pp = config.get('primitive_params', {})
        for k, v in pp.items():
            if k not in config:
                config[k] = v
        strategies = [s['name'] for s in config.get('discovery_strategies', [])]
        if strategies:
            config['strategies'] = strategies

    print(f'Project: {project_dir.name}')
    print(f'Source decoder: {config.get("source_decoder", "auto-detect")}')

    # 1. Decode sources
    print('\n=== Decoding ===')
    programs = decode_sources(project_dir, config)
    total_elements = sum(sum(len(u.elements) for u in dp.program_units) for dp in programs)
    total_connections = sum(sum(len(u.connections) for u in dp.program_units) for dp in programs)
    print(f'Programs: {len(programs)}, Elements: {total_elements}, Connections: {total_connections}')

    # 2. DefUse analysis
    print('\n=== DefUse Analysis ===')
    analyzer = DefUseAnalyzer()
    all_edges = []
    for dp in programs:
        edges = analyzer.analyze([dp])
        all_edges.extend(edges)
    print(f'VariableFlowEdges: {len(all_edges)}')
    scope_counts = Counter(e.scope for e in all_edges)
    status_counts = Counter(e.resolution_status for e in all_edges)
    print(f'  by scope: {dict(scope_counts)}')
    print(f'  by status: {dict(status_counts)}')

    # 3. Discover complete Step Universe (all strategies)
    print('\n=== Step Universe Discovery ===')
    step_candidates = discover_steps_from_canonical(programs, config)
    print(f'Step candidates: {len(step_candidates)}')
    st_counts = Counter(c.get('source_type', 'unknown') for c in step_candidates)
    print(f'  by source_type: {dict(st_counts)}')

    # 4. Extract base logic for all steps (same-unit element classification)
    print('\n=== Base Logic Extraction ===')
    base_logic = extract_step_logic_from_canonical(programs, config)
    print(f'Base logic steps: {len(base_logic)}')

    # 5. Find EQ anchors for slicer
    print('\n=== Step Anchors (EQ only) ===')
    anchors = find_step_anchors(programs, config)
    print(f'EQ anchors: {len(anchors)}')

    # 6. Slice step logic (topology + dataflow + boolean)
    print('\n=== Step Logic Slicing ===')
    slicer = StepLogicSlicer(config)
    slices = slicer.slice(programs, anchors, variable_flow_edges=all_edges)

    with_guards = sum(1 for s in slices if s.guards)
    with_trans = sum(1 for s in slices if s.transitions)
    print(f'Slices: {len(slices)}')
    print(f'  with guards: {with_guards}')
    print(f'  with transitions: {with_trans}')

    # 7. Merge: complete Step Universe + sliced logic
    print('\n=== Merging Step Universe + Sliced Logic ===')
    steps_raw = merge_step_universe_with_slices(step_candidates, base_logic, slices)
    print(f'Merged steps: {len(steps_raw)}')

    # Report resolution status
    status_counts = Counter(s.get('logic_resolution_status', 'unknown') for s in steps_raw)
    print(f'  by resolution_status: {dict(status_counts)}')

    steps_raw_path = output_dir / 'steps_raw.json'
    with open(steps_raw_path, 'w', encoding='utf-8') as f:
        json.dump(steps_raw, f, ensure_ascii=False, indent=2)
    print(f'Saved steps_raw.json ({len(steps_raw)} steps)')

    # 6. Build StepGraph
    state_domains = config.get('state_domains', [])
    graph = steps_raw_to_step_graph(steps_raw, {'state_domains': state_domains})
    graph_path = output_dir / 'step_graph.json'
    graph.save(str(graph_path))
    print(f'Saved step_graph.json ({graph.unique_edge_count()} unique edges)')

    # 7. Save slices
    slices_path = output_dir / 'step_logic_slices.json'
    with open(slices_path, 'w', encoding='utf-8') as f:
        json.dump([s.to_dict() for s in slices], f, ensure_ascii=False, indent=2)
    print(f'Saved step_logic_slices.json')

    # 8. Report statistics
    slice_dicts = [s.to_dict() for s in slices]
    report_statistics(steps_raw, graph, config, slices=slice_dicts)

    # 9. Classify dangling targets
    print('\n=== Dangling Target Classification ===')
    classifications = classify_dangling(steps_raw, graph)
    cat_counts = Counter(c['category'] for c in classifications)
    for cat, count in cat_counts.most_common():
        print(f'  {cat}: {count}')
    # Save classifications
    dangling_path = output_dir / 'dangling_classifications.json'
    with open(dangling_path, 'w', encoding='utf-8') as f:
        json.dump(classifications, f, ensure_ascii=False, indent=2)
    print(f'Saved dangling_classifications.json')


if __name__ == '__main__':
    main()
