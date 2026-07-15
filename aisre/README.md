# aisre —— AI SRE MVP(第 1–4 周交付)

依据 [ai-sre/google-ai-sre-能力与实现方案.md](../ai-sre/google-ai-sre-能力与实现方案.md)
(Google AI SRE 文章的落地方案)完成 12 周计划中前两个周期的交付:

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

## 运行

```bash
python3 -m unittest discover        # 全部测试(94 个)
python3 demo/run_demo.py            # 端到端演示(接入→采集→证据库→丰富→动作→审批→基线)
python3 -m aisre.cli scenarios      # 列出场景定义
python3 -m aisre.cli intake --file webhook.json --format alertmanager
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
  cli.py             五个子命令(输出 JSON,违规时退出码 1,格式错误退出码 2)
tests/               unittest 套件(TDD,逐模块 red-green)
demo/                端到端演示 + 样例数据
```

## 后续(第 5–6 周)

告警丰富链路(90 秒预算编排)、Top-3 假设生成、事故工作台、120 秒 p95 优化——
接入/采集/证据三件套已就绪,丰富链路是它们之上的编排层。
