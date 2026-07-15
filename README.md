# google-ai-sre — AI SRE 工程实践仓库

以 Google SRE 文章 [*AI in SRE: How Google is Engineering the Future of Reliable
Operations*](https://sre.google/resources/practices-and-processes/ai-engineering-reliable-operations/)
为蓝本的 AI SRE 落地工程:一份能力提炼与实现方案,加三个可运行、全测试覆盖的
Python 引擎。零第三方依赖(Python ≥ 3.10),纯 stdlib + unittest。

## 仓库结构

| 目录 | 定位 | 测试 |
|---|---|---|
| [ai-sre/](ai-sre/) | 方案文档:文章能力全景 → 逐项实现方案 → 三组件架构 → 12 周交付计划 | — |
| [aisre/](aisre/) | AI SRE MVP(12 周计划第 1–8 周交付):契约/取证/丰富/工作台 + 回放评测/Gold/Shadow/指标看板 | 158 |
| [rca/](rca/) | 故障根因定位引擎:日志+变更并行取证 → code graph 集合运算裁决 → 证据链分级结论 | 71 |
| [changeflow/](changeflow/) | 统一变更数据平台:五源变更时间线、风险画像、三道门准入、异常-变更关联 | 22 |

三个引擎对应文章能力图谱的不同环节:

```text
告警/事故
  │
  ├─ aisre        数据契约层:事实必须带证据、动作是类型化契约、
  │               自治按 服务×场景×动作×环境 粒度授权(对标 Actus/评测体系的地基)
  │
  ├─ rca          Investigate:三线取证 + 裁决,产出 CONFIRMED/LIKELY/HYPOTHESIS
  │               分级根因与证据链(对标 Incident Hypothesis / InvD)
  │
  └─ changeflow   变更数据源:发布/配置/DB/中间件/基础设施统一时间线,
                  变更失败率口径与风险门禁(对标发布连接器与变更关联)
```

## 快速开始

```bash
# 各模块独立运行,进入目录即可
cd aisre      && python3 -m unittest discover && python3 demo/run_demo.py
cd rca        && python3 -m unittest discover && python3 demo/run_demo.py
cd changeflow && python3 -m unittest discover && python3 demo/run_demo.py

# CLI 入口
python3 -m aisre.cli --help        # scenarios / baseline / validate-plan / validate-enrichment
python3 -m rca.cli --help          # run / suggest-upstreams / ...
python3 -m changeflow.cli --help   # ingest-* / timeline / profile / precheck / ...
```

## 设计原则(取自文章,三条不可妥协)

1. **渐进授权** —— 自治权按 `服务×场景×动作` 逐格开启,用数据晋级,出事自动降级,不给全局 L3;
2. **推理与执行分离** —— 模型只能"提案",执行永远经过确定性的、人类控制的独立网关;
3. **评测先于自治** —— 没有 Gold 数据和统计显著的精度证明就没有 L3,评测用确定性精确匹配。

验收底线:每条事实可追溯、每个动作可逆、每项指标能从审计事件计算。

## 路线图

MVP 按 [12 周计划](ai-sre/google-ai-sre-能力与实现方案.md#七12-周交付计划)推进:

- [x] 第 1–2 周:场景定义、90 天基线、服务目录、事件与动作 Schema(`aisre/`)
- [x] 第 3–4 周:告警接入、五类只读连接器、证据存储(`aisre/`)
- [x] 第 5–6 周:告警丰富、Top-3 假设、事故工作台、120 秒链路(`aisre/`)
- [x] 第 7–8 周:Gold 数据、时间切片回放、指标看板、Shadow(`aisre/`)
- [ ] 第 9–10 周:独立执行网关、OPA、身份、审批、两个 L2 动作
- [ ] 第 11–12 周:Guardian、Kill Switch、审计、故障注入、生产 Shadow

## 工程约定

- 纯 Python stdlib,无第三方依赖;`python3 -m unittest discover` 即可全量验证;
- 每个模块自带 `README.md`(用法)、`DESIGN.md`(设计取舍)、`demo/run_demo.py`(端到端演示);
- 新功能一律 TDD:先写失败测试,再写最小实现。
