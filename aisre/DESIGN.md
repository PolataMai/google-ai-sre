# aisre 设计说明(第 1–2 周范围)

## 定位

12 周 AI SRE MVP 的数据契约层。上游方案见
[ai-sre/google-ai-sre-能力与实现方案.md](../ai-sre/google-ai-sre-能力与实现方案.md)。
第 1–2 周只做四件事:场景定义、事件与动作 Schema、服务目录、90 天基线——
即后续所有组件(orchestrator / actuation-gateway / eval-runner)共享的地基。

## 关键决策

### 1. 证据约束放在数据结构里,不放在流程约定里

`Enrichment.add_fact` 直接拒绝无证据或引用不存在证据的事实(`MissingEvidence` /
`UnknownEvidence`)。推测只能进 `hypotheses`(待验证假设)。这样证据覆盖率
不是"要求大家遵守的规范",而是 API 上做不出来的违规。

外部载入(`from_dict`)不做入口校验——回放、Shadow 对比要能加载历史上不合规
的数据;由 `validate_enrichment` 返回违规清单、`evidence_coverage` 暴露缺口。
入口严格、载入宽容、校验兜底,与 rca 的 `validate_verdict` 同一模式。

### 2. 动作是封闭目录,审批绑定内容哈希

`ALLOWED_ACTION_TYPES` 只有 `scale_out` / `rollback_release`。
`plan_hash()` 对全字段规范化 JSON 做 SHA-256;`Approval` 记录
`action_id + plan_hash`,参数任何变化都使原审批失效(`is_approval_valid`)。
这是执行网关(第 9–10 周)的准入前提:网关只需重算哈希即可判定审批是否仍有效,
不需要理解动作语义。

扩容边界:增量 ∈ [max(1, ceil(10%)), floor(25%)];回滚必须恢复
`original_replicas`。`now` 由调用方注入而非取系统时间——TTL 判定在测试和
历史回放中必须可复现。

### 3. 自治权限的最小粒度是四元组

`scope_key = 服务+场景+动作+环境`。目录只允许按此粒度授予,授予即 SHADOW;
不存在"给 Agent 全局 L3"的表达方式。晋级状态机(SHADOW → L2_APPROVAL →
L3_AUTO / SUSPENDED)属于 F12,本期只落枚举与默认值。

试点准入在 `register` 时硬校验:Tier-1、无状态、Kubernetes,不满足直接
`PilotEligibilityError`——MVP 范围不靠自觉。

### 4. 基线口径确定性优先

分位数用 nearest-rank 而非插值:值必然来自真实事故,审计时可指认到具体
`incident_id`。open 事故不进 MTTM 但单独计数(`open_excluded`),避免
"未缓解的事故不算数"造成基线偏乐观。变更失败率与事故分开统计,
两者关联(哪次变更引发哪次事故)是第 3–4 周变更连接器的职责。

### 5. 接入层的幂等靠确定性 id,不靠状态查询(第 3–4 周)

`incident_id = "inc-" + sha1(source|fingerprint)[:12]`:同一告警指纹在任何
实例、任何时刻得到同一 id。重复投递天然幂等(F01 的 2 秒返回不需要查库),
回放历史告警可精确复现当时的事故编号。去重键是 `(source, fingerprint)`,
活跃事故期间的重复告警合并、不重复触发丰富工作流。解析失败显式抛错
(`UnknownFormat` / `MalformedPayload`)——接入层丢告警是最不可接受的失败模式。

### 6. 采集的失败语义分三种,超时不重试

连接器只读(client 注入,无写路径),失败语义:`ok` / `failed`(异常,
含一次受控重试)/ `timeout`(标缺失,不重试——超时源再重试会吃掉整体预算,
宁可缺失不阻塞发布)。`collect_context` 线程池并行,全部源共享同一墙钟
截止点,整体耗时 ≈ 最慢单源;超时线程不等待收尾(`shutdown(wait=False)`)。
这是 90 秒丰富预算里"并行查询 40s、80s 未返回标缺失"的机制化。

### 7. 证据不可篡改分两道防线

写入口:同一事故内 `evidence_id` 不允许覆盖(`DuplicateEvidence`),追加式
write-through 落盘;审计口:每条证据存规范化 JSON 的 sha256,`verify` 重算
比对可发现落盘后被篡改的记录。证据按事故隔离成单文件,回放时按
`incident_id` 整体装载。

## 测试

94 个 unittest,TDD 逐模块 red-green:
scenarios(7) / schemas(14) / actions(19) / catalog(11) / baseline(8) /
intake(10) / connectors(9) / evidence_store(8) / e2e-intake-flow(1) / cli(7)。
CLI 测试直接调 `cli.main(argv)` 断言退出码与 JSON 输出,不起子进程;
并行/超时行为用可控的假 client(sleep/抛错)验证,不依赖真实数据源。
