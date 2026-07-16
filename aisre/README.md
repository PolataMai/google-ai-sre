# aisre —— AI SRE MVP(第 1–12 周交付,开发完成)

依据 [ai-sre/google-ai-sre-能力与实现方案.md](../ai-sre/google-ai-sre-能力与实现方案.md)
(Google AI SRE 文章的落地方案)完成 12 周计划全部六个周期的交付:

**第 1–2 周(数据契约层)**

| 交付 | 模块 | 要点 |
|---|---|---|
| 场景定义 | `aisre/scenarios.py` | 三类诊断场景封闭注册表:检测信号 + 验证步骤 + 动作白名单 |
| 事件 Schema | `aisre/schemas.py` | 证据/事实/假设分离;无证据事实被拒;覆盖率可对任意来源数据计算 |
| 动作 Schema | `aisre/actions.py` | 类型化 ActionPlan;扩容 10%–25% 边界;TTL + 强制 dry-run;审批绑定 plan_hash |
| 服务目录 | `aisre/catalog.py` | 试点准入(Tier-1 无状态 K8s);自治 scope = 服务+场景+动作+环境,默认 SHADOW |
| 90 天基线 | `aisre/baseline.py` | MTTM 中位数/p75(nearest-rank)、变更失败率,窗口过滤,open 事故单独计数 |

**第 3–4 周(接入与取证层)**

| 交付 | 模块 | 要点 |
|---|---|---|
| 告警接入 F01 | `aisre/intake.py` | Alertmanager/PagerDuty/自研三格式归一;确定性 incident_id;幂等去重;新事故触发工作流 |
| 只读连接器 F02 | `aisre/connectors.py` | metrics/logs/trace/release/topology 五源并行;单源失败不阻塞;异常一次受控重试;超时标缺失 |
| 证据存储 F03 | `aisre/evidence_store.py` | 按事故落盘、追加不可覆盖、sha256 完整性校验、直接吞采集结果 |

**第 5–6 周(丰富与调查层)**

| 交付 | 模块 | 要点 |
|---|---|---|
| 事实抽取 | `aisre/facts.py` | 六条规则从证据快照确定性提取事实(阈值为常量,同证据同事实,天生绑定证据 id) |
| Top-3 假设 F04 | `aisre/hypotheses.py` | 三场景确定性打分;支持/反对证据;时序矛盾(错误早于发布)自动进反证;验证步骤取自场景定义 |
| 告警丰富编排 | `aisre/enrichment.py` | 采集→入库→事实→Top-3→守门→发布;缺失源先发布后追加(refresh);分段计时;p95 口径=告警到发布墙钟 |
| 事故工作台 F05 | `aisre/workbench.py` | 单一视图:时间线对齐/数据源状态/带链接事实/Top-3/建议动作;Markdown 渲染可直接贴事故平台 |

**第 7–8 周(评测与准入层)**

| 交付 | 模块 | 要点 |
|---|---|---|
| Gold 数据流程 | `aisre/gold.py` | 关单时从实际执行动作预填建议;接受/修改/拒绝;追加式 JSONL,按事故取最新 |
| Shadow 计划器 | `aisre/planner.py` | Top-1 置信 ≥0.8 才生成;参数只取自事实(缺参数拒绝不猜);产出必过契约校验 |
| 时间切片回放 F11 | `aisre/replay.py` | 录制快照重放同一套线上代码路径;缺源重放为当时不可用;ShadowLog 计数服务 500 例门槛 |
| 评测 | `aisre/evaluation.py` | Top-3 召回率(≥85%)/Top-1 准确率/L2 精确匹配率(≥95%,全等才算);无 Gold 不进分母 |
| 指标看板 F13 | `aisre/board.py` | 业务/Agent/安全/准入四区全部从记录计算;任一安全事件否决 L3 资格 |

**第 9–10 周(执行与安全层)**

| 交付 | 模块 | 要点 |
|---|---|---|
| Agent 身份 | `aisre/identity.py` | 短时 HMAC 签名令牌;agent/human 主体机器可区分;过期/篡改/错密钥显式拒绝 |
| 策略引擎 | `aisre/policy.py` | OPA 语义替身:默认拒绝、数据驱动规则、决策带策略版本;动作目录/命名空间/爆炸半径三条内置 |
| 执行网关 F08 | `aisre/gateway.py` | 12 环检查链;提交者必须 agent、审批人必须 human;幂等重放不二次执行;红色按钮;全程审计;fail closed |
| 晋降级状态机 | `aisre/catalog.py` | SHADOW→L2→L3 逐级晋升不可跳级;任意态可挂起;降级后禁直恢复 L3 |

**第 11–12 周(守护与生产 Shadow 层)**

| 交付 | 模块 | 要点 |
|---|---|---|
| Guardian F09 | `aisre/guardian.py` | 执行后按观测序列守护 SLI;成功放行,恶化/超时自动回滚补偿动作 + 熔断 scope;fail closed(拿不到 SLI 也止血) |
| 故障注入演练 | `tests/test_fault_injection.py` | 两个 L2 动作注入恶化均回滚(通过率 100%);回滚熔断后网关在 autonomy 环拒绝后续,端到端闭环 |
| 生产 Shadow F11 | `aisre/shadow.py` | 对真实告警只生成计划记入 ledger、绝不执行("不执行"结构性保证:不 import gateway);累积案例服务 500 例准入门槛 |

> 开发完成 = 能力具备;业务指标仍须经 ≥8 周 L2 生产试点用真实数据验证,不预先宣称达标。

**开发完成后的加固**

| 加固 | 模块 | 要点 |
|---|---|---|
| 成功条件结构化 | `aisre/actions.py` | `SuccessCriterion {metric,op,threshold}` 契约层归一;非法格式构造时响亮报错,不再静默变成 Guardian 永久超时回滚;Guardian 去掉自有正则 |
| L3 准入门禁 | `aisre/admission.py` | 把"开发完成 ≠ 指标达标"变成代码强制:9 道数据门只能靠真实试点数据放行;board 的四项只是"就绪预览"不冒充授权 |
| 准入接入状态机(PK 复审) | `aisre/admission.py` + `catalog.py` | 升 L3 唯一入口 `promote_to_l3`:重算门禁 + 两个不同的已验证人类主体;`set_level` 对 L3 一律拒绝;可派生指标从记录算(`derive_pilot_counts`) |

## 运行

```bash
python3 -m unittest discover        # 全部测试(256 个)
python3 demo/run_demo.py            # 13 步全链路(接入→丰富→工作台→网关→Guardian→Shadow→看板→准入门禁)
python3 -m aisre.cli scenarios      # 列出场景定义
python3 -m aisre.cli intake --file webhook.json --format alertmanager
python3 -m aisre.cli replay --cases demo/data/replay_cases.jsonl
python3 -m aisre.cli admission --file pilot_metrics.json   # L3 数据门,达标退出 0(授权另须 promote_to_l3 双人批准)
python3 -m aisre.cli baseline --incidents demo/data/incidents.jsonl \
    --changes demo/data/changes.jsonl --as-of 2026-07-15T00:00:00Z
python3 -m aisre.cli validate-plan --file plan.json --now 2026-07-15T10:12:00Z \
    --scenario RECENT_RELEASE_REGRESSION
python3 -m aisre.cli validate-enrichment --file enrichment.json
```

纯 stdlib,无第三方依赖(与 rca / changeflow 同约定)。

## 目录

```
aisre/
  scenarios.py       三类场景注册表(cause_code 封闭枚举)
  schemas.py         Evidence / Fact / Hypothesis / Enrichment + 守门校验
  actions.py         ActionPlan / plan_hash / Approval + 校验
  catalog.py         ServiceCatalog / AutonomyLevel / scope_key
  baseline.py        IncidentRecord / ChangeRecord / compute_baseline
  intake.py          Webhook 归一 / IntakeService(幂等去重 + 工作流触发)
  connectors.py      五类只读连接器 / collect_context 并行采集
  evidence_store.py  EvidenceStore(追加式 + sha256 完整性)
  facts.py           规则化事实抽取(六条规则,阈值常量)
  hypotheses.py      Top-3 假设引擎(确定性打分 + 反证)
  enrichment.py      丰富编排(部分发布/追加/分段计时/p95)
  workbench.py       事故工作台(结构化视图 + Markdown 渲染)
  gold.py            Gold 标注流程(预填建议 + 追加式存储)
  planner.py         Shadow 计划器(只生成不执行)
  replay.py          时间切片回放 + ShadowLog
  evaluation.py      Top-3 召回 / Top-1 准确 / L2 精确匹配
  board.py           指标看板(四区 + L3 准入门槛)
  identity.py        Agent/Human 主体令牌(HMAC 短时签名)
  policy.py          策略引擎(OPA 替身,默认拒绝)
  gateway.py         执行网关(12 环检查链 + 红色按钮 + 审计)
  guardian.py        执行后守护(观测序列 + 自动回滚 + 熔断)
  shadow.py          生产 Shadow(只生成计划记 ledger,不执行)
  admission.py       L3 准入(9 道数据门 + promote_to_l3 双人批准唯一入口)
  cli.py             七个子命令(输出 JSON,违规时退出码 1,格式错误退出码 2)
tests/               unittest 套件(TDD,逐模块 red-green)
demo/                端到端演示(13 步全链路)+ 样例数据
```

## 后续(开发完成之后)

MVP 开发已收口,进入 ≥8 周 L2 生产试点:累积真实执行、连续 8 周核心指标
达标、故障注入与回滚演练通过率 100% 后,才为单个"服务×场景×动作"开 L3。
业务指标(MTTM、变更失败率)必须用真实试点数据对照 90 天基线验证,
不因开发完成而预先宣称达标。
