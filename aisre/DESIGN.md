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

## 测试

64 个 unittest,TDD 逐模块 red-green:
scenarios(7) / schemas(14) / actions(19) / catalog(11) / baseline(8) / cli(5)。
CLI 测试直接调 `cli.main(argv)` 断言退出码与 JSON 输出,不起子进程。
