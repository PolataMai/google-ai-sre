# changeflow — 统一变更数据平台

代码发布、配置修改、数据库变更、中间件变更、基础设施变更 → 同一条变更时间线。
变更前风险画像与准入，变更中指标偏移观测，变更后自动验收；异常时把指标异常与
最近变更做可解释关联，并导出给 [rca 引擎](../rca/)做证据链级根因定位。

方案全文见 [DESIGN.md](DESIGN.md)。零第三方依赖（Python ≥ 3.10）。

## 快速开始

```bash
python3 -m unittest discover          # 22 个测试
python3 demo/run_demo.py              # 全生命周期演示（自动使用 mall-dill 真实依赖图）
python3 -m changeflow.cli --help      # ingest-git / ingest-audit / ingest-json /
                                      # timeline / profile / precheck / watch /
                                      # accept / correlate / export-rca-audit
```

## 目录

```
changeflow/    schemas（五源统一契约）/ timeline / deps（依赖图）/
               ingest / risk（五因子画像）/ gates（三道门+关联）/ cli
tests/         unittest 套件 + 夹具
demo/          run_demo.py 端到端演示
```

## 与生态的衔接

- 依赖图 ← mall-dill `knowledge/indexes/services.json`（`--services-json`，Feign/MQ 边）
- 进程内调用边 ← project-map `calls.json`（`--calls-json`，补单体模块直调盲区）
- 历史故障 ← rca 定案记录（`--incidents`）
- 嫌疑清单 → rca `--audit`（`export-rca-audit`）
