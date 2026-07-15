"""配置中心/DB 变更审计 adapter：把各系统的导出格式归一为 audit.json 契约。

audit.json 契约（change_sources.collect_audit_changes 消费）：
  {"id", "type": "config|db|infra", "service", "timestamp", "summary",
   "author", "keys": [...], "relation"(可选)}

支持：
- from_apollo：Apollo 发布历史（openapi releases 导出）；
- from_nacos ：Nacos 配置历史（/nacos/v1/cs/history 导出）；
- from_ddl   ：DDL 工单系统的通用导出。

service_map：外部系统的应用标识 → 本方案的服务名（如 Apollo appId → service）。
"""
from __future__ import annotations

from typing import Optional


def from_apollo(releases: list[dict],
                service_map: Optional[dict[str, str]] = None) -> list[dict]:
    """Apollo 发布历史 → audit 条目。

    输入条目关键字段：id, appId, namespaceName, name, comment,
    configurations({key: value}), dataChangeCreatedBy, dataChangeCreatedTime。
    """
    service_map = service_map or {}
    out = []
    for r in releases:
        app_id = str(r.get("appId", ""))
        out.append({
            "id": f"apollo-{r.get('id', '')}",
            "type": "config",
            "service": service_map.get(app_id, app_id),
            "timestamp": r.get("dataChangeCreatedTime", ""),
            "summary": r.get("comment") or r.get("name", "")
                       or f"Apollo 发布 {r.get('namespaceName', '')}",
            "author": r.get("dataChangeCreatedBy", ""),
            "keys": sorted((r.get("configurations") or {}).keys()),
        })
    return out


def from_nacos(items: list[dict],
               service_map: Optional[dict[str, str]] = None) -> list[dict]:
    """Nacos 配置历史 → audit 条目。

    输入条目关键字段：id, dataId, group, appName, srcUser, opType,
    lastModifiedTime（或 createdTime）。
    """
    service_map = service_map or {}
    out = []
    for it in items:
        app = str(it.get("appName") or "")
        data_id = str(it.get("dataId", ""))
        # 无 appName 时用 dataId 前缀猜服务名（如 order-service-prod.yaml）
        service = service_map.get(app) or service_map.get(data_id) or app \
            or data_id.rsplit(".", 1)[0]
        out.append({
            "id": f"nacos-{it.get('id', '')}",
            "type": "config",
            "service": service,
            "timestamp": it.get("lastModifiedTime") or it.get("createdTime", ""),
            "summary": f"Nacos {it.get('opType', 'U').strip()} {data_id}"
                       f"@{it.get('group', '')}",
            "author": it.get("srcUser", ""),
            "keys": [data_id],
        })
    return out


def from_ddl(tickets: list[dict],
             service_map: Optional[dict[str, str]] = None) -> list[dict]:
    """DDL 工单 → audit 条目。

    输入条目关键字段：ticket_id, database, tables([...]), summary/sql,
    executed_at, executor, service(可选)。
    未标注 service 的 DDL 默认 relation=shared-db——库表可能被多服务共享，
    宁可进入候选被裁决器评估，不可漏。
    """
    service_map = service_map or {}
    out = []
    for t in tickets:
        service = t.get("service") or service_map.get(str(t.get("database", "")), "")
        entry = {
            "id": f"ddl-{t.get('ticket_id', '')}",
            "type": "db",
            "service": service,
            "timestamp": t.get("executed_at", ""),
            "summary": t.get("summary") or (t.get("sql", "")[:120]),
            "author": t.get("executor", ""),
            "keys": list(t.get("tables", [])) + [str(t.get("database", ""))],
        }
        if not service:
            entry["relation"] = "shared-db"
        out.append(entry)
    return out


CONVERTERS = {"apollo": from_apollo, "nacos": from_nacos, "ddl": from_ddl}
