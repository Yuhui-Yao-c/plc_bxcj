# v10.40 第一批生产路径重构与回归指引

## 当前目标与边界

本轮只解决 **PLC Step找对、Step控制事实提取对、中文描述尽可能使用确定的命令/FB参数**。

业务动作跨Step合并、A/B汇总、人工采集表粒度调整，暂不修改。保留原始所有Step以便工程师核查。

### 本轮实际修改

- `run.py`：阶段失败即失败；使用当前Python解释器；Mylar接口分析在StepLogicSlices产出之后运行；旧缓存不存在时不得跳过基础阶段。
- `vendor/tools/build_step_graph.py`：SMC2读取`.smc2`/XML；未发现输入或解析出零Unit时立即失败；同一Step的所有出现位置必须保留，旧扁平投影不代表Owner已经核验。
- `vendor/lib/decoders/smc2_decoder.py`：SMC2 Ix按Network限定，LRI/RRI映射保留；**这改变了元素ID，旧slices无效**。
- `vendor/lib/step_logic_slicer.py`：状态变量家族精确匹配；只取相同网络注释；保留写入目标的完整状态变量；布尔路径不确定则不冒充已验证。
- `vendor/lib/primitives_canonical.py`：Step家族识别避免相似变量名前缀交叉；原始EQ表达式决定实际状态变量家族。
- `vendor/lib/step_fact_projection.py`：保留FB输入输出、命令/Guard事实、AST、证据位置；多Anchor未验证owner时明确提示。
- `vendor/lib/step_graph.py`：Transition target引用真实被写入的状态变量家族。
- `vendor/tools/describe_steps.py`：在没有可靠注释时，按真实AxisMove/MoveLinear的Axis与Position参数生成保守中文。
- `dlib/trace_pipeline.py`：威纶通CSV时间戳由Unix秒转换成毫秒；非法时间不能伪装成零时刻。
- `configs/transfer.yaml`：不再默认复用旧网络身份/StepFact契约的中间结果。

## 自动测试

无需三项目专有源码：

```bash
python -m pip install -r bxcj_demo/requirements.lock
python -m compileall -q bxcj_demo
python -m unittest discover -s bxcj_demo/tests -p 'test_*.py' -v
```

PR配有GitHub Actions CI。**CI通过仅证明轻量回归测试通过，不能替代三项目真实XML/Trace全量评测。**

## 三项目必须实测的真实案例

| 项目 | 案例 | 验证点 |
|---|---|---|
| Mylar | `m_nNextAutoStep[3]=110` | Axis009、正常位置3、FB完成信号、实际后继120；不得借用load_Up状态机的相同步值 |
| Mylar | `m_nNextAutoStep[5]=240` | 多轴命令、原生FB参数、后继250 |
| Mylar | `m_nNextAutoStep[37]=140` | Axis053、目标位置3、后继150 |
| Mylar | `m_nNextAutoStep[28]=310` | 区分读取该状态的跨线程同步网络与自身写入320的网络 |
| 转接片 | EM01 Process_steps[2] | 所有状态及内部网络、命令、Guard、目标状态，无跨网络串扰 |
| 转接片 | EM01 Process_steps[3]=170 | Robot与板链同步独立事实，不将多个Guard误当多个动作 |
| 超声 | ST01 AutoStep[2] | LD/ST兼容，真实命令和后继状态 |
| 超声 | 原始AbsEx线圈 | 不能仅因变量名是AbsEx就当完成反馈 |
| 超声 | L7A源程序/L8A Trace | 有配对也必须标版本不匹配，不能标为已证实时间 |

这些是**回归检查要求**，而非宣称本PR已在专有原始文件上验证的成果。

## 来源版本 / Gold 隔离

1. 转接片以20260504未后补版本为正式数据源。
2. 超声以L7A未后补版本为正式数据源；L5B人工后补监控变量不能成为源码事实。
3. Mylar使用L5A0411_2。
4. 人工Gold只可在冻结生产产物后进行独立评价。
5. 最新代码未完整实现OwnerStep/ReferencedStep和已证实的跨网络控制引用分析，不得只凭重复EQ合并各网络事实。

## 运行方式

确保本机三个源码及Trace路径已经按YAML正确设置，再执行：

```bash
python bxcj_demo/run.py --all --fresh
```

或提供显式绝对路径：

```bash
python bxcj_demo/run.py --project mylar --source "D:/原始资料/包mylar" --trace "D:/原始资料/包mylar/trace" --fresh
```

特别注意：此次SMC2元素身份和Steps投影已变更，**禁止用v10.35–v10.39遗留的`step_logic_slices.json`充当本版实测结果**。

## 待后续处理的明确缺口

- 更完整地判定OwnerStep与ReferencedStep，而不是简单按EQ/StepValue合并。
- 跨网络VariableFlow只提供数据依赖，不自动证明当前Step的控制转移。
- Mylar CSV多文件跨段去重、采样精度/RawTime验证、状态驻留的物理时长口径。
- 中文语义仍需HMI原始中文、EC、DUT/轴位置的身份一致映射；AxisMove参数回退仅是保守初步能力。
- 现有正式Excel仍使用旧Planner；新版ProcessModel/ActionSpan不等于已接管生产采集粒度（这符合本轮暂缓合并的要求）。

**本PR任何时候都不能用旧版动作数或Gold吻合度宣称程序事实正确。**
