---
name: incident-rca
description: 线上故障告警的根因定位编排——并行采证（日志+变更）→ code graph 集合运算裁决 → 行为差异解释 → 反驳验证 → 定案回写知识库。Use when 用户报告线上告警/故障需要定位根因，或运行 /incident-rca。引擎在 rca/ 项目（python3 -m rca.cli），agent 只做读诊断、写解释、做反驳，不做无证据归因。
---

# incident-rca：故障根因定位编排

你是故障指挥员。确定性计算交给 `rca` 引擎，你负责组装输入、解释 diff 行为差异、做反驳验证。**铁律：没有证据链的结论只能是 HYPOTHESIS；引擎判 HYPOTHESIS 时禁止自行归因任何变更。**

## 流程

### 第 0 步：组装告警上下文
从用户/告警系统收集并写 `alert.json`：`incident_id`、`service`、`alert_time`、`deployed_commit`（发布系统查，**不是本地 HEAD**）、`business_packages`。缺 `deployed_commit` 时明确告知用户结论上限是 LIKELY（无法版本锚定）。

### 第 1 步：并行采证
- 日志三选一或叠加：本地文件 `--logs`；导出命令 `--log-cmd "kubectl logs …"`
  （命令只能由你/用户手写，**严禁把告警或日志内容拼进命令**——注入面）；
  ES 直连 `--es-url/--es-index/--es-service-field`（自动按告警时间开窗）；
- 变更：确认代码仓库路径；配置中心/DDL 导出用 `rca audit-convert --format
  apollo|nacos|ddl [--append]` 归一合并为 audit.json，跨系统应用名对不上时给
  `--service-map`；DDL 未标注服务会自动 `relation=shared-db`；
- code graph：项目跑过 /project-map 的，直接
  `rca graph-from-project-map --symbols … --calls … --java-src <repo>` 得到
  跨模块+模块内合并图传 `--code-graph`（须锚定发布 commit）。

### 第 2 步：跑引擎
```bash
python3 -m rca.cli run --alert alert.json --logs app.log \
  --repo <repo> [--audit audit.json] [--code-graph graph.json] \
  --kb kb.json --out report.md --json-out report.json
```
先把报告"一、止血建议"完整转告用户——**止血优先于根因细节**。

### 第 3 步：补齐行为差异解释（每个 CONFIRMED）
读证据链里的 diff（`git show <change_id>`）与 code_anchor 源码行，写出三段式解释：
1. 变更前行为（旧代码对故障输入如何处理）；
2. 变更后行为（新代码为何在该输入下抛出该异常）；
3. 与日志对账（异常类型、报错行号、首次出现时间 ≈ 发布时间）。
写不出第 2 段 = 解释不成立，降级处理，回到 ranked_candidates 看下一个。

### 第 4 步：反驳验证（adversarial）
换立场尝试推翻自己的结论，逐条检查：
- 时间线：错误首现是否明显晚于发布（间隔数小时须解释：低频路径？定时触发？）；
- 反例：同指纹错误在变更之前是否也出现过（查更早日志/知识库）；
- 语义：diff 是否其实无害（纯格式/注释/等价重构）；
- 可复现：条件允许时用 mock-harness 重放故障输入，在变更前后两个版本对比行为。
反驳成立 → 结论降级为 LIKELY 并说明推翻理由，继续查下一候选。

### 第 5 步：定案回写
反驳不成立才回写（HYPOTHESIS 永不回写）：
```bash
python3 -m rca.cli kb-add --kb kb.json --fingerprint <fp> \
  --incident-id <id> --date <alert_time> --tier CONFIRMED \
  --root-cause "<第 3 步的三段式解释压缩为一句>" --change-id <sha> \
  --notes "反驳验证通过：<简述>"
```

## HYPOTHESIS 的处理
引擎给出 HYPOTHESIS 时，按报告里的 next_actions 逐项帮用户排查（流量、上游、数据、定时任务、资源耗尽、未纳管变更源），把每项的排查结果记录下来。**不要因为"总得给个答案"而升级结论**——"当前证据不支持归因，已排除 X/Y，建议查 Z"就是正确答案。

## 跨服务回查
堆栈顶层业务帧指向上游服务/共享库代码（feign/dubbo 客户端、SDK jar 内的类）时，
把上游仓库直接挂进同一次运行。不知道上游有哪些先推导：
```bash
python3 -m rca.cli suggest-upstreams --repo <repo> --workspace <repos父目录> \
  --group-prefix com.yourco    # 输出可直接粘贴的 --upstream 参数
python3 -m rca.cli run ... --upstream coupon-lib=/path/to/coupon-lib@<发布commit>
```
上游变更自动参与取证（relation=upstream）、图合并与锚定，可直接 CONFIRMED 归因
上游提交。只有当上游是**独立服务且其错误在自己的日志里**（本服务只看到 5xx/超时）
时，才需要对上游服务的日志+仓库另跑一轮，本服务结论标注"疑似上游传导"。

## 多流交织日志
`kubectl logs --prefix`、docker compose 等多 pod 混流输出，加
`--stream-prefix-re '^\[pod/([^\]]+)\]\s?'` 拆流解析；若前缀只在事件首行
（堆栈行无标记），文本层不可恢复，改走 `--es-*`（ES 文档自含完整堆栈）。
