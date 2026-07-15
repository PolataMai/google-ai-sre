# rca — 线上故障根因定位引擎

告警 → 日志取证 + 变更取证（并行）→ code graph 集合运算裁决 → 带证据链的分级结论（CONFIRMED / LIKELY / HYPOTHESIS）→ 止血建议 → 知识库闭环。

方案全文见 [DESIGN.md](DESIGN.md)。零第三方依赖（Python ≥ 3.10 + git）。

## 快速开始

```bash
# 跑测试（71 个）
python3 -m unittest discover

# 跑端到端合成故障演示（构造假仓库+故障日志，全流程出报告）
python3 demo/run_demo.py

# 真实使用
python3 -m rca.cli run \
  --alert alert.json \                # incident_id/service/alert_time/deployed_commit/business_packages
  --logs app.log \                    # 可多个
  --repo /path/to/service-repo \      # 代码变更源 + 版本锚定
  --audit audit.json \                # 配置/DB/infra 变更审计（可选）
  --code-graph graph.json \           # 预构建 code graph（可选，缺省从 --repo 现场构建）
  --kb kb.json --write-back \         # 知识库查历史 + 回写（可选）
  --out report.md --json-out report.json

# 不知道上游有哪些？从 pom.xml 依赖自动推导（输出可直接粘贴的 --upstream 参数）
python3 -m rca.cli suggest-upstreams --repo /path/order-service \
  --workspace /path/repos --group-prefix com.example

# 堆栈指向上游共享库/服务？把上游仓库挂进来（可重复），
# 上游变更参与取证（relation=upstream）、图合并与版本锚定
python3 -m rca.cli run --alert alert.json --logs app.log \
  --repo /path/order-service \
  --upstream coupon-lib=/path/coupon-lib@<发布commit> ...

# kubectl --prefix 等多流交织日志：按行前缀拆流
python3 -m rca.cli run --alert alert.json --logs pods.log \
  --stream-prefix-re '^\[pod/([^\]]+)\]\s?' ...

# 日志源可选三种、可叠加：文件 / 导出命令 / Elasticsearch
python3 -m rca.cli run --alert alert.json \
  --log-cmd "kubectl logs deploy/order-service --since=2h" ... 
python3 -m rca.cli run --alert alert.json \
  --es-url http://es:9200 --es-index "app-log-*" \
  --es-service-field service.keyword --es-lookback-min 120 \
  --es-auth "user:pass" ...

# 配置中心/DDL 审计导出 → audit.json（--append 多源合并、按 id 去重）
python3 -m rca.cli audit-convert --format apollo --input releases.json \
  --out audit.json --service-map svcmap.json
python3 -m rca.cli audit-convert --format ddl --input tickets.json \
  --out audit.json --append

# project-map 索引 → code graph（--java-src 同时用内置解析器补模块内调用链）
python3 -m rca.cli graph-from-project-map \
  --symbols .claude/project-map/symbols.json \
  --calls .claude/project-map/calls.json \
  --java-src /path/to/repo --out graph.json

# 单独构建 code graph（仅内置解析器）
python3 -m rca.cli build-graph --java-src /path/to/repo --out graph.json

# 反驳验证通过后，定案回写最终根因解释
python3 -m rca.cli kb-add --kb kb.json --fingerprint <fp> \
  --incident-id INC-x --date 2026-07-11T14:25:00 --tier CONFIRMED \
  --root-cause "变更删除空券判断，空券请求路径解引用 null 导致 NPE" --change-id <sha>
```

## alert.json 示例

```json
{
  "incident_id": "INC-20260711-001",
  "service": "order-service",
  "alert_time": "2026-07-11T14:25:00",
  "deployed_commit": "c622950e…",
  "business_packages": ["com.example"]
}
```

## 目录结构

```
rca/                 核心包：schemas / log_forensics / change_sources /
                     code_graph / adjudicator / knowledge_base / report / cli
                     + 接入层：log_sources（文件/命令/ES）、
                     audit_adapters（Apollo/Nacos/DDL）、
                     project_map_adapter（symbols/calls.json → CodeGraph）
tests/               unittest 套件 + 合成故障工厂（helpers.py）
demo/run_demo.py     端到端演示：构造合成故障 → CLI 全流程 → 断言校验
skills/incident-rca/ agent 编排层 skill（并行采证→裁决→反驳验证→定案回写）
DESIGN.md            生产级方案文档
```
