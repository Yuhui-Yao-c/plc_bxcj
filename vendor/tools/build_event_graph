#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/build_event_graph.py — 构建 EventGraph

从 step_logic_slices.json 构建 EventGraph，输出 event_graph.json。

用法:
    python tools/build_event_graph.py <project_dir>

输出:
    output/event_graph.json — 事件级工程动作关系图
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.event_graph import EventGraphBuilder


def main():
    args = sys.argv[1:]
    if not args:
        print('用法: python tools/build_event_graph.py <project_dir>')
        sys.exit(1)

    project_dir = Path(args[0])
    output_dir = project_dir / 'output'
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load config
    config_path = output_dir / 'extract_config.json'
    config = {}
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        pp = config.get('primitive_params', {})
        for k, v in pp.items():
            if k not in config:
                config[k] = v

    # Load slices
    slice_path = output_dir / 'step_logic_slices.json'
    if not slice_path.exists():
        print(f'错误: 未找到 {slice_path}')
        sys.exit(1)

    with open(slice_path, 'r', encoding='utf-8') as f:
        slices = json.load(f)
    print(f'读取: {slice_path} ({len(slices)} slices)')

    # Build EventGraph
    builder = EventGraphBuilder(config)
    graph = builder.build(slices)

    # Save
    output_path = output_dir / 'event_graph.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(graph.to_dict(), f, ensure_ascii=False, indent=2)
    print(f'保存: {output_path}')

    # Statistics
    print(f'\n=== EventGraph 统计 ===')
    print(f'  Steps: {len(graph.steps)}')
    print(f'  Commands: {len(graph.commands)}')
    print(f'  Completions: {len(graph.completions)}')
    print(f'  Waits: {len(graph.waits)}')
    print(f'  Conditions: {len(graph.conditions)}')
    print(f'  Transitions: {len(graph.transitions)}')
    print(f'  EngineeringActions: {len(graph.actions)}')

    # Match statistics
    matched = sum(1 for c in graph.completions if c.matched_command_id)
    unmatched = sum(1 for c in graph.completions if not c.matched_command_id)
    print(f'\n  命令-反馈匹配:')
    print(f'    已匹配: {matched}')
    print(f'    未匹配: {unmatched}')

    # Multi-action steps
    from collections import Counter
    action_counts = Counter()
    for a in graph.actions:
        if a.event_type == 'command':
            key = (a.step_identity.get('station', ''),
                   a.step_identity.get('thread_idx', -1),
                   a.step_identity.get('step_value', -1))
            action_counts[key] += 1

    multi = sum(1 for v in action_counts.values() if v > 1)
    print(f'\n  多命令步骤: {multi}/{len(action_counts)}')


if __name__ == '__main__':
    main()
