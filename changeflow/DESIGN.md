# changeflow — 统一变更数据平台（生产级方案）

> 出发点：线上大部分故障与变更强相关，但代码发布、配置、DB、中间件、基础设施变更
> 分散在不同系统。本平台把五类变更纳入**同一条时间线**，围绕它做变更前评估、
> 变更中观测、变更后验收与异常关联，并与 rca 引擎、mall-dill 知识库构成闭环。

## 1. 在三件套里的位置

```
mall-dill 知识库（事实层）──services.json 依赖图──▶ changeflow（变更全生命周期）
                                                        │ export-rca-audit
故障发生 ◀── 一线告警 ──── correlate 嫌疑清单 ──────────▶ rca 引擎（证据链级根因定位）
                                                        │ kb-add 定案
                                              incidents 历史故障 ──▶ 风险画像 history 因子
```

- 知识库供给**依赖图**（爆炸半径的计算基础）；
- rca 供给**历史故障**（风险画像因子）并消费**嫌疑清单**（audit.json 契约无缝衔接）；
- 三者各自独立可用，契约对接，无编译耦合。

## 2. 统一变更契约（`schemas.py`）

`ChangeEvent`：五源（code/config/db/middleware/infra）归一，`details` 按源携带
语义（files/keys/tables/components），下游（画像/关联/导出）只依赖统一字段——
**新增变更源不改下游**。灰度与回滚是变更的"声明"字段，由 precheck 门核查声明
与风险等级是否匹配。状态机 planned → in_progress → done → rolled_back，
已回滚的变更不参与异常关联（它不在场）。

## 3. 时间线（`timeline.py`）

Append-only JSONL，一行一事件；同 change_id 后写覆盖（status 流转），
审计友好（git diff 可读）。查询：时间窗/服务/源过滤；`window_before` 是
异常关联的标准窗口。量大后可平移 SQLite/ES，查询接口不变。

## 4. 风险画像（`risk.py`）——五因子规则式，可解释可审计

| 因子 | 规则要点 | 证据形态 |
|---|---|---|
| scope 范围 | 文件数分档；DDL 恒 20；配置键数分档 | 具体文件/键/表清单 |
| blast_radius | 依赖图反向 BFS（限深 3），4 分/服务封顶 20 | 下游服务点名 |
| core_link | 自身或爆炸半径命中核心链路服务 +20 | 命中的核心服务 |
| history | rca 定案的历史故障次数 5 分/次封顶 15 | 次数 |
| coverage | <50% +15，<70% +8，无数据按未知从严 +10 | 覆盖率数值 |
| timing | 周五 16 点后 +10 / 周末 +8 / 凌晨全量 +5 | 具体时刻 |
| capability | 未声明灰度 +5 / 未声明回滚 +10 | 声明缺失项 |

≥60 HIGH，≥35 MEDIUM。**v1 故意不用模型**：变更准入是要跟人对质的场景，
每一分都必须有 evidence；分数只做排序，拦截决策在门里。

## 5. 三道门（`gates.py`）

**事前 precheck**：影响范围清单、高风险依赖点名（爆炸半径∩核心链路）、
回滚能力（HIGH 无回滚 → BLOCK）、灰度能力（HIGH 的代码/配置无灰度 → BLOCK；
DB/infra 用回滚兜底）。退出码即门禁信号，可直接卡流水线。

**事中 watch**：基线窗 [t-30m, t) vs 观察窗 (t, t+30m]，均值漂移检测。
**双条件**（相对变化 ≥25% 且 z ≥3）——只用 z 会在方差极小的平稳指标上误报，
只用相对值会漏低基数指标；测试里两个方向都有守护用例。

**事后 accept**：漂移检测 + 硬阈值规则（{metric: {max/min}}）；
**验收窗口无数据本身即不通过**（观测缺失不是通过的理由）。REJECTED 时给出
下一步：correlate + 按声明的回滚方案回滚。

**异常关联 correlate**：时间邻近（≤1h 40 分 / ≤6h 30 分 / ≤24h 20 分）
+ 服务关系（同服务 30 / 上游依赖 20 / 爆炸半径覆盖 20）+ 风险画像分/5，
输出可解释 reasons 的嫌疑排序；深度定位交给 rca（`export-rca-audit` 产出
rca `--audit` 直接可用的文件）。

## 6. 接入方式

```bash
# 汇入：git 提交、rca 生态审计（Apollo/Nacos/DDL）、发布/中间件/infra 通用 JSON
changeflow ingest-git --repo <repo> --service order --since 2026-07-10 --timeline tl.jsonl
changeflow ingest-audit --input audit.json --timeline tl.jsonl
changeflow ingest-json  --input deploys.json --timeline tl.jsonl

# 三道门（退出码可直接接 CI/发布系统门禁）
changeflow precheck --timeline tl.jsonl --change-id rel-1 \
    --services-json <mall-dill>/knowledge/indexes/services.json \
    --core-services order,pay,seckill --coverage cov.json --incidents inc.json
changeflow watch  --timeline tl.jsonl --change-id rel-1 --metrics metrics.json
changeflow accept --timeline tl.jsonl --change-id rel-1 --metrics metrics.json --rules rules.json

# 告警来了
changeflow correlate --timeline tl.jsonl --at "…T14:23:05" --service order --services-json …
changeflow export-rca-audit --timeline tl.jsonl --out rca-audit.json
```

## 7. 测试与验证（25/25 通过 + 演示全流程断言通过）

- timeline：幂等/覆盖/窗口/compact；deps：真实结构 services.json 的边方向与传导半径；
- risk：HIGH/LOW 全因子场景、DDL、证据非空强制；
- gates：BLOCK/放行、漂移双条件的两类误报守护、验收缺数据即失败、
  关联排序（真凶第一、已回滚排除）、rca 导出契约；
- deps 补边：module_edges 加载、*-api 归一、类级边聚合兜底、
  merge 后进程内直调盲区闭合（order 经 product 变更进入爆炸半径并传导到 points）；
- demo（`demo/run_demo.py`）：用 **mall-dill 真实依赖图 + 真实 calls.json**
  跑完整生命周期（BLOCK → 补声明放行 → DRIFTED → REJECTED → 真凶排第一且
  点名"上游依赖 order → product" → 导出 rca audit）。

## 8. 已知边界

- **进程内调用边（已补齐）**：mall-dill 单体形态下 order→product 是模块直调，
  Feign/MQ 边抓不到；`--calls-json` 把 project-map calls.json 的 AST 级
  `module_edges` 并入依赖图（`from_project_map_calls` + `merge`，*-api 契约
  模块归一到服务本体，老版本产物无 module_edges 时从类级边聚合兜底）。
  该边 confidence 为 best-effort，按"宁可多一条关联，不可漏真凶通路"并入；
- 指标 adapter 是拉取式 JSON——生产接 Prometheus 只需写一个导出脚本，
  契约（{metric: [{ts, value}]}）不变；
- timing 因子未含大促日历——把公司发布窗口/封网期做成配置即可扩展；
- 风险分权重是启发式初值，应随定案回写（rca kb → incidents）持续校准。
