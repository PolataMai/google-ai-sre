# aisre —— AI SRE MVP(第 1–2 周交付)

依据 [ai-sre/google-ai-sre-能力与实现方案.md](../ai-sre/google-ai-sre-能力与实现方案.md)
(Google AI SRE 文章的落地方案)完成 12 周计划中第 1–2 周的四项交付:

| 交付 | 模块 | 要点 |
|---|---|---|
| 场景定义 | `aisre/scenarios.py` | 三类诊断场景封闭注册表:检测信号 + 验证步骤 + 动作白名单 |
| 事件 Schema | `aisre/schemas.py` | 证据/事实/假设分离;无证据事实被拒;覆盖率可对任意来源数据计算 |
| 动作 Schema | `aisre/actions.py` | 类型化 ActionPlan;扩容 10%–25% 边界;TTL + 强制 dry-run;审批绑定 plan_hash |
| 服务目录 | `aisre/catalog.py` | 试点准入(Tier-1 无状态 K8s);自治 scope = 服务+场景+动作+环境,默认 SHADOW |
| 90 天基线 | `aisre/baseline.py` | MTTM 中位数/p75(nearest-rank)、变更失败率,窗口过滤,open 事故单独计数 |

## 运行

```bash
python3 -m unittest discover        # 全部测试(64 个)
python3 demo/run_demo.py            # 端到端演示
python3 -m aisre.cli scenarios      # 列出场景定义
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
  scenarios.py    三类场景注册表(cause_code 封闭枚举)
  schemas.py      Evidence / Fact / Hypothesis / Enrichment + 守门校验
  actions.py      ActionPlan / plan_hash / Approval + 校验
  catalog.py      ServiceCatalog / AutonomyLevel / scope_key
  baseline.py     IncidentRecord / ChangeRecord / compute_baseline
  cli.py          四个子命令(输出 JSON,违规时退出码 1)
tests/            unittest 套件(TDD,逐模块 red-green)
demo/             端到端演示 + 样例数据
```

## 后续(第 3–4 周)

告警接入(F01)、五类只读连接器(F02)、证据存储落地——本期 Schema 即其数据契约。
