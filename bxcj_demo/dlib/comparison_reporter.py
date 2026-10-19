#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bxcj_demo/lib/comparison_reporter.py — P7 独立人工对比报告

人工 Gold 隔离原则：
  - 不在生产 pipeline 任何阶段加载人工表
  - 仅在采集表冻结后独立加载做对照
  - 对照结果不进入主表
  - 输出独立 markdown 报告
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


class ComparisonReporter:
    """生成与人工 Gold 的对比报告。"""

    def __init__(self, work_output_dir: Path, demo_output_dir: Path,
                 cfg: dict = None):
        self.work_output_dir = work_output_dir
        self.demo_output_dir = demo_output_dir
        self.cfg = cfg or {}
        self.project_name = self.cfg.get('project', {}).get('name', 'unknown')
        self.display_name = self.cfg.get('project', {}).get('display_name', '')
        self.manual_table_path = self.cfg.get('project', {}).get('manual_table', '')

    def _load_manual_steps(self) -> list:
        """加载人工 Gold 表。"""
        if not self.manual_table_path:
            return []
        repo_root = Path(self.cfg.get('_repo_root', '.'))
        p = Path(self.manual_table_path)
        if not p.is_absolute():
            p = repo_root / self.manual_table_path
        if not p.exists():
            print(f'  [Comparison] 人工表不存在: {p}')
            return []

        print(f'  [Comparison] 加载人工表: {p.name}')
        steps = []
        ext = p.suffix.lower()
        try:
            if ext == '.xls' or ext == '.xlsx':
                import xlrd  # type: ignore
                use_xlrd = ext == '.xls'
                if use_xlrd:
                    wb = xlrd.open_workbook(str(p))
                    sh = wb.sheet_by_index(0)
                    for r in range(1, sh.nrows):
                        row = [str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)]
                        steps.append(row)
                else:
                    from openpyxl import load_workbook
                    wb = load_workbook(str(p), read_only=True)
                    sh = wb.active
                    for r in sh.iter_rows(min_row=2, values_only=True):
                        steps.append([str(c).strip() if c is not None else '' for c in r])
        except Exception as e:
            print(f'  [Comparison] 加载失败: {e}')
        return steps

    def _load_auto_actions(self) -> list:
        """加载已冻结的自动采集表。"""
        p = self.demo_output_dir / 'evidence.json'
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f).get('sample_actions', [])
        return []

    def _load_filtered_actions(self) -> list:
        """加载 filtered_actions.json。"""
        p = self.work_output_dir / 'filtered_actions.json'
        if not p.exists():
            return []
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f).get('actions', [])

    def run(self) -> bool:
        print('  [Comparison] 开始独立对照...')
        manual_steps = self._load_manual_steps()
        auto_actions = self._load_filtered_actions()
        auto_main = [a for a in auto_actions if a.get('in_main_table', True)]

        # 统计
        stats = {
            'project': self.project_name,
            'display_name': self.display_name,
            'manual_step_count': len(manual_steps),
            'auto_action_count': len(auto_main),
            'auto_total_actions': len(auto_actions),
            'filtered_out_transitions': len(auto_actions) - len(auto_main),
        }

        # 颜色分布
        from collections import Counter
        evidence_counts = Counter(a.get('evidence_level', '') for a in auto_main)
        type_counts = Counter(a.get('action_type', '') for a in auto_main)
        intent_counts = Counter(a.get('timing_intent', '') for a in auto_main)
        review_counts = Counter(a.get('review_reason_code', '') for a in auto_main
                                if a.get('review_reason_code'))

        stats['auto_evidence_distribution'] = dict(evidence_counts)
        stats['auto_type_distribution'] = dict(type_counts)
        stats['auto_timing_intent_distribution'] = dict(intent_counts)
        stats['auto_review_reasons'] = dict(review_counts)

        # 写 markdown 报告
        report = f"""# {self.display_name} 人工差异分析报告

> 本报告在自动采集表冻结后独立生成，人工 Gold 未进入任何生产环节。

## 1. 基本统计

| 指标 | 值 |
|---|---|
| 项目 | {self.display_name} |
| 人工 Gold 步数 | {stats['manual_step_count']} |
| 自动主表动作数 | {stats['auto_action_count']} |
| 自动总动作数（含过滤） | {stats['auto_total_actions']} |
| 过滤为纯转移步 | {stats['filtered_out_transitions']} |

## 2. 自动动作分布

### 按动作类型
"""
        for k, v in type_counts.most_common():
            report += f'- {k or "(空)"}: {v}\n'

        report += "\n### 按计时意图\n"
        for k, v in intent_counts.most_common():
            report += f'- {k or "(空)"}: {v}\n'

        report += "\n### 按证据等级\n"
        for k, v in evidence_counts.most_common():
            report += f'- {k or "(空)"}: {v}\n'

        report += "\n## 3. 黄色待复核原因分布\n"
        if review_counts:
            from dlib.demo_exporter import REVIEW_REASON_DESC
            for k, v in review_counts.most_common():
                desc = REVIEW_REASON_DESC.get(k, k)
                report += f'- {k} ({desc}): {v}\n'
        else:
            report += '- 无待复核项\n'

        report += f"""

## 4. 差异类别归纳

按 P7 规范分类：

| 类别 | 数量 | 说明 |
|---|---|---|
| 真正漏提 | - | 自动未发现而人工有的步序 |
| 真实额外动作 | - | 自动发现而人工未记录 |
| 合理辅助状态 | {stats['filtered_out_transitions']} | 纯转移步被过滤，不影响主表 |
| 状态转移口径 | - | 结束标签使用业务后继状态而非完成信号 |
| 内部Step合并 | - | 自动按动作跨度合并 |
| 命令反馈关联错误 | - | 命令与完成信号匹配错误 |
| 中文语义错误 | - | 描述缺少必要依据 |
| 源码版本差异 | - | 源码与人工表版本不同 |
| Trace不匹配 | - | Trace 信号缺失或版本不匹配 |
| 无法裁决 | - | 信息不足以下结论 |

## 5. 主要观察

"""
        # 自动添加观察
        if stats['filtered_out_transitions'] > 0:
            report += f"- 已过滤 {stats['filtered_out_transitions']} 个纯转移步（A类），这些步序仅做内部状态传递，无独立计时意义。\n"
        if review_counts:
            top_reason = review_counts.most_common(1)[0]
            report += f"- 主要待复核原因：{top_reason[0]}（{top_reason[1]} 项）。\n"
        if not manual_steps:
            report += "- 人工表未加载或为空，无法做步序级对照。\n"

        report += """
## 6. 结论

本轮 Demo 输出以"业务状态转移"为默认计时口径，对纯转移步做了合理过滤，
对不确定项标黄并给出复核原因码。详细差异需结合具体工位逐行核对。

人工 Gold 仅作为独立评价参考，未参与任何生产决策。
"""

        # 写文件
        out_path = self.demo_output_dir.parent / 'reports' / f'{self.project_name}_comparison.md'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f'  [Comparison] 输出: {out_path}')

        # 保存 JSON 统计
        stats_path = self.demo_output_dir / 'comparison_stats.json'
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        return True
