#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/build_evidence.py — Phase 2: 构建证据库

用法:
    python tools/build_evidence.py <project_dir> [--plc <path>] [--hmi <path>] [--manifest <path>]

输入:
    --plc  : PLC 源码路径 (文件或目录)
    --hmi  : HMI 工程路径 (文件或目录)
    --manifest : resource_manifest.json (由 discover_resources.py 生成)
    如果不提供 --plc/--hmi，从 manifest 或 project_dir/source 读取。

输出:
    output/evidence_store.json — 所有实体和关系

opencode 读取此输出后:
    - 检查变量有无 documentation / HMI标签 / FB调用链
    - 发现缺失: 哪些变量没有中文? 哪些没有工站?
    - 记录结论到 output/analysis_notes.md
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.evidence_builder import build_evidence_store
from lib.decoders import HMIDecoder


def main():
    args = sys.argv[1:]
    if not args:
        print('用法: python tools/build_evidence.py <project_dir> [--plc <path>] [--hmi <path>] [--manifest <path>]')
        sys.exit(1)

    project_dir = Path(args[0])
    output_dir = project_dir / 'output'
    output_dir.mkdir(parents=True, exist_ok=True)

    plc_path = None
    hmi_path = None
    manifest_path = None

    i = 1
    while i < len(args):
        if args[i] == '--plc' and i + 1 < len(args):
            plc_path = args[i + 1]
            i += 2
        elif args[i] == '--hmi' and i + 1 < len(args):
            hmi_path = args[i + 1]
            i += 2
        elif args[i] == '--manifest' and i + 1 < len(args):
            manifest_path = args[i + 1]
            i += 2
        else:
            i += 1

    # 从 manifest 读取路径
    if manifest_path:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        if not plc_path and manifest.get('plc_source'):
            # 取最大的 PLC 源码文件
            plc_entries = manifest['plc_source']
            plc_path = plc_entries[0]['path']
        if not hmi_path and manifest.get('hmi_project'):
            hmi_path = manifest['hmi_project'][0]['path']

    # 回退: project_dir/source
    if not plc_path:
        source_dir = project_dir / 'source'
        if source_dir.exists():
            xml_files = list(source_dir.glob('*.xml'))
            if xml_files:
                plc_path = str(xml_files[0])

    if not plc_path:
        print('错误: 未找到 PLC 源码文件 (用 --plc 或 --manifest 指定)')
        sys.exit(1)

    if not hmi_path:
        hmi_dir = project_dir / 'hmi'
        if hmi_dir.exists():
            hmi_path = HMIDecoder.find_hmi_project(str(hmi_dir))

    print(f'PLC: {plc_path}')
    print(f'HMI: {hmi_path or "(无)"}')

    # Load evidence_config from extract_config.json (Program Pattern Spec)
    # This contains project-level patterns (station_code_pattern, etc.)
    # that were moved out of source_schema.json to respect Spec boundaries.
    extractor_config = None
    config_path = output_dir / 'extract_config.json'
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            spec = json.load(f)
        extractor_config = spec.get('evidence_config')
        if extractor_config:
            print(f'Evidence config: loaded from extract_config.json')

    store = build_evidence_store(plc_path, hmi_path,
                                  extractor_config=extractor_config)

    output_path = output_dir / 'evidence_store.json'
    store.save(str(output_path))

    stats = store.stats()
    print(f'\n输出: {output_path}')
    print(f'  实体: {stats["total_entities"]}')
    print(f'  关系: {stats["total_relations"]}')
    print(f'  实体类型:')
    for t, c in sorted(stats['entities_by_type'].items(), key=lambda x: -x[1]):
        print(f'    {t}: {c}')
    print(f'  关系类型:')
    for t, c in sorted(stats['relations_by_type'].items(), key=lambda x: -x[1]):
        print(f'    {t}: {c}')


if __name__ == '__main__':
    main()
