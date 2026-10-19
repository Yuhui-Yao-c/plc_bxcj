#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dlib/action_span.py — ActionTemplate / ActionInstance / EngineeringActionSpan（v10.38 P4）

一个工程动作可以覆盖：
  - 单个 Step
  - 多个内部 Step（transition-only steps aggregated）
  - 多个并行执行机构
  - 多个条件状态
  - 多个 A/B 执行实例
  - 一个完整的复合工艺片段

本模块判断：
  1. 哪个状态实际启动动作
  2. 哪些中间状态仅传递控制或等待反馈
  3. 该动作对应哪组机构及命令
  4. 什么时候可以认为动作完成
  5. 是否与另一个动作并行
  6. 是否具备独立计时价值
  7. 是否应进入主采集表

输出：
  - ActionTemplate（业务动作模板，可含多实例）
  - ActionInstance（A/B 或单实例）
  - EngineeringActionSpan（具体计时区间，含 CoveredSteps / CommandEvidence / CompletionEvidence）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from collections import defaultdict


@dataclass
class CommandEvidence:
    """命令证据——PLC 源码中实际发出的执行机构命令。"""
    variable: str
    command_type: str = ""
    actuator_path: str = ""
    interface_dir: str = ""
    element_id: str = ""
    unit_name: str = ""
    network_id: str = ""
    provenance: str = ""
    is_interface_cmd: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompletionEvidence:
    """完成反馈证据——PLC 源码中检测到的动作完成信号。"""
    variable: str
    feedback_type: str = ""
    matched_command_id: str = ""
    match_confidence: str = ""
    element_id: str = ""
    unit_name: str = ""
    provenance: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CoveredStep:
    """ActionSpan 覆盖的单个 Step。"""
    step_identity: dict
    role: str = "intermediate"
    is_start: bool = False
    is_end: bool = False
    is_pure_transition: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActionTemplate:
    """业务动作模板。

    一个模板对应一个工程意义的业务动作（如 "BTH17下降取胶"），
    可包含多个 ActionInstance（A/B 双实例、不同 thread 等）。
    """
    template_id: str
    process_id: str
    business_description: str = ""
    mechanism: str = ""
    operation: str = ""
    target_position: str = ""
    operation_direction: str = ""
    stroke: str = ""
    instance_ids: list = field(default_factory=list)
    source_provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActionInstance:
    """动作实例——模板的一次具体执行。

    A/B 双实例时，instance_label 为 "A" 或 "B"。
    单实例时为 "single"。
    """
    instance_id: str
    template_id: str
    instance_label: str = "single"
    step_identity: dict = field(default_factory=dict)
    thread_idx: int = -1
    station: str = ""
    start_label: str = ""
    end_label: str = ""
    timing_intent: str = "UNRESOLVED"
    source_provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EngineeringActionSpan:
    """工程动作计时区间。

    一个 Span 是 ActionInstance 的具体计时实现：
      - CoveredSteps: 该 Span 覆盖的所有内部 Step
      - CommandEvidence: 启动该动作的命令证据
      - CompletionEvidence: 完成该动作的反馈证据
      - TimingIntent: 计时意图（来自 TimingIntentSpec）
      - selected_start / selected_end: 唯一选定的起止标签
      - alternative_starts / alternative_ends: 备选边界（进入证据表，不扩张主表）
      - parallel_spans: 并行执行的 Span ID 列表
      - has_independent_timing_value: 是否具备独立计时价值（决定是否进入主表）
    """
    span_id: str
    template_id: str
    instance_id: str
    step_identity: dict = field(default_factory=dict)
    covered_steps: list = field(default_factory=list)
    command_evidence: list = field(default_factory=list)
    completion_evidence: list = field(default_factory=list)
    timing_intent: str = "UNRESOLVED"
    selected_start: str = ""
    selected_end: str = ""
    alternative_starts: list = field(default_factory=list)
    alternative_ends: list = field(default_factory=list)
    parallel_spans: list = field(default_factory=list)
    has_independent_timing_value: bool = True
    in_main_table: bool = True
    exclusion_reason: str = ""
    source_provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'span_id': self.span_id,
            'template_id': self.template_id,
            'instance_id': self.instance_id,
            'step_identity': self.step_identity,
            'covered_steps': [cs.to_dict() if isinstance(cs, CoveredStep) else cs for cs in self.covered_steps],
            'command_evidence': [ce.to_dict() if isinstance(ce, CommandEvidence) else ce for ce in self.command_evidence],
            'completion_evidence': [ce.to_dict() if isinstance(ce, CompletionEvidence) else ce for ce in self.completion_evidence],
            'timing_intent': self.timing_intent,
            'selected_start': self.selected_start,
            'selected_end': self.selected_end,
            'alternative_starts': self.alternative_starts,
            'alternative_ends': self.alternative_ends,
            'parallel_spans': self.parallel_spans,
            'has_independent_timing_value': self.has_independent_timing_value,
            'in_main_table': self.in_main_table,
            'exclusion_reason': self.exclusion_reason,
            'source_provenance': self.source_provenance,
        }


class ActionSpanBuilder:
    """从 ProcessModel + EventGraph 构建 ActionSpan 列表。

    工程师一般不会独立采集所有内部赋值、预留、计算和跳转。
    但不能因为某个 Step 被分类为 transition 就删掉真实电机、气缸、等待和工艺动作。

    算法：
      1. 遍历 ProcessModel.normal_path 的所有 Step
      2. 为每个 Step 关联 EventGraph 中的 commands/completions/conditions
      3. 聚合连续的 pure-transition steps 到前一个有命令的 Span
      4. 判断每个 Span 的 TimingIntent
      5. 检测 A/B 双实例并创建 ActionTemplate
      6. 标记 has_independent_timing_value
    """

    def __init__(self, project: str, cfg: dict = None):
        self.project = project
        self.cfg = cfg or {}

    def build(self, process_models: list, event_graph: dict) -> tuple:
        """返回 (templates, instances, spans)。"""
        commands = event_graph.get('commands', [])
        completions = event_graph.get('completions', [])
        conditions = event_graph.get('conditions', [])
        waits = event_graph.get('waits', [])
        transitions = event_graph.get('transitions', [])

        cmd_by_step = self._indexByStep(commands)
        comp_by_step = self._indexByStep(completions)
        cond_by_step = self._indexByStep(conditions)
        wait_by_step = self._indexByStep(waits)
        trans_by_step = self._indexByStep(transitions)

        templates = []
        instances = []
        spans = []
        span_counter = 0
        tmpl_counter = 0
        inst_counter = 0

        for pm in process_models:
            normal_steps = []
            if pm.normal_path:
                normal_steps = pm.normal_path.steps

            if not normal_steps:
                continue

            ab_groups = self._detectABInstances(normal_steps, pm)

            for group_label, group_steps in ab_groups.items() if ab_groups else [("single", normal_steps)]:
                for step in group_steps:
                    si = step if isinstance(step, dict) else step
                    step_key = self._stepKey(si)
                    cmds = cmd_by_step.get(step_key, [])
                    comps = comp_by_step.get(step_key, [])
                    conds = cond_by_step.get(step_key, [])
                    waits_here = wait_by_step.get(step_key, [])
                    trans_here = trans_by_step.get(step_key, [])

                    is_pure_transition = (not cmds and not comps and not waits_here
                                         and len(trans_here) <= 1)

                    if is_pure_transition and spans and spans[-1].template_id.startswith(f"T{tmpl_counter:04d}"):
                        spans[-1].covered_steps.append(CoveredStep(
                            step_identity=si, role="transition_pass_through",
                            is_pure_transition=True
                        ))
                        continue

                    if not cmds and not comps and not waits_here and is_pure_transition:
                        continue

                    tmpl_id = f"T{tmpl_counter:04d}_{pm.process_id}_s{si.get('step_value', -1)}"
                    inst_id = f"I{inst_counter:04d}_{tmpl_id}_{group_label}"
                    span_id = f"S{span_counter:04d}_{inst_id}"

                    tmpl = ActionTemplate(
                        template_id=tmpl_id,
                        process_id=pm.process_id,
                        business_description="",
                        mechanism=pm.entry.mechanisms[0] if pm.entry.mechanisms else "",
                        operation="",
                        target_position="",
                        instance_ids=[inst_id],
                        source_provenance={'step_value': si.get('step_value', -1)}
                    )
                    inst = ActionInstance(
                        instance_id=inst_id,
                        template_id=tmpl_id,
                        instance_label=group_label,
                        step_identity=si,
                        thread_idx=si.get('thread_idx', -1),
                        station=si.get('station', ''),
                    )
                    span = EngineeringActionSpan(
                        span_id=span_id,
                        template_id=tmpl_id,
                        instance_id=inst_id,
                        step_identity=si,
                        covered_steps=[CoveredStep(step_identity=si, role="start", is_start=True)],
                        command_evidence=[self._toCmdEvidence(c) for c in cmds],
                        completion_evidence=[self._toCompEvidence(c) for c in comps],
                        timing_intent="UNRESOLVED",
                        source_provenance={
                            'process_id': pm.process_id,
                            'step_value': si.get('step_value', -1),
                        }
                    )

                    templates.append(tmpl)
                    instances.append(inst)
                    spans.append(span)
                    span_counter += 1
                    tmpl_counter += 1
                    inst_counter += 1

        self._linkParallelSpans(spans, instances)
        self._markIndependentTimingValue(spans)

        return templates, instances, spans

    def _indexByStep(self, events: list) -> dict:
        idx = defaultdict(list)
        for e in events:
            si = e.get('step_identity', {})
            key = (si.get('station', ''), si.get('thread_idx', -1),
                   si.get('state_variable', ''), si.get('step_value', -1))
            idx[key].append(e)
        return dict(idx)

    def _stepKey(self, si: dict) -> tuple:
        return (si.get('station', ''), si.get('thread_idx', -1),
                si.get('state_variable', ''), si.get('step_value', -1))

    def _toCmdEvidence(self, c: dict) -> CommandEvidence:
        return CommandEvidence(
            variable=c.get('variable', ''),
            command_type=c.get('command_type', ''),
            actuator_path=c.get('actuator_path', ''),
            interface_dir=c.get('interface_dir', ''),
            element_id=c.get('element_id', ''),
            unit_name=c.get('unit_name', ''),
            network_id=c.get('network_id', ''),
            provenance=c.get('provenance', ''),
            is_interface_cmd=c.get('is_interface_cmd', False),
        )

    def _toCompEvidence(self, c: dict) -> CompletionEvidence:
        return CompletionEvidence(
            variable=c.get('variable', ''),
            feedback_type=c.get('feedback_type', ''),
            matched_command_id=c.get('matched_command_id', ''),
            match_confidence=c.get('match_confidence', ''),
            element_id=c.get('element_id', ''),
            unit_name=c.get('unit_name', ''),
            provenance=c.get('provenance', ''),
        )

    def _detectABInstances(self, steps: list, pm) -> dict:
        if not hasattr(pm, 'entry') or not pm.entry:
            return {}
        thread = pm.entry.thread_idx
        syncs = pm.syncs if hasattr(pm, 'syncs') else []
        ab_syncs = [s for s in syncs if s.sync_type == 'compound_condition' and len(s.threads) >= 2]
        if not ab_syncs:
            return {}
        return {"A": steps, "B": steps}

    def _linkParallelSpans(self, spans: list, instances: list):
        inst_lookup = {i.instance_id: i for i in instances}
        by_thread = defaultdict(list)
        for s in spans:
            inst = inst_lookup.get(s.instance_id)
            if inst:
                by_thread[inst.thread_idx].append(s)
        for thread, thread_spans in by_thread.items():
            if len(thread_spans) > 1:
                for s in thread_spans:
                    s.parallel_spans = [other.span_id for other in thread_spans if other.span_id != s.span_id]

    def _markIndependentTimingValue(self, spans: list):
        for s in spans:
            if not s.command_evidence and not s.completion_evidence:
                s.has_independent_timing_value = False
                s.in_main_table = False
                s.exclusion_reason = "no_command_no_completion"
            elif len(s.command_evidence) == 0 and len(s.completion_evidence) > 0:
                s.has_independent_timing_value = True
            else:
                s.has_independent_timing_value = True


def saveActionSpans(templates: list, instances: list, spans: list,
                    output_dir: Path):
    """保存到 output_dir/action_spans.json。"""
    out = {
        'templates': [t.to_dict() for t in templates],
        'instances': [i.to_dict() for i in instances],
        'spans': [s.to_dict() for s in spans],
    }
    with open(output_dir / 'action_spans.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
