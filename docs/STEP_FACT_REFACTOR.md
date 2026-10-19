# StepFact 事实保真重构（第一批）

## 目标和范围

本批以找对 Step、提对程序事实、为中文描述保存证据为目标。**没有**调整动作合并、人工表匹配、Trace时长或项目计时策略。

### 已有代码变更

- `vendor/lib/step_fact_projection.py`：单独实现完整FB参数/Boolean AST/Evidence投影，兼容旧 `steps_raw` 中的 `actions`、`guards`、`calls`、`transitions` 等字段。
- `vendor/tools/build_step_graph`：重复Step Anchor不再无提示覆盖；选择一个供旧消费端展示的primary，并把其余Slice的事实完整放到 `unselected_slice_facts`。多Anchor标记为 `needs_owner_verification`，**不是**控制归属已验证。无EQ Anchor的多份Base Logic同样保留。
- `tests/test_step_fact_projection.py`：测试FB实参/证据路径保真、状态变量家族隔离、重复Anchor不冒充owner、布尔括号及未知分支。

## 重要未完成项

仓库当前main只有四个无扩展名的 `vendor/tools` 脚本，没有 `vendor/lib/canonical_ir.py`、`step_logic_slicer.py`、Decoder、DefUse、`step_graph.py`、`primitives_canonical.py`、`bxcj_demo/run.py` 等依赖。因而此PR**不是可运行的完整PLC工作流重构**，并未完成三项目回归。后续需要将这些源码及测试同步入仓库，再接入真实PLC数据验证。

另外，旧版StepGraph消费的扁平字段只能表达一个展示投影，不能以其取代 `slice_occurrences` 的全量事实。未来应：
1. 在Canonical IR中统一 `(source_file, unit, network_id, element_id)` 节点身份，修复SMC2重复Ix碰撞；
2. 基于状态写入和拓扑关系判定 `owner_step` 与 `referenced_step`，不能把所有相同EQ当控制owner；
3. 改造Slicer和StepGraph以消费多网络事实，而不仅是一个primary；
4. 用真实原始XML检验Mylar `m_nNextAutoStep[3]=110`、`[28]=310`，转接片EM01 Ps[2]和超声ST01；
5. 做静态事实、FB描述证据、非法跨网络Transition三类独立统计。

## 兼容注意

`vendor/tools/build_step_graph` 保留了仓库既有无扩展名路径，和用户本地 `vendor/tools/build_step_graph.py` 不是同一个文件名。外部消费者仍需确认执行入口和路径。同名 `lib` 包要求上层把 `vendor` 加入Python导入路径；仓库尚无完整Python包安装文件。

**验收限制**：本批没有运行缺失依赖的整套Pipeline、未得到任何三项目新准确率，不能声称完成P0全部问题或已证明代码对三项目不退化。
