"""共用夹具：mall-dill 形态的 services.json、变更事件工厂、指标序列生成器。

日期事实（测试断言依赖）：2026-07-10 = 周五，2026-07-11 = 周六，2026-07-14 = 周二。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from changeflow.schemas import ChangeEvent, Source, Status

# mall-dill services.json 的最小子集（结构同真实产物）
SERVICES_FIXTURE = {"services": {
    "mall-dill-order": {
        "feign_calls": ["ProductApi -> mall-dill-product-service",
                        "MarketingApi -> mall-dill-marketing-service"],
        "topics_produced": ["order.paid"], "topics_consumed": [],
    },
    "mall-dill-points": {
        "feign_calls": [], "topics_produced": [],
        "topics_consumed": ["order.paid"],
    },
    "mall-dill-cart": {
        "feign_calls": ["ProductApi -> mall-dill-product-service"],
        "topics_produced": [], "topics_consumed": [],
    },
    "mall-dill-product": {"feign_calls": [], "topics_produced": [],
                          "topics_consumed": []},
}}

TUESDAY_NOON = "2026-07-14T12:00:00"       # 无 timing 风险的基准时刻
FRIDAY_EVENING = "2026-07-10T17:30:00"     # 周五晚窗口


def write_services_json(dirpath: str) -> str:
    p = Path(dirpath) / "services.json"
    p.write_text(json.dumps(SERVICES_FIXTURE, ensure_ascii=False), encoding="utf-8")
    return str(p)


def make_event(change_id="chg-1", source=Source.CODE, service="product",
               timestamp=TUESDAY_NOON, summary="示例变更", gray=True,
               rollback_plan="回滚上一镜像", status=Status.DONE,
               **details_kw) -> ChangeEvent:
    details = details_kw.pop("details", None)
    if details is None:
        details = {"files": [f"src/F{i}.java" for i in range(3)]} \
            if source == Source.CODE else {}
    return ChangeEvent(change_id=change_id, source=source, service=service,
                       timestamp=timestamp, summary=summary, gray=gray,
                       rollback_plan=rollback_plan, status=status,
                       details=details, **details_kw)


def metric_series(anchor: str, minutes_before: int = 30, minutes_after: int = 30,
                  base_value: float = 0.5, post_value: float = 0.5,
                  wobble: float = 0.05) -> list[dict]:
    """以 anchor 为界的分钟级序列：前段 base±wobble，后段 post±wobble（确定性锯齿）。"""
    t0 = datetime.fromisoformat(anchor)
    out = []
    for i in range(minutes_before, 0, -1):
        v = base_value + (wobble if i % 2 else -wobble)
        out.append({"ts": (t0 - timedelta(minutes=i)).isoformat(), "value": v})
    for i in range(1, minutes_after + 1):
        v = post_value + (wobble if i % 2 else -wobble)
        out.append({"ts": (t0 + timedelta(minutes=i)).isoformat(), "value": v})
    return out
