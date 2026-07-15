# 线上故障根因定位（RCA）生产级方案

> 目标：告警发生后，agent 不是"猜测泛泛而谈"，而是基于**实际日志、实际变更、实际代码结构**给出带证据链的根因结论。本方案把"不猜测"从愿望变成机制。

## 1. 总体架构

```
                        ┌─ 线 1：日志取证 ──────────────┐
  告警(alert.json) ──┬──┤  ES/SLS 聚合 → 堆栈解析 → 指纹聚类 │──┐
  · incident_id     │  └──────────────────────────────┘  │
  · service         │        （两条线互不依赖，并行）        ├─→ 线 3：汇合裁决 ─→ 反驳验证 ─→ 知识回写
  · alert_time      │  ┌─ 线 2：变更取证 ──────────────┐  │   (adjudicator)    (agent层)   (kb)
  · deployed_commit └──┤  git / 配置审计 / DDL / 基础设施  │──┘
  · business_packages  └──────────────────────────────┘
                                     ↑
                          code graph（project-map / 内置构建器）
```

- **线 1、线 2 并行**：都只依赖告警上下文，产出结构化取证结果；
- **线 3 汇合**：集合运算式裁决（见 §4），允许一轮回查（堆栈指向上游服务时回到线 1 查上游）；
- **反驳验证**：CONFIRMED 结论须经独立反驳（agent 层，见 §7），可用 mock-harness 本地复现；
- **知识回写**：定案后"指纹 → 根因 → 证据链"入库，同指纹告警下次直接命中历史案例。

## 2. 数据契约（`rca/schemas.py`）

| 对象 | 关键字段 | 说明 |
|---|---|---|
| `ErrorSignature` | fingerprint / exception_type / top_business_frame / **first_seen** / count | 指纹 = 异常类型 + 顶层业务帧符号（**不含行号**，跨版本稳定）；异常类型取最深层 `Caused by`；first_seen 是与线 2 对齐的 join key |
| `CandidateChange` | change_id / change_type(code\|config\|db\|infra) / timestamp / relation / touched / keys | touched 为变更后版本的行号区间；relation 标注与故障服务的关联路径 |
| `Verdict` | tier / mechanism / change_id / evidence_chain / next_actions | 结论对象，受守门校验约束 |
| `RcaReport` | mitigation / verdicts / candidates / kb_matches / warnings | **止血建议与根因分析分离** |

## 3. 三条线的落地细节

### 线 1：日志取证（`log_forensics.py` + `log_sources.py`）
- agent 不 grep 原始日志：先聚合（指纹聚类、量级统计），只取样本堆栈进上下文；
- 框架帧过滤（spring/dubbo/netty/mybatis/…），支持 `business_packages` 白名单；
- `Caused by` 链折叠到最深层——wrapper 异常不是根因；
- **日志源 adapter（已实现）**：文件 / 导出命令（`--log-cmd`，仅限操作者输入，
  禁止拼接告警内容——注入面）/ Elasticsearch（`--es-*`，按告警时间开窗查
  ERROR/FATAL，把 `@timestamp/level/message/stack_trace` 重组为解析器行格式，
  截断时显式告警量级失真）；三源可叠加，解析器不感知来源差异；时区统一 UTC。

### 线 2：变更取证（`change_sources.py`）
- 以 **first_seen 为锚精确开窗** `[first_seen − window, first_seen]`——错误之后的变更被硬性排除（测试 `test_window_and_relation` 守护）；
- git 源：`git log <deployed_commit>` 只取发布版本可达的提交，`--unified=0` 的 hunk 解析出变更后行号区间；
- 审计源：配置/DB/基础设施变更走 JSON 契约；**审计 adapter（已实现）**：
  `rca audit-convert --format apollo|nacos|ddl` 把 Apollo 发布历史 / Nacos 配置
  历史 / DDL 工单导出归一到该契约，`--append` 按 id 去重做多源合并，
  未标注服务的 DDL 默认 `relation=shared-db`（宁进候选，不可漏）；
- 容易漏的变更源已入清单：上游服务发布、共享库版本、扩缩容/证书/网络（audit 的 `relation` 字段表达关联路径）。

### 线 3：汇合裁决（`adjudicator.py` + `code_graph.py`）
见 §4、§5。

## 4. 裁决规则：集合运算代替自由发挥

对每个错误签名：

1. 报错业务帧 → code graph 节点 → **故障可达代码集**（先沿调用图上溯 5 层得到调用路径，再从整条路径下钻 2 层——覆盖祖先旁支被调方，它们可能制造导致报错点故障的状态；过近似：宁多勿漏）；
2. 对每个候选变更判定机制：

| 机制 | 判定条件 | 结论分级 |
|---|---|---|
| `DIRECT` | 变更 diff 行区间 ∩ 堆栈帧(文件:行号) ≠ ∅，或 hunk 函数上下文命中报错方法 | **CONFIRMED**（需通过版本锚定） |
| `GRAPH` | 变更符号 ∈ 故障可达代码集 | LIKELY |
| `TEMPORAL` | 配置/DB/infra 变更，关联路径 ∈ {same-service, upstream, shared-lib, shared-db}，时间在窗口内；变更键命中日志词面则加权 | LIKELY |
| `NONE` | 以上全不满足 | 不归因 |
| 全部 NONE | —— | **HYPOTHESIS**：禁止归因任何变更，必须输出下一步排查动作 |

3. 排序：DIRECT > GRAPH > TEMPORAL(带 key_match) > TEMPORAL；全部候选保留在 `ranked_candidates` 供人工复核。

### 守门校验（`validate_verdict`，违反即抛 `GuardrailViolation`）
- HYPOTHESIS 携带 change_id → 拒绝（这就是被禁止的"硬凑根因"）；
- HYPOTHESIS 无 next_actions → 拒绝（无变更故障必须有 fallback 路径：流量/数据/第三方/周期任务/资源耗尽）;
- CONFIRMED 机制非 DIRECT → 拒绝；
- CONFIRMED 证据链缺 stack_frame 或 diff_hunk → 拒绝。

## 5. 版本锚定（解决"行号对不上"）

故障时刻线上跑的是 `deployed_commit`，不是本地 HEAD：
- 变更收集以 `deployed_commit` 为 ref，只看发布版本可达的提交；
- DIRECT 命中后，**命中的那一帧**回查 `git show deployed_commit:file` 的真实源码行，锚定成功才给 CONFIRMED，证据链附上源码原文（`code_anchor`）；
- 锚定失败（行号越界/文件不存在 = 版本漂移）→ 自动降级 LIKELY + 显式告警（测试 `test_anchor_failure_downgrades_to_likely` 守护）；
- code graph 可用 `--code-graph` 挂预构建产物（project-map 索引），要求同样锚定到发布 commit。

## 6. 止血与根因分离

报告第一节永远是止血建议，优先级高于根因细节：
- CONFIRMED 代码变更 → 【立即】回滚该变更（回滚优先于修代码）；
- LIKELY 配置/DB → 【评估】回滚该变更；
- 仅 HYPOTHESIS → 【兜底】限流/降级/扩容 + 按 next_actions 排查。

## 7. Agent 编排层（`skills/incident-rca/SKILL.md`）

确定性引擎负责"算"，agent 负责"读、解释、反驳"：
1. 从告警系统组装 `alert.json`（trace_id/服务名/时间窗/发布 commit）；
2. 跑 `rca run` 得到分级结论；
3. **补齐行为差异解释**：对 CONFIRMED 结论，读 diff 与锚定源码行，写出"变更前 X、变更后 Y、故障输入 Z 时行为差异如何导致该异常"；
4. **反驳验证**：独立视角尝试推翻结论（时间线矛盾？其他签名反例？diff 语义无害？），可用 mock-harness 重放故障输入复现；
5. 反驳不成立 → `rca kb-add` 定案回写（含最终解释）；反驳成立 → 降级并回到候选列表。

## 8. 知识闭环（`knowledge_base.py`）

- 运行时按指纹查历史案例（同指纹故障直接提示历史根因与处置）；
- 只回写 CONFIRMED/LIKELY，**HYPOTHESIS 不入库**（未定案猜想会污染检索）；
- 按 incident_id 幂等，`kb-add` 定案精修覆盖初稿；
- 生产上可把 JSON 后端换成 graphify 知识图谱，契约不变。

## 9. 测试与验证（当前状态：71/71 通过 + E2E 演示 8/8 断言通过）

| 层 | 测试 | 守护的方案要点 |
|---|---|---|
| 线 1 | test_log_forensics（10） | 堆栈解析、Caused by 折叠、框架过滤、指纹跨行号漂移稳定、多流交织拆流重组 |
| 日志源 | test_log_sources（6） | ES 查询体构造/Basic 认证/重组文本直通解析器/截断告警；命令源免 shell 执行 |
| 线 2 | test_change_sources（6） | 窗口排除 baseline/事后变更、hunk 行区间覆盖报错行 |
| 审计源 | test_audit_adapters（4） | Apollo/Nacos/DDL 归一契约、service_map 映射、shared-db 兜底、直通线 2 |
| graph | test_code_graph（5） | 调用边、可达集（路径下钻语义）、JSON 契约往返 |
| project-map | test_project_map_adapter（6） | 方法级节点行区间、类级边方法展开、与内置图合并互补（跨模块可达） |
| 线 3 | test_adjudicator（13） | 四种机制、DIRECT>TEMPORAL 排序、锚定降级、四条守门规则、止血生成 |
| KB | test_knowledge_base（6） | 回写/查询/幂等/定案精修/HYPOTHESIS 不入库 |
| E2E | test_e2e（8）+ demo/run_demo.py | 合成故障全流程：NPE→CONFIRMED 归因坏提交并建议回滚；超时→HYPOTHESIS 不硬凑；无关配置不背锅 |
| fan-out | test_fanout（5） | 上游共享库坏提交 DIRECT 胜出并在上游仓锚定；主仓提交保留为 GRAPH 候选；relation=upstream；止血指向上游 |
| 上游推导 | test_upstream_discovery（5） | 多模块 pom 依赖收集、仓内自依赖排除、workspace 匹配、DTD pom 拒绝 |

复现：项目根目录 `python3 -m unittest discover` 与 `python3 demo/run_demo.py`。
另有 CLI 冒烟链路：`audit-convert`（Apollo+DDL 双源合并）→ `graph-from-project-map`
（symbols/calls + 内置图合并）→ `run --log-cmd`，验证同服务"调低超时"配置变更被
正确判为超时错误的 LIKELY(TEMPORAL) 候选。

## 10. 已知局限与生产化路线

- 内置 Java 解析器是方法级正则解析（构造函数/lambda/重载不精确）——已支持
  `graph-from-project-map` 挂 AST 级索引（tree-sitter），并与内置图合并互补
  （project-map 缺模块内边、内置缺跨模块边）；字节码级调用图（jdeps/soot）仍可经
  `--code-graph` JSON 契约接入；
- **多流交织日志（已实现）**：`--stream-prefix-re` 按行前缀（kubectl --prefix /
  docker compose 等）拆流后分别解析再合并聚类；前缀只打在事件首行、堆栈行无标记
  的交织在文本层不可恢复——该场景走 ES 源（每文档自含完整堆栈）；
- **跨仓 fan-out（已实现）**：`--upstream name=path[@commit]`（可重复）把上游
  服务/共享库仓库纳入变更取证（relation=upstream）、code graph 跨仓合并与版本
  锚定——堆栈指向上游代码时可直接 CONFIRMED 归因上游提交；
- **上游清单自动推导（已实现）**：`suggest-upstreams --repo … --workspace …`
  解析服务仓多模块 pom.xml 依赖、匹配 workspace 各仓库 artifactId，输出可直接
  粘贴的 `--upstream` 参数（pom 解析拒绝 DTD，防 XXE/实体膨胀）；Gradle 与
  仅存在于制品库的依赖不在覆盖范围，需发布系统元数据回溯源仓库；
- 审计源覆盖度决定线 2 上限：未纳管的变更（手工改配置、直连改库）永远查不到——这正是 HYPOTHESIS 的 next_actions 里"确认未纳管变更源"一条存在的原因。
