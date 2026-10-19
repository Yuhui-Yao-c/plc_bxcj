#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bxcj_demo/run.py — Demo 主入口（独立工程，不依赖 plc_bxcj）

编排 vendor/lib pipeline + Demo 增量层，产出可核验的步序采集表。

用法:
    python bxcj_demo/run.py --config bxcj_demo/configs/transfer.yaml
    python bxcj_demo/run.py --config bxcj_demo/configs/ultrasound.yaml
    python bxcj_demo/run.py --config bxcj_demo/configs/mylar.yaml
    python bxcj_demo/run.py --all
    python bxcj_demo/run.py --all --fresh  # 强制重跑所有阶段

也支持命令行直接指定输入文件:
    python bxcj_demo/run.py --project transfer \\
        --xml "D:/资料/转接片.xml" \\
        --trace "D:/trace/ZJPJIANKONG0906.trace"
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import yaml

# ==== 路径设置（独立工程，不依赖 plc_bxcj）====
DEMO_ROOT = Path(__file__).resolve().parent
REPO_ROOT = DEMO_ROOT.parent  # 向后兼容：仍可用于查找 步序采集资料 等
VENDOR_ROOT = DEMO_ROOT / 'vendor'  # vendor/lib + vendor/tools

# sys.path 顺序（从前到后）：
# 1. VENDOR_ROOT — 使 `from lib.X import Y` 解析到 vendor/lib/（pipeline 模块）
# 2. REPO_ROOT — 使 `from dlib.X import Y` 解析到 bxcj_demo/dlib/
sys.path.insert(0, str(VENDOR_ROOT))
sys.path.insert(0, str(DEMO_ROOT))
sys.path.insert(0, str(REPO_ROOT))


def log(msg, level='INFO'):
    ts = time.strftime('%H:%M:%S')
    print(f'[{ts}] [{level}] {msg}', flush=True)


def run_subprocess(cmd, cwd=None, timeout=900):
    log(f'$ {" ".join(cmd)}')
    try:
        result = subprocess.run(
            cmd, cwd=cwd or str(REPO_ROOT),
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=timeout
        )
        if result.stdout:
            for line in result.stdout.splitlines()[:50]:
                print(f'  {line}')
        if result.returncode != 0:
            log(f'  STDERR: {result.stderr[-2000:]}', 'ERROR')
            return False
        return True
    except subprocess.TimeoutExpired:
        log(f'  超时 ({timeout}s)', 'ERROR')
        return False
    except Exception as e:
        log(f'  异常: {e}', 'ERROR')
        return False


def ensure_project_layout(workdir: Path, plc_source: str, source_format: str,
                          trace_file: str = None):
    """确保 project_workdir 下有 source/ 和 output/ 子目录，并放入源文件。"""
    workdir.mkdir(parents=True, exist_ok=True)
    source_dir = workdir / 'source'
    source_dir.mkdir(exist_ok=True)
    output_dir = workdir / 'output'
    output_dir.mkdir(exist_ok=True)

    # PLC 源码：先在 workdir/source/ 中查找，再在仓库根查找
    plc_src_path = None
    candidates = [
        source_dir / plc_source,                # 已在 workdir/source/
        workdir / plc_source,                   # workdir 根
        REPO_ROOT / plc_source,                 # 仓库根（绝对路径）
        VENDOR_ROOT / plc_source,                  # plc_bxcj 下
    ]
    for c in candidates:
        if c.exists():
            plc_src_path = c
            break
    if plc_src_path is None:
        raise FileNotFoundError(f'PLC 源码未找到: {plc_source}（已尝试 {len(candidates)} 个位置）')

    dst = source_dir / plc_src_path.name
    if not dst.exists() and dst.resolve() != plc_src_path.resolve():
        # 不复制大文件，创建符号链接（Windows 用目录连接）
        if source_format == 'smc2' or plc_src_path.stat().st_size > 50 * 1024 * 1024:
            try:
                # Windows 目录连接需要管理员，回退到 hardlink 或复制
                if not getattr(os, 'link', None):
                    shutil.copy(str(plc_src_path), str(dst))
                else:
                    try:
                        os.link(str(plc_src_path), str(dst))
                    except OSError:
                        shutil.copy(str(plc_src_path), str(dst))
            except Exception as e:
                log(f'  链接源码失败: {e}，尝试复制', 'WARN')
                shutil.copy(str(plc_src_path), str(dst))
        else:
            shutil.copy(str(plc_src_path), str(dst))
        log(f'  放入源码: {dst.name}')

    # trace 文件
    if trace_file:
        trace_src = REPO_ROOT / trace_file
        if not trace_src.exists():
            trace_src = VENDOR_ROOT / trace_file
        if trace_src.exists():
            # 如果是目录，不复制，直接用原路径（trace_pipeline 支持目录）
            if trace_src.is_dir():
                log(f'  trace 目录: {trace_src} ({len(list(trace_src.iterdir()))} 文件)')
            else:
                trace_dst = source_dir / trace_src.name
                if not trace_dst.exists():
                    try:
                        os.link(str(trace_src), str(trace_dst))
                    except OSError:
                        shutil.copy(str(trace_src), str(trace_dst))
                    log(f'  放入 trace: {trace_dst.name}')

    return source_dir, output_dir


def write_extract_config(workdir: Path, output_dir: Path, cfg: dict):
    """写出 extract_config.json。"""
    ec = cfg.get('extract_config', {})
    if ec.get('reuse_existing', False):
        existing = output_dir / 'extract_config.json'
        if existing.exists():
            log(f'  复用现有 extract_config.json')
            return
    # 否则从配置生成
    config = {}
    for k in ('state_variables', 'step_var_name', 'step_var_extract_pattern',
              'eq_block_type', 'source_decoder', 'split_dir',
              'discovery_strategies', 'primitive_params', 'state_domains',
              'strategies'):
        if k in ec:
            config[k] = ec[k]
    # 默认值
    config.setdefault('source_decoder',
                      'smc2' if cfg['project'].get('source_format') == 'smc2'
                      else 'plcopen_xml')
    out_path = output_dir / 'extract_config.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    log(f'  写出 extract_config.json ({len(config)} fields)')


# ============== Pipeline 阶段 ==============

def stage_build_evidence(workdir: Path, cfg: dict, fresh: bool) -> bool:
    output_dir = workdir / 'output'
    target = output_dir / 'evidence_store.json'
    if target.exists() and not fresh and cfg['pipeline'].get('reuse_intermediates', False):
        log(f'  复用 evidence_store.json')
        return True
    cmd = [sys.executable, 'tools/build_evidence.py', str(workdir)]
    return run_subprocess(cmd, cwd=str(VENDOR_ROOT))


def stage_build_step_graph(workdir: Path, cfg: dict, fresh: bool) -> bool:
    output_dir = workdir / 'output'
    target = output_dir / 'step_logic_slices.json'
    if target.exists() and not fresh and cfg['pipeline'].get('reuse_intermediates', False):
        log(f'  复用 step_logic_slices.json')
        return True
    cmd = [sys.executable, 'tools/build_step_graph.py', str(workdir)]
    return run_subprocess(cmd, cwd=str(VENDOR_ROOT), timeout=1800)


def stage_build_event_graph(workdir: Path, cfg: dict, fresh: bool,
                            force_rerun: bool = False) -> bool:
    output_dir = workdir / 'output'
    target = output_dir / 'event_graph.json'
    if target.exists() and not fresh and not force_rerun and \
       cfg['pipeline'].get('reuse_intermediates', False):
        log(f'  复用 event_graph.json')
        return True
    cmd = [sys.executable, 'tools/build_event_graph.py', str(workdir)]
    return run_subprocess(cmd, cwd=str(VENDOR_ROOT), timeout=600)


def stage_describe_steps(workdir: Path, cfg: dict, fresh: bool) -> bool:
    output_dir = workdir / 'output'
    target = output_dir / 'steps_described.json'
    if target.exists() and not fresh and cfg['pipeline'].get('reuse_intermediates', False):
        log(f'  复用 steps_described.json')
        return True
    cmd = [sys.executable, 'tools/describe_steps.py', str(workdir)]
    return run_subprocess(cmd, cwd=str(VENDOR_ROOT), timeout=600)


def stage_timing_boundary(workdir: Path, cfg: dict, fresh: bool) -> bool:
    """调用 TimingBoundaryResolver 生成 timing_boundaries.json。"""
    output_dir = workdir / 'output'
    target = output_dir / 'timing_boundaries.json'
    if target.exists() and not fresh and cfg['pipeline'].get('reuse_intermediates', False):
        log(f'  复用 timing_boundaries.json')
        return True

    slices_path = output_dir / 'step_logic_slices.json'
    if not slices_path.exists():
        log(f'  跳过：slices 不存在', 'WARN')
        return False

    with open(slices_path, 'r', encoding='utf-8') as f:
        slices = json.load(f)

    # 加载 extract_config
    config_path = output_dir / 'extract_config.json'
    config = {}
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

    from lib.timing_boundary_resolver import TimingBoundaryResolver

    resolver = TimingBoundaryResolver(config)
    # 收集 source files
    source_dir = workdir / 'source'
    source_files = []
    if source_dir.exists():
        source_files = [str(p) for p in source_dir.iterdir() if p.suffix in ('.xml', '.smc2')]

    # 从 slices 构建 step_list（resolver 需要可迭代的 step_list）
    step_list = []
    for s in slices:
        si = s.get('step_identity', {})
        step_list.append({
            'station': si.get('station', ''),
            'thread_idx': si.get('thread_idx', -1),
            'state_variable': si.get('state_variable', ''),
            'step_value': si.get('step_value', -1),
        })

    try:
        boundaries = resolver.resolve_all_steps(
            step_list=step_list,
            source_files=source_files,
            step_logic_slices=slices
        )
        # 序列化
        out_data = [b.to_dict() if hasattr(b, 'to_dict') else b for b in boundaries]
        with open(target, 'w', encoding='utf-8') as f:
            json.dump(out_data, f, ensure_ascii=False, indent=2)
        log(f'  生成 {len(out_data)} 条 timing_boundary')
        return True
    except Exception as e:
        log(f'  TimingBoundaryResolver 失败: {e}', 'ERROR')
        traceback.print_exc()
        return False


def stage_end_tag_selector(workdir: Path, output_dir: Path, cfg: dict) -> bool:
    """P1: 选择结束标签。"""
    from dlib.end_tag_selector import EndTagSelector
    selector = EndTagSelector(workdir / 'output', cfg)
    return selector.run()


def stage_transition_filter(workdir: Path, output_dir: Path, cfg: dict) -> bool:
    """P2: 过滤纯转移步。"""
    from dlib.transition_filter import TransitionFilter
    filt = TransitionFilter(workdir / 'output', cfg)
    return filt.run()


def stage_trace_pipeline(workdir: Path, output_dir: Path, cfg: dict) -> bool:
    """P4: Trace 配对（如启用）。"""
    trace_cfg = cfg['pipeline'].get('trace', {})
    if not trace_cfg.get('enabled', False):
        log(f'  trace 未启用，跳过')
        # 写一个空报告
        empty_report = {
            'enabled': False,
            'reason': 'no_trace_configured',
            'matched_actions': 0,
            'unmatched_actions': 0
        }
        # 同时写到 workdir 和 demo output
        for d in (workdir / 'output', output_dir):
            with open(d / 'trace_match_report.json', 'w', encoding='utf-8') as f:
                json.dump(empty_report, f, ensure_ascii=False, indent=2)
        return True

    from dlib.trace_pipeline import TracePipeline
    tp = TracePipeline(workdir / 'output', cfg)
    ok = tp.run()
    # 复制到 demo output
    src = workdir / 'output' / 'trace_match_report.json'
    if src.exists():
        import shutil
        shutil.copy(str(src), str(output_dir / 'trace_match_report.json'))
    return ok


# ============== v10.39 新增阶段 ==============

def stage_smc2_fb_recognizer(workdir: Path, cfg: dict) -> bool:
    """v10.39: Mylar 专用 — SMC2 FB 调用识别器。
    读取 event_graph.json + evidence_store.json + step_logic_slices.json，
    识别 Axis_Group_*/Cylinder[N]/AxisValP[N] 等真实 SMC2 FB 模式，
    输出补充 completions 到 fb_supplement.json。
    不修改原始 event_graph.json。"""
    proj_name = cfg.get('project', {}).get('name', '')
    if proj_name != 'mylar':
        log(f'  非 mylar 项目，跳过 SMC2FBRecognizer')
        return True

    work_output = workdir / 'output'
    eg_path = work_output / 'event_graph.json'
    ev_path = work_output / 'evidence_store.json'
    slices_path = work_output / 'step_logic_slices.json'

    if not eg_path.exists():
        log(f'  event_graph.json 不存在，跳过', 'WARN')
        return False

    log(f'  加载 event_graph.json...')
    with open(eg_path, 'r', encoding='utf-8') as f:
        eg = json.load(f)
    orig_cmd = len(eg.get('commands', []))
    orig_comp = len(eg.get('completions', []))
    log(f'  原 commands: {orig_cmd}, 原 completions: {orig_comp}')

    log(f'  加载 evidence_store.json...')
    ev = {}
    if ev_path.exists():
        with open(ev_path, 'r', encoding='utf-8') as f:
            ev = json.load(f)
        log(f'  entities: {len(ev.get("entities", {}))}')

    log(f'  加载 step_logic_slices.json...')
    slices = []
    if slices_path.exists():
        with open(slices_path, 'r', encoding='utf-8') as f:
            slices = json.load(f)
        log(f'  slices: {len(slices)}')

    from dlib.smc2_fb_recognizer import SMC2FBRecognizer
    recognizer = SMC2FBRecognizer()
    log(f'  运行 SMC2FBRecognizer v2（基于真实 SMC2 命名模式）...')
    result = recognizer.recognize(slices, eg, ev)

    stats = result.get('stats', {})
    log(f'  === SMC2FBRecognizer v2 统计 ===')
    for k, v in stats.items():
        log(f'    {k}: {v}')

    fb_count = len(result.get('fb_evidence', []))
    new_comp_count = len(result.get('completions', []))
    log(f'  FB evidence 总数: {fb_count}')
    log(f'  新增 confirmed completions: {new_comp_count}')

    # 输出 FB 类型分布
    from collections import Counter
    fb_types = Counter(ev.get('fb_type', '?') for ev in result.get('fb_evidence', []))
    log(f'  FB 类型分布: {dict(fb_types)}')
    fb_conf = Counter(ev.get('confidence', '?') for ev in result.get('fb_evidence', []))
    log(f'  置信度分布: {dict(fb_conf)}')

    out_path = work_output / 'fb_supplement.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f'  写出: {out_path.name}')
    return True


def stage_ultrasound_interface_analyzer(workdir: Path, cfg: dict) -> bool:
    """v10.39: 超声专用 — 接口分析与反馈重分类。
    读取 event_graph.json，将 Done/Ready/PosOK/AbsEx/M_Act/M_Back 重分类为 completions，
    但保留 AbsEx 在 LD 网络中作为执行命令的身份（不一刀切）。
    输出 reclass_suggestions.json，不修改原始 event_graph.json。"""
    proj_name = cfg.get('project', {}).get('name', '')
    if proj_name != 'ultrasound':
        log(f'  非 ultrasound 项目，跳过 UltrasoundInterfaceAnalyzer')
        return True

    work_output = workdir / 'output'
    eg_path = work_output / 'event_graph.json'
    if not eg_path.exists():
        log(f'  event_graph.json 不存在，跳过', 'WARN')
        return False

    log(f'  加载 event_graph.json...')
    with open(eg_path, 'r', encoding='utf-8') as f:
        eg = json.load(f)
    orig_cmd = len(eg.get('commands', []))
    orig_comp = len(eg.get('completions', []))
    log(f'  原 commands: {orig_cmd}, 原 completions: {orig_comp}')

    from dlib.ultrasound_interface_analyzer import UltrasoundInterfaceAnalyzer
    analyzer = UltrasoundInterfaceAnalyzer()
    log(f'  运行 UltrasoundInterfaceAnalyzer...')
    result = analyzer.analyze(eg)

    reclass_cmd = len(result.get('reclassified_commands', []))
    new_comp = len(result.get('new_completions', []))
    iface_corr = len(result.get('interface_dir_corrections', {}))
    suggestions = result.get('suggestions', [])
    log(f'  重分类后 commands: {reclass_cmd}')
    log(f'  新增 completions: {new_comp}')
    log(f'  接口方向修正: {iface_corr}')
    log(f'  重分类建议: {len(suggestions)}')

    from collections import Counter
    sugg_types = Counter(s.get('suggested_type', '?') for s in suggestions)
    log(f'  建议类型分布: {dict(sugg_types)}')

    out_path = work_output / 'reclass_suggestions.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f'  写出: {out_path.name}')
    return True


def stage_build_process_model(workdir: Path, cfg: dict) -> bool:
    """v10.39: 构建 ProcessModel。
    输入: event_graph.json + step_logic_slices.json + steps_described.json
          + (mylar) fb_supplement.json + (ultrasound) reclass_suggestions.json
    输出: process_models.json
    """
    work_output = workdir / 'output'
    eg_path = work_output / 'event_graph.json'
    slices_path = work_output / 'step_logic_slices.json'
    desc_path = work_output / 'steps_described.json'

    if not eg_path.exists() or not slices_path.exists():
        log(f'  缺少 event_graph 或 step_logic_slices，跳过', 'WARN')
        return False

    log(f'  加载 event_graph.json...')
    with open(eg_path, 'r', encoding='utf-8') as f:
        eg = json.load(f)

    # 应用项目专属增强
    proj_name = cfg.get('project', {}).get('name', '')
    if proj_name == 'mylar':
        fb_supp_path = work_output / 'fb_supplement.json'
        if fb_supp_path.exists():
            log(f'  合并 Mylar FB supplement...')
            with open(fb_supp_path, 'r', encoding='utf-8') as f:
                supp = json.load(f)
            from dlib.smc2_fb_recognizer import supplementEventGraph
            eg = supplementEventGraph(eg, supp)
            log(f'  合并后 commands: {len(eg.get("commands", []))}, completions: {len(eg.get("completions", []))}')
    elif proj_name == 'ultrasound':
        reclass_path = work_output / 'reclass_suggestions.json'
        if reclass_path.exists():
            log(f'  合并超声 reclass...')
            with open(reclass_path, 'r', encoding='utf-8') as f:
                reclass = json.load(f)
            from dlib.ultrasound_interface_analyzer import applyReclassification
            eg = applyReclassification(eg, reclass)
            log(f'  合并后 commands: {len(eg.get("commands", []))}, completions: {len(eg.get("completions", []))}')

    log(f'  加载 step_logic_slices.json...')
    with open(slices_path, 'r', encoding='utf-8') as f:
        slices = json.load(f)

    steps_described = []
    if desc_path.exists():
        log(f'  加载 steps_described.json...')
        with open(desc_path, 'r', encoding='utf-8') as f:
            steps_described = json.load(f)

    from dlib.process_model import ProcessModelBuilder, saveProcessModels
    builder = ProcessModelBuilder(proj_name, cfg)
    log(f'  构建 ProcessModel...')
    models = builder.build(eg, slices, steps_described)
    log(f'  ProcessModel 总数: {len(models)}')

    # 统计
    total_normal_steps = sum(len(m.normal_path.steps) if m.normal_path else 0 for m in models)
    total_branches = sum(len(m.branches) for m in models)
    total_syncs = sum(len(m.syncs) for m in models)
    log(f'  正常路径总 step 数: {total_normal_steps}')
    log(f'  条件分支总数: {total_branches}')
    log(f'  同步关系总数: {total_syncs}')

    out_path = work_output / 'process_models.json'
    saveProcessModels(models, out_path)
    log(f'  写出: {out_path.name}')
    return True


def stage_build_action_span(workdir: Path, cfg: dict) -> bool:
    """v10.39: 构建 ActionSpan。
    输入: process_models.json + event_graph.json (含增强)
    输出: action_spans.json (templates + instances + spans)
    """
    work_output = workdir / 'output'
    pm_path = work_output / 'process_models.json'
    eg_path = work_output / 'event_graph.json'

    if not pm_path.exists() or not eg_path.exists():
        log(f'  缺少 process_models 或 event_graph，跳过', 'WARN')
        return False

    log(f'  加载 process_models.json...')
    from dlib.process_model import loadProcessModels
    pm_dicts = loadProcessModels(pm_path)
    log(f'  ProcessModel 数: {len(pm_dicts)}')

    log(f'  加载 event_graph.json...')
    with open(eg_path, 'r', encoding='utf-8') as f:
        eg = json.load(f)

    # 应用项目专属增强（与 process_model 阶段一致）
    proj_name = cfg.get('project', {}).get('name', '')
    if proj_name == 'mylar':
        fb_supp_path = work_output / 'fb_supplement.json'
        if fb_supp_path.exists():
            with open(fb_supp_path, 'r', encoding='utf-8') as f:
                supp = json.load(f)
            from dlib.smc2_fb_recognizer import supplementEventGraph
            eg = supplementEventGraph(eg, supp)
    elif proj_name == 'ultrasound':
        reclass_path = work_output / 'reclass_suggestions.json'
        if reclass_path.exists():
            with open(reclass_path, 'r', encoding='utf-8') as f:
                reclass = json.load(f)
            from dlib.ultrasound_interface_analyzer import applyReclassification
            eg = applyReclassification(eg, reclass)

    # 将字典转回 ProcessModel 对象（简单转换，只取必要字段）
    from dlib.process_model import ProcessModel, ProcessEntry, ProcessPath
    from dlib.action_span import ActionSpanBuilder, saveActionSpans
    models = []
    for pm_dict in pm_dicts:
        entry_dict = pm_dict.get('entry', {}) or {}
        entry = ProcessEntry(
            process_id=entry_dict.get('process_id', ''),
            station=entry_dict.get('station', ''),
            thread_idx=entry_dict.get('thread_idx', -1),
            state_variable=entry_dict.get('state_variable', ''),
            entry_step=entry_dict.get('entry_step', -1),
            process_purpose=entry_dict.get('process_purpose', ''),
            mechanisms=entry_dict.get('mechanisms', []),
        )
        np_dict = pm_dict.get('normal_path', {}) or {}
        normal_path = ProcessPath(
            path_id=np_dict.get('path_id', ''),
            process_id=np_dict.get('process_id', ''),
            path_type=np_dict.get('path_type', 'main'),
            steps=np_dict.get('steps', []),
        )
        branches = [ProcessPath(
            path_id=b.get('path_id', ''),
            process_id=b.get('process_id', ''),
            path_type=b.get('path_type', 'branch'),
            steps=b.get('steps', []),
            branch_from=b.get('branch_from', ''),
            guard=b.get('guard', ''),
        ) for b in pm_dict.get('branches', [])]
        pm = ProcessModel(
            project=pm_dict.get('project', ''),
            process_id=pm_dict.get('process_id', ''),
            entry=entry,
            purpose=pm_dict.get('purpose', ''),
            mechanisms=pm_dict.get('mechanisms', []),
            normal_path=normal_path,
            branches=branches,
            syncs=[],
            endpoint_step=pm_dict.get('endpoint_step', -1),
            endpoint_type=pm_dict.get('endpoint_type', ''),
        )
        models.append(pm)

    builder = ActionSpanBuilder(proj_name, cfg)
    log(f'  构建 ActionSpan...')
    templates, instances, spans = builder.build(models, eg)
    log(f'  templates: {len(templates)}')
    log(f'  instances: {len(instances)}')
    log(f'  spans: {len(spans)}')

    in_main = sum(1 for s in spans if s.in_main_table)
    has_cmd = sum(1 for s in spans if s.command_evidence)
    has_comp = sum(1 for s in spans if s.completion_evidence)
    log(f'  进入主表: {in_main}, 有命令: {has_cmd}, 有完成反馈: {has_comp}')

    out_path = work_output / 'action_spans.json'
    saveActionSpans(templates, instances, spans, work_output)
    log(f'  写出: {out_path.name}')
    return True


def stage_select_timing_intent(workdir: Path, cfg: dict) -> bool:
    """v10.39: 为每个 ActionSpan 选择 TimingIntentSpec。
    输入: action_spans.json
    输出: timing_intents.json (span_id -> TimingIntentSpec)
    """
    work_output = workdir / 'output'
    aspan_path = work_output / 'action_spans.json'
    if not aspan_path.exists():
        log(f'  action_spans.json 不存在，跳过', 'WARN')
        return False

    log(f'  加载 action_spans.json...')
    with open(aspan_path, 'r', encoding='utf-8') as f:
        aspan_data = json.load(f)

    spans = aspan_data.get('spans', [])
    log(f'  ActionSpan 总数: {len(spans)}')

    proj_name = cfg.get('project', {}).get('name', '')
    from dlib.timing_intent import TimingIntentSelector, saveTimingIntents
    selector = TimingIntentSelector(proj_name, cfg)
    log(f'  项目: {proj_name}, 默认 intent: {selector.default_intent}')

    specs = {}
    from collections import Counter
    intent_dist = Counter()
    for s in spans:
        spec = selector.select(s)
        specs[s.get('span_id', '')] = spec.to_dict()
        intent_dist[spec.intent_type.value] += 1

    log(f'  TimingIntent 分布: {dict(intent_dist)}')

    out_path = work_output / 'timing_intents.json'
    saveTimingIntents(specs, out_path)
    log(f'  写出: {out_path.name}')
    return True


def stage_demo_exporter(workdir: Path, output_dir: Path, cfg: dict) -> bool:
    """P5: 导出最终 Excel（含颜色+review_reason_code）。"""
    from dlib.demo_exporter import DemoExporter
    exporter = DemoExporter(workdir / 'output', output_dir, cfg)
    return exporter.run()


def stage_comparison(workdir: Path, output_dir: Path, cfg: dict) -> bool:
    """P7: 独立人工对比报告（隔离 Gold）。"""
    # v10.35: 新增 comparison_builder（5-Sheet 对照 Excel）
    from dlib.comparison_builder import ComparisonBuilder
    proj = cfg.get('project', {})
    cb = ComparisonBuilder(
        project_name=proj.get('name', 'unknown'),
        display_name=proj.get('display_name', ''),
        work_output_dir=workdir / 'output',
        demo_output_dir=output_dir,
        manual_table_path=proj.get('manual_table'),
        repo_root=str(REPO_ROOT),
    )
    cb.run()

    # 同时保留旧的 markdown 对比报告
    from dlib.comparison_reporter import ComparisonReporter
    cr = ComparisonReporter(workdir / 'output', output_dir, cfg)
    cr.run()
    return True


# ============== 主流程 ==============

def run_project(config_path: str, fresh: bool = False) -> bool:
    log(f'==== 加载配置: {config_path} ====')
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)

    proj = cfg['project']
    pipeline = cfg.get('pipeline', {})
    rerun = pipeline.get('rerun_stages', [])

    workdir_str = proj['project_workdir']
    workdir = REPO_ROOT / workdir_str if not Path(workdir_str).is_absolute() else Path(workdir_str)

    log(f'项目: {proj["display_name"]} ({proj["name"]})')
    log(f'工作目录: {workdir}')

    # 注入 _repo_root 供下游模块使用
    cfg['_repo_root'] = str(REPO_ROOT)

    # 1. 准备目录
    ensure_project_layout(
        workdir, proj['plc_source'], proj['source_format'],
        proj.get('trace_file')
    )

    output_dir = REPO_ROOT / cfg['output']['dir']
    output_dir.mkdir(parents=True, exist_ok=True)

    # 2. extract_config
    write_extract_config(workdir, workdir / 'output', cfg)

    # 3. Mylar interface_spec 自动生成（如需要）
    ispec = cfg.get('interface_spec')
    if ispec and ispec.get('auto_generate'):
        log('--- Mylar interface_spec 自动生成 ---')
        from dlib.mylar_interface_analyzer import MylarInterfaceAnalyzer
        analyzer = MylarInterfaceAnalyzer(workdir / 'output', cfg)
        if not analyzer.run():
            log('  interface_spec 生成失败', 'ERROR')
            return False

    # 4. 重跑 event_graph（interface_spec 改变时）
    force_eg = bool(ispec and ispec.get('auto_generate'))

    # 5. 主 pipeline
    stages = [
        ('build_evidence', lambda: stage_build_evidence(workdir, cfg, fresh)),
        ('build_step_graph', lambda: stage_build_step_graph(workdir, cfg, fresh)),
        ('build_event_graph', lambda: stage_build_event_graph(workdir, cfg, fresh, force_rerun=force_eg)),
        ('describe_steps', lambda: stage_describe_steps(workdir, cfg, fresh)),
        ('timing_boundary', lambda: stage_timing_boundary(workdir, cfg, fresh)),
        # v10.39 新增：项目专属增强（不修改原始 event_graph）
        ('smc2_fb_recognizer', lambda: stage_smc2_fb_recognizer(workdir, cfg)),
        ('ultrasound_interface_analyzer', lambda: stage_ultrasound_interface_analyzer(workdir, cfg)),
        # v10.39 新增：核心架构层
        ('build_process_model', lambda: stage_build_process_model(workdir, cfg)),
        ('build_action_span', lambda: stage_build_action_span(workdir, cfg)),
        ('select_timing_intent', lambda: stage_select_timing_intent(workdir, cfg)),
        # 原 P1-P7 增量层
        ('end_tag_selector', lambda: stage_end_tag_selector(workdir, output_dir, cfg)),
        ('transition_filter', lambda: stage_transition_filter(workdir, output_dir, cfg)),
        ('trace_pipeline', lambda: stage_trace_pipeline(workdir, output_dir, cfg)),
        ('demo_exporter', lambda: stage_demo_exporter(workdir, output_dir, cfg)),
        ('comparison', lambda: stage_comparison(workdir, output_dir, cfg)),
    ]

    for name, fn in stages:
        # 决定是否执行此阶段
        should_run = fresh or name in rerun or name in ('end_tag_selector', 'transition_filter',
                                                          'trace_pipeline', 'demo_exporter',
                                                          'comparison',
                                                          # v10.39 新增 stage 默认执行
                                                          'smc2_fb_recognizer',
                                                          'ultrasound_interface_analyzer',
                                                          'build_process_model',
                                                          'build_action_span',
                                                          'select_timing_intent')
        # comparison 默认执行（除非显式禁用）
        if name == 'comparison':
            comp_cfg = pipeline.get('comparison', {})
            if not comp_cfg.get('enabled', True):
                should_run = False
            else:
                should_run = True
        # 复用模式下，前 5 个阶段可以复用
        if not fresh and pipeline.get('reuse_intermediates', False) and \
           name in ('build_evidence', 'build_step_graph', 'build_event_graph',
                    'describe_steps', 'timing_boundary') and name not in rerun:
            should_run = False

        if not should_run:
            log(f'--- 跳过 {name} ---')
            continue

        log(f'--- 阶段: {name} ---')
        try:
            ok = fn()
            if not ok:
                log(f'阶段 {name} 失败', 'ERROR')
                # 失败后不可继续使用陈旧产物伪装成成功
                return False
        except Exception as e:
            log(f'阶段 {name} 异常: {e}', 'ERROR')
            traceback.print_exc()
            return False

    log(f'==== 项目 {proj["name"]} 完成 ====')
    log(f'输出目录: {output_dir}')
    return True


def build_config_from_args(args) -> dict:
    """从命令行参数动态生成配置。"""
    project = args.project
    # 项目默认值
    defaults = {
        'transfer': {
            'display_name': '转接片',
            'source_format': 'plcopen_xml',
            'state_variables': [{'name': 'Process_steps'}],
            'step_var_extract_pattern': r'([A-Z]{2}\d+)V?\.(\w+)\[(\d+)\]',
            'eq_block_type': 'EQ',
        },
        'ultrasound': {
            'display_name': '超声',
            'source_format': 'plcopen_xml',
            'state_variables': [{'name': 'AutoStep', 'pattern': '{station}.AutoStep[{thread}]'}],
            'step_var_extract_pattern': r'(\w+)\.?(AutoStep)\[(\d+)\]',
            'eq_block_type': 'EQ',
        },
        'mylar': {
            'display_name': '包Mylar',
            'source_format': 'smc2',
            'state_variables': [{'name': 'm_nNextAutoStep'}],
            'step_var_extract_pattern': r'(\w+)\[(\d+)\]',
            'eq_block_type': 'EQ',
        },
    }
    d = defaults.get(project, {'display_name': project, 'source_format': 'plcopen_xml'})

    # 工作目录
    workdir = args.workdir or str(DEMO_ROOT / 'workspace' / project)
    Path(workdir).mkdir(parents=True, exist_ok=True)

    # PLC 源码
    plc_source = args.xml or args.source or ''
    if plc_source:
        plc_path = Path(plc_source)
        # 如果是文件，复制到 workdir/source/
        source_dir = Path(workdir) / 'source'
        source_dir.mkdir(parents=True, exist_ok=True)
        if plc_path.is_file():
            dst = source_dir / plc_path.name
            if not dst.exists():
                import shutil
                shutil.copy(str(plc_path), str(dst))
            plc_source = plc_path.name
        elif plc_path.is_dir():
            # SMC2 工程目录：复制所有文件
            import shutil
            for f in plc_path.iterdir():
                if f.is_file() and f.suffix in ('.smc2', '.xml', '.cmtp', '.cxob', '.debug'):
                    dst = source_dir / f.name
                    if not dst.exists():
                        shutil.copy(str(f), str(dst))
            plc_source = next((f.name for f in source_dir.iterdir()
                              if f.suffix in ('.smc2', '.xml')), '')

    cfg = {
        'project': {
            'name': project,
            'display_name': d['display_name'],
            'project_workdir': workdir,
            'plc_source': plc_source,
            'trace_file': args.trace or None,
            'manual_table': args.manual or None,
            'source_format': d['source_format'],
        },
        'extract_config': {
            'reuse_existing': False,
            'state_variables': d['state_variables'],
            'step_var_extract_pattern': d.get('step_var_extract_pattern', ''),
            'eq_block_type': d.get('eq_block_type', 'EQ'),
            'source_decoder': 'smc2' if d['source_format'] == 'smc2' else 'plcopen_xml',
        },
        'pipeline': {
            'reuse_intermediates': False,
            'rerun_stages': ['build_evidence', 'build_step_graph', 'build_event_graph',
                            'describe_steps', 'timing_boundary',
                            'smc2_fb_recognizer', 'ultrasound_interface_analyzer',
                            'build_process_model', 'build_action_span', 'select_timing_intent',
                            'end_tag_selector', 'transition_filter', 'trace_pipeline', 'demo_exporter',
                            'comparison'],
            'trace': {
                'enabled': bool(args.trace),
            },
            'comparison': {
                'enabled': bool(args.manual),
            },
        },
        'output': {
            'dir': str(DEMO_ROOT / 'output' / project),
        },
    }

    # Mylar 需要 interface_spec 自动生成
    if project == 'mylar':
        cfg['interface_spec'] = {'auto_generate': True}

    return cfg


def main():
    parser = argparse.ArgumentParser(
        description='bxcj_demo — PLC 步序采集通用 Demo',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 通过配置文件运行
  python run.py --config configs/transfer.yaml
  python run.py --all

  # 通过命令行参数运行（独立环境）
  python run.py --project transfer \\
      --xml "D:/资料/转接片.xml" \\
      --trace "D:/trace/ZJPJIANKONG0906.trace"

  python run.py --project mylar \\
      --source "D:/资料/包mylar工程" \\
      --trace "D:/trace目录"
        """,
    )
    parser.add_argument('--config', help='YAML 配置文件路径')
    parser.add_argument('--all', action='store_true', help='运行所有项目')
    parser.add_argument('--fresh', action='store_true', help='强制重跑所有阶段')
    # 命令行直接指定输入
    parser.add_argument('--project', help='项目名（transfer/ultrasound/mylar）')
    parser.add_argument('--xml', help='PLC XML 源码路径')
    parser.add_argument('--source', help='PLC 工程目录路径（SMC2 等）')
    parser.add_argument('--trace', help='Trace 文件或目录路径')
    parser.add_argument('--workdir', help='工作目录路径（存放 source/ 和 output/）')
    parser.add_argument('--manual', help='人工 Gold 表路径（可选，仅对比用）')
    args = parser.parse_args()

    # 命令行模式：动态生成配置
    if args.project and (args.xml or args.source):
        cfg = build_config_from_args(args)
        cfg_path = DEMO_ROOT / 'output' / f'{args.project}_runtime.yaml'
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, allow_unicode=True)
        log(f'动态生成配置: {cfg_path}')
        if not run_project(str(cfg_path), fresh=args.fresh):
            sys.exit(1)
        return

    if args.all:
        configs = [
            'bxcj_demo/configs/transfer.yaml',
            'bxcj_demo/configs/ultrasound.yaml',
            'bxcj_demo/configs/mylar.yaml',
        ]
        results = {}
        for c in configs:
            cfg_path = REPO_ROOT / c
            if not cfg_path.exists():
                log(f'配置不存在: {c}', 'ERROR')
                results[c] = False
                continue
            try:
                ok = run_project(str(cfg_path), fresh=args.fresh)
                results[c] = ok
            except Exception as e:
                log(f'项目 {c} 异常: {e}', 'ERROR')
                traceback.print_exc()
                results[c] = False

        log('\n==== 总结 ====')
        for c, ok in results.items():
            log(f'  {c}: {"OK" if ok else "FAIL"}')
        if not all(results.values()):
            sys.exit(1)
    elif args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = REPO_ROOT / args.config
        if not run_project(str(cfg_path), fresh=args.fresh):
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
