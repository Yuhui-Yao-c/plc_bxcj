#!/usr/bin/env python3
"""Phase 5: 生成正式 steps_described.json (v4 — spec-driven + evidence levels)

v4 改进:
  1. Spec-driven: reads project_semantic_spec.json (datatype families, command conventions, doc patterns, roles)
  2. Evidence levels: exact_source_fact / validated_structural_semantic / contextual_semantic / unresolved
  3. Validated aliases only: reads validated_aliases.json
  4. Role classification: physical_actuator_command / process_command / internal_logic / status_flag / alarm / routing_control / unknown
  5. Hardened comment validator: consistent requires actor+operation real match
  6. Per-evidence-level reporting
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.evidence_model import EvidenceStore, Entity, Relation, load_store
from lib.action_semantic_resolver import ActionSemanticResolver, parse_expression
from lib.doc_decomposer import DocumentationDecomposer
from lib.evidence_levels import (EXACT_SOURCE_FACT, VALIDATED_STRUCTURAL_SEMANTIC,
                                  PLAUSIBLE_STRUCTURAL_HYPOTHESIS,
                                  CONTEXTUAL_SEMANTIC, UNRESOLVED)


def load_json(path):
    p = Path(path)
    if not p.exists():
        return None
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f)


def clean_comment(comment):
    if not comment:
        return ''
    lines = comment.split('\n')
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        line = re.sub(r'^\d+\s*', '', line)
        line = re.sub(r'^=+\s*', '', line)
        line = re.sub(r'\s*=+$', '', line)
        if line:
            cleaned.append(line)
    return ' '.join(cleaned) if cleaned else ''


def classify_comment_vs_fact(comment, fact):
    """Classify relationship between PLC comment and ActionSemanticFact — HARDENED.

    Returns: consistent | semantic_fact_more_specific | comment_more_specific | conflict | insufficient

    Hardened rules:
      - consistent: requires actor OR operation to actually match (not just both non-empty)
      - conflict: actor or operation explicitly contradicts
      - insufficient: not enough information to compare
    """
    clean_comm = clean_comment(comment)
    if not clean_comm:
        return 'insufficient'

    actor = fact.get('actor', {})
    operation = fact.get('operation', {})

    actor_val = str(actor.get('value', '')) if actor.get('resolved') else ''
    op_val = str(operation.get('value', '')) if operation.get('resolved') else ''

    has_actor = bool(actor_val)
    has_op = bool(op_val)

    if not has_actor and not has_op:
        return 'insufficient'

    # Check actor match — must find actual actor name in comment
    actor_match = False
    if has_actor:
        # Try to find actor value (or significant parts) in comment
        actor_parts = [p.strip() for p in re.split(r'[\s\d#]+', actor_val) if len(p.strip()) > 1]
        for part in actor_parts:
            if part in clean_comm:
                actor_match = True
                break

    # Check operation match
    op_match = False
    if has_op:
        op_parts = [p.strip() for p in re.split(r'[\s\d#]+', op_val) if len(p.strip()) > 1]
        for part in op_parts:
            if part in clean_comm:
                op_match = True
                break

    if actor_match or op_match:
        return 'consistent'

    # Check for conflict: if actor is a physical object and comment mentions a DIFFERENT physical object
    # This is hard to detect without semantic understanding, so we check if comment
    # mentions a different actuator type keyword
    if has_actor:
        # Simple conflict check: if actor contains "气缸" but comment doesn't mention any "气缸"
        # while mentioning "电机" instead, that's a potential conflict
        actor_has_cylinder = '气缸' in actor_val
        actor_has_motor = '电机' in actor_val or '马达' in actor_val
        comm_has_cylinder = '气缸' in clean_comm
        comm_has_motor = '电机' in clean_comm or '马达' in clean_comm

        if actor_has_cylinder and comm_has_motor and not comm_has_cylinder:
            return 'conflict'
        if actor_has_motor and comm_has_cylinder and not comm_has_motor:
            return 'conflict'

    # If fact has more detail
    if has_actor and has_op and len(clean_comm) < 10:
        return 'semantic_fact_more_specific'

    # If comment has more detail
    if len(clean_comm) > 30 and not has_op:
        return 'comment_more_specific'

    return 'insufficient'


def test_comment_validator():
    """Test the comment validator with synthetic examples."""
    tests = [
        # (comment, fact, expected)
        ('压保护盖Z轴下降', {'actor': {'value': '压保护盖Z轴', 'resolved': True},
                          'operation': {'value': '下降', 'resolved': True}}, 'consistent'),
        ('1#气缸伸出', {'actor': {'value': '1#气缸', 'resolved': True},
                      'operation': {'value': '伸出', 'resolved': True}}, 'consistent'),
        ('电机正转', {'actor': {'value': '气缸', 'resolved': True},
                    'operation': {'value': '伸出', 'resolved': True}}, 'conflict'),
        ('', {'actor': {'value': '气缸', 'resolved': True}}, 'insufficient'),
        ('执行动作', {'actor': {'value': '', 'resolved': False},
                   'operation': {'value': '', 'resolved': False}}, 'insufficient'),
        ('板链线移动到下一个位置', {'actor': {'value': '', 'resolved': False},
                                'operation': {'value': '', 'resolved': False}}, 'insufficient'),
    ]

    results = []
    for comment, fact, expected in tests:
        actual = classify_comment_vs_fact(comment, fact)
        passed = actual == expected
        results.append(passed)
        if not passed:
            print('  FAIL: comment="{}" | expected={} | actual={}'.format(comment, expected, actual))

    passed_count = sum(results)
    print('Comment validator test: {}/{} passed'.format(passed_count, len(tests)))
    return passed_count == len(tests)


def check_hmi_reachable(store, fact, resolver=None):
    """Check HMI reachability for an action fact."""
    chain = fact.get('traversal_chain', [])
    for step in chain:
        entity_id = step.get('entity_id', '')
        if not entity_id:
            continue
        if resolver:
            for r in resolver._relations_from_index.get((entity_id, 'maps_to'), []):
                addr = store.get_entity(r.target_id)
                if addr:
                    for r2 in resolver._relations_from_index.get((addr.entity_id, 'labelled_as'), []):
                        label = store.get_entity(r2.target_id)
                        if label:
                            return {'reachable': True, 'level': 'exact',
                                    'path': [step.get('name', ''), addr.key, label.key]}
            for r in resolver._relations_to_index.get((entity_id, 'binds_to'), []):
                comp = store.get_entity(r.source_id)
                if comp:
                    return {'reachable': True, 'level': 'structural',
                            'path': [step.get('name', ''), comp.key]}
        else:
            entity = store.get_entity(entity_id)
            if not entity:
                continue
            for r in store.find_relations_from(entity.entity_id, 'maps_to'):
                addr = store.get_entity(r.target_id)
                if addr:
                    for r2 in store.find_relations_from(addr.entity_id, 'labelled_as'):
                        label = store.get_entity(r2.target_id)
                        if label:
                            return {'reachable': True, 'level': 'exact',
                                    'path': [entity.key, addr.key, label.key]}
            for r in store.find_relations_to(entity.entity_id, 'binds_to'):
                comp = store.get_entity(r.source_id)
                if comp:
                    return {'reachable': True, 'level': 'structural',
                            'path': [entity.key, comp.key]}

    for step in chain:
        if step.get('documentation'):
            return {'reachable': False, 'level': 'station_only',
                    'path': [step.get('name', '')]}

    return {'reachable': False, 'level': 'unreachable', 'path': []}


def describe_verified_fb_call(calls: list) -> str:
    """Conservative, vendor-independent call fallback with explicit parameters.

    Only describe the operation implied by a recognized FB name. A call alone
    does not prove a requested command has executed or its feedback is true.
    """
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        callee = str(call.get('name') or call.get('callee') or '').strip()
        params = {}
        for inp in call.get('inputs', []):
            if isinstance(inp, dict) and inp.get('name'):
                params[str(inp['name']).lower()] = str(inp.get('value', '')).strip()
        if callee.lower() in ('axismove', 'movelinear'):
            axis = params.get('_axis') or params.get('axis')
            target = params.get('_position') or params.get('position')
            if axis and target:
                return f'{axis} 运动至目标位置参数{target}'
            if axis:
                return f'{axis} 执行{callee}运动'
    return ''


def build_description_from_fact(fact, comment, station_name, calls=None):
    """Build description from ActionSemanticFact + Comment + Context."""
    clean_comm = clean_comment(comment)
    if clean_comm:
        if station_name:
            return '{}: {}'.format(station_name, clean_comm), 'plc_comment'
        return clean_comm, 'plc_comment'

    fb_desc = describe_verified_fb_call(calls or [])
    if fb_desc:
        return ((station_name + ': ') if station_name else '') + fb_desc, 'fb_call'

    parts = []
    if station_name:
        parts.append(station_name)
    actor = fact.get('actor', {})
    operation = fact.get('operation', {})
    direction = fact.get('direction_state', {})
    position_index = fact.get('position_index', {})

    if actor.get('resolved') and actor.get('value'):
        parts.append(str(actor['value']))
    if operation.get('resolved') and operation.get('value'):
        parts.append(str(operation['value']))
    elif direction.get('resolved') and direction.get('value'):
        parts.append(str(direction['value']))
    if position_index.get('resolved') and position_index.get('value') is not None:
        parts.append('位置{}'.format(position_index['value']))

    if len(parts) >= 2:
        return ': '.join([parts[0], ' '.join(parts[1:])]), 'semantic_fact'
    if parts:
        return parts[0], 'partial_fact'
    return (station_name + ': 执行动作') if station_name else '执行动作', 'fallback'


def main():
    args = sys.argv[1:]
    if not args:
        print('用法: python tools/describe_steps.py <project_dir>')
        print('示例: python tools/describe_steps.py projects/转接片')
        sys.exit(1)

    project_dir = Path(args[0])
    output_dir = project_dir / 'output'

    # Load all spec components
    spec = load_json(output_dir / 'project_semantic_spec.json') or {}
    validated_aliases = load_json(output_dir / 'validated_aliases.json') or {}
    command_hypotheses = load_json(output_dir / 'command_hypothesis_lookup.json') or {}

    store = load_store(str(output_dir / 'evidence_store.json'))
    steps = json.load(open(output_dir / 'steps_raw.json', 'r', encoding='utf-8'))

    # Create spec-driven decomposer
    doc_decomposer = DocumentationDecomposer(spec=spec)

    # Create spec-driven resolver with validated aliases
    resolver = ActionSemanticResolver(
        store, doc_decomposer=doc_decomposer,
        command_hypotheses=command_hypotheses,
        spec=spec, validated_aliases=validated_aliases)

    # Test comment validator
    print('=== Comment Validator Test ===')
    test_comment_validator()
    print()

    # Collect all unique action variables
    action_counter = Counter()
    for s in steps:
        for a in s.get('actions', []):
            action_counter[a] += 1
    unique_vars = list(action_counter.keys())
    print('Unique action variables: {}'.format(len(unique_vars)))

    # Batch resolve
    batch_result = resolver.resolve_batch(unique_vars)
    print('Resolution flags:')
    for flag, count in batch_result['resolution_flags'].items():
        print('  {}: {}/{} ({:.1f}%)'.format(
            flag, count, len(unique_vars), count / len(unique_vars) * 100))

    resolution_map = {fact.raw_action: fact for fact in batch_result['facts']}

    # Generate described steps
    described = []
    desc_source_counter = Counter()
    comment_classification_counter = Counter()
    role_counter = Counter()
    evidence_level_counter = defaultdict(Counter)  # field -> {level: count}
    hmi_by_role = defaultdict(Counter)

    for s in steps:
        sv = s.get('state_variable', '')
        t = s.get('thread_idx', 0)
        v = s.get('step_value', 0)
        station_code = s.get('station', '')
        comment = s.get('comment', '') or ''
        actions = s.get('actions', [])

        semantic_facts = []
        step_roles = []

        for var in actions[:10]:
            fact = resolution_map.get(var)
            if not fact:
                continue
            fact_dict = fact.to_dict()
            hmi = check_hmi_reachable(store, fact_dict, resolver)
            fact_dict['hmi_reachable'] = hmi

            role = fact_dict.get('action_role', {}).get('value', 'unknown') or 'unknown'
            step_roles.append(role)
            role_counter[role] += 1
            hmi_by_role[role][hmi['level']] += 1

            comment_class = classify_comment_vs_fact(comment, fact_dict)
            comment_classification_counter[comment_class] += 1

            # Track evidence levels per field
            for fname in ['station', 'actor', 'actor_type', 'operation',
                          'direction_state', 'target', 'position_index',
                          'position_name', 'command_member', 'action_role']:
                fe = fact_dict.get(fname, {})
                level = fe.get('evidence_level', UNRESOLVED)
                evidence_level_counter[fname][level] += 1

            semantic_facts.append(fact_dict)

        primary_role = step_roles[0] if step_roles else 'unknown'
        primary_fact = None
        for sf in semantic_facts:
            if sf.get('actor', {}).get('resolved'):
                primary_fact = sf
                break
        if not primary_fact and semantic_facts:
            primary_fact = semantic_facts[0]

        station_name = station_code
        for e in store.entities.values():
            if e.entity_type == 'station_thread' and e.key == station_code:
                for r_idx in resolver._relations_from_index.get((e.entity_id, 'named_as'), []):
                    tgt = store.get_entity(r_idx.target_id)
                    if tgt and tgt.entity_type == 'documentation_text':
                        station_name = tgt.key
                        break
                break

        description, desc_source = build_description_from_fact(
            primary_fact or {}, comment, station_name, s.get('calls', []))
        desc_source_counter[desc_source] += 1

        described.append({
            'step_identity': {
                'state_variable': sv,
                'thread_idx': t,
                'step_value': v,
                'station': station_code,
            },
            'action_semantic_facts': semantic_facts,
            'description': description,
            'description_source': desc_source,
            'comment_cross_evidence': {
                'comment': clean_comment(comment),
                'classification': classify_comment_vs_fact(comment, primary_fact or {}),
            },
            'primary_action_role': primary_role,
            'generation_provenance': {
                'method': desc_source,
                'resolver_version': 'v4_spec_driven',
                'spec_version': spec.get('spec_version', 'unknown'),
            },
        })

    # Save
    output_path = output_dir / 'steps_described.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(described, f, ensure_ascii=False, indent=2)

    # ========== Stats ==========
    total_facts = sum(len(d.get('action_semantic_facts', [])) for d in described)
    print()
    print('Output: {}'.format(output_path))
    print('  Total steps: {}'.format(len(described)))
    print('  Total action facts: {}'.format(total_facts))

    print()
    print('  === Evidence Level Breakdown ===')
    for fname in ['station', 'actor', 'actor_type', 'operation',
                   'direction_state', 'position_index', 'position_name',
                   'command_member', 'action_role']:
        levels = evidence_level_counter[fname]
        total = sum(levels.values())
        print('  {}:'.format(fname))
        for level in [EXACT_SOURCE_FACT, VALIDATED_STRUCTURAL_SEMANTIC,
                       PLAUSIBLE_STRUCTURAL_HYPOTHESIS,
                       CONTEXTUAL_SEMANTIC, UNRESOLVED]:
            count = levels.get(level, 0)
            print('    {}: {} ({:.1f}%)'.format(level, count, count / total * 100 if total else 0))

    print()
    print('  === Operation Evidence Breakdown ===')
    op_levels = evidence_level_counter['operation']
    op_total = sum(op_levels.values())
    for level in [EXACT_SOURCE_FACT, VALIDATED_STRUCTURAL_SEMANTIC,
                   PLAUSIBLE_STRUCTURAL_HYPOTHESIS,
                   CONTEXTUAL_SEMANTIC, UNRESOLVED]:
        count = op_levels.get(level, 0)
        print('    {}: {} ({:.1f}%)'.format(level, count, count / op_total * 100 if op_total else 0))

    print()
    print('  === Actor Evidence Breakdown ===')
    actor_levels = evidence_level_counter['actor']
    actor_total = sum(actor_levels.values())
    for level in [EXACT_SOURCE_FACT, VALIDATED_STRUCTURAL_SEMANTIC,
                   PLAUSIBLE_STRUCTURAL_HYPOTHESIS,
                   CONTEXTUAL_SEMANTIC, UNRESOLVED]:
        count = actor_levels.get(level, 0)
        print('    {}: {} ({:.1f}%)'.format(level, count, count / actor_total * 100 if actor_total else 0))

    print()
    print('  === Action Role Distribution ===')
    for role, count in role_counter.most_common():
        print('    {}: {} ({:.1f}%)'.format(role, count, count / total_facts * 100))

    print()
    print('  === HMI by Action Role ===')
    for role in sorted(hmi_by_role.keys()):
        levels = hmi_by_role[role]
        total = sum(levels.values())
        print('    {} ({} actions):'.format(role, total))
        for level in ['exact', 'structural', 'station_only', 'unreachable']:
            c = levels.get(level, 0)
            print('      {}: {} ({:.1f}%)'.format(level, c, c / total * 100 if total else 0))

    print()
    print('  === Comment Cross-Evidence ===')
    for cls, count in comment_classification_counter.most_common():
        print('    {}: {}'.format(cls, count))

    print()
    print('  === Description Sources ===')
    for src, count in desc_source_counter.most_common():
        print('    {}: {} ({:.1f}%)'.format(src, count, count / len(described) * 100))


if __name__ == '__main__':
    main()
