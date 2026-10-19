#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bxcj_demo/lib/transition_filter.py — P2 纯转移步过滤

分类：
  A: 纯内部状态（无命令/无等待/无调用，仅状态转移） → 从主表隐藏
  B: 真实设备动作（有命令匹配 interface_spec） → 保留
  C: 必要等待与同步（有 wait 事件或跨工站等待） → 保留
  D: 无法确定 → 保留 + 标黄

输入：end_tag_decisions.json
输出：filtered_actions.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path


class TransitionFilter:
    """对结束标签决策后的动作做过滤分类。"""

    def __init__(self, output_dir: Path, cfg: dict = None):
        self.output_dir = output_dir
        self.cfg = cfg or {}

    def _classify(self, action: dict) -> str:
        """返回分类：A/B/C/D。"""
        action_type = action.get('action_type', 'transition')
        commands = action.get('commands', [])
        completions = action.get('completions', [])
        timing_intent = action.get('timing_intent', '')
        review_reason = action.get('review_reason_code', '')

        # B: 真实设备动作
        if action_type in ('motion', 'clamp', 'vacuum', 'action') and commands:
            return 'B'

        # C: 等待/同步
        if action_type == 'wait':
            return 'C'

        # A: 纯内部状态（transition 且无命令无等待）
        if action_type == 'transition' and not commands:
            # 进一步检查是否有 call（FB 调用）
            # 这里简化：纯 transition 都归为 A
            return 'A'

        # 有命令但 action_type=transition（异常情况）
        if commands:
            return 'B'

        # D: 无法确定
        return 'D'

    def run(self) -> bool:
        in_path = self.output_dir / 'end_tag_decisions.json'
        if not in_path.exists():
            print(f'  [TransitionFilter] 输入不存在: {in_path}')
            return False

        with open(in_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        actions = data.get('actions', [])
        print(f'  [TransitionFilter] 输入 {len(actions)} 个动作')

        results = []
        for a in actions:
            category = self._classify(a)
            results.append({
                **a,
                'filter_category': category,
                'in_main_table': category != 'A',  # A 类不进主表
            })

        # 统计
        cat_counts = Counter(r['filter_category'] for r in results)
        in_main = sum(1 for r in results if r['in_main_table'])
        print(f'  [TransitionFilter] 分类: {dict(cat_counts)}')
        print(f'  [TransitionFilter] 主表动作: {in_main}/{len(results)}')

        out_path = self.output_dir / 'filtered_actions.json'
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump({
                'actions': results,
                'stats': {
                    'total': len(results),
                    'in_main_table': in_main,
                    'filtered_out': len(results) - in_main,
                    'by_category': dict(cat_counts),
                }
            }, f, ensure_ascii=False, indent=2)
        print(f'  [TransitionFilter] 输出: {out_path}')
        return True
