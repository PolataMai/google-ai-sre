"""测试与 demo 共用的合成故障工厂：假 Java 服务仓库 + 故障日志 + 变更审计。

时间线（全 UTC）：
- 07-01 10:00  baseline 提交（好代码，含 null 判断）——在 72h 窗口之外
- 07-11 10:00  bad 提交（删掉 null 判断，引入 NPE）——即发布版本
- 07-11 14:23  NPE 首次出现（告警）
- 07-11 14:24  支付超时错误（与任何变更无交集 → 应判 HYPOTHESIS）
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

BASELINE_TIME = "2026-07-01T10:00:00+00:00"
BAD_TIME = "2026-07-11T10:00:00+00:00"
NPE_FIRST_SEEN = "2026-07-11 14:23:05.123"
ALERT_TIME = "2026-07-11T14:25:00"

SRC = "src/main/java/com/example/order"

COUPON = """package com.example.order.model;

import java.math.BigDecimal;

public class Coupon {

    private BigDecimal discount;

    public BigDecimal getDiscount() {
        return discount;
    }
}
"""

PRICING_BASELINE = """package com.example.order.service;

import com.example.order.model.Coupon;
import java.math.BigDecimal;

public class PricingService {

    public BigDecimal applyCoupon(BigDecimal amount, Coupon coupon) {
        if (coupon == null) {
            return amount;
        }
        return amount.subtract(coupon.getDiscount());
    }
}
"""

PRICING_BAD = """package com.example.order.service;

import com.example.order.model.Coupon;
import java.math.BigDecimal;

public class PricingService {

    public BigDecimal applyCoupon(BigDecimal amount, Coupon coupon) {
        BigDecimal discount = coupon.getDiscount();
        return amount.subtract(discount);
    }
}
"""

ORDER_SERVICE = """package com.example.order.service;

import com.example.order.model.Coupon;
import java.math.BigDecimal;

public class OrderService {

    private final PricingService pricingService = new PricingService();

    public void createOrder(String orderId, BigDecimal amount, Coupon coupon) {
        BigDecimal payable = pricingService.applyCoupon(amount, coupon);
        persist(orderId, payable);
    }

    private void persist(String orderId, BigDecimal payable) {
        // write to db
    }
}
"""

ORDER_CONTROLLER = """package com.example.order.controller;

import com.example.order.model.Coupon;
import com.example.order.service.OrderService;
import java.math.BigDecimal;

public class OrderController {

    private final OrderService orderService = new OrderService();

    public String create(String orderId, BigDecimal amount, Coupon coupon) {
        orderService.createOrder(orderId, amount, coupon);
        return "ok";
    }
}
"""

PAYMENT_CLIENT = """package com.example.order.gateway;

public class PaymentClient {

    public String charge(String orderId) {
        return "paid:" + orderId;
    }
}
"""


def line_of(content: str, needle: str) -> int:
    for i, line in enumerate(content.splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"fixture 里找不到 {needle!r}")


def _git(repo: str, *args: str, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    out = subprocess.run(["git", "-C", repo, *args],
                         capture_output=True, text=True, check=True, env=env)
    return out.stdout.strip()


def commit_all(repo: str, message: str, when: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, env_extra={
        "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
    return _git(repo, "rev-parse", "HEAD")


def make_service_repo(root: str) -> dict:
    """构建 order-service 仓库：baseline（窗口外）→ bad 提交（发布版本）。"""
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "dev")

    files = {
        f"{SRC}/model/Coupon.java": COUPON,
        f"{SRC}/service/PricingService.java": PRICING_BASELINE,
        f"{SRC}/service/OrderService.java": ORDER_SERVICE,
        f"{SRC}/controller/OrderController.java": ORDER_CONTROLLER,
        f"{SRC}/gateway/PaymentClient.java": PAYMENT_CLIENT,
    }
    for rel, content in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    baseline_sha = commit_all(root, "feat: 初始版本（券折扣含空券保护）", BASELINE_TIME)

    (Path(root) / f"{SRC}/service/PricingService.java").write_text(
        PRICING_BAD, encoding="utf-8")
    bad_sha = commit_all(root, "refactor: 简化券折扣计算路径", BAD_TIME)

    return {
        "repo": root,
        "baseline_sha": baseline_sha,
        "bad_sha": bad_sha,
        "npe_line": line_of(PRICING_BAD, "coupon.getDiscount()"),
        "os_call_line": line_of(ORDER_SERVICE, "pricingService.applyCoupon"),
        "ctrl_call_line": line_of(ORDER_CONTROLLER, "orderService.createOrder"),
        "charge_line": line_of(PAYMENT_CLIENT, "public String charge"),
    }


def make_incident_log(info: dict) -> str:
    npe, osl, ctl, chg = (info["npe_line"], info["os_call_line"],
                          info["ctrl_call_line"], info["charge_line"])
    npe_stack = (
        'java.lang.NullPointerException: Cannot invoke "Coupon.getDiscount()" because "coupon" is null\n'
        f"\tat com.example.order.service.PricingService.applyCoupon(PricingService.java:{npe})\n"
        f"\tat com.example.order.service.OrderService.createOrder(OrderService.java:{osl})\n"
        f"\tat com.example.order.controller.OrderController.create(OrderController.java:{ctl})\n"
        "\tat org.springframework.web.method.support.InvocableHandlerMethod.doInvoke(InvocableHandlerMethod.java:205)\n"
        "\tat java.base/java.lang.Thread.run(Thread.java:833)")
    timeout_stack = (
        "java.net.SocketTimeoutException: Read timed out\n"
        "\tat java.base/java.net.SocketInputStream.read(SocketInputStream.java:100)\n"
        f"\tat com.example.order.gateway.PaymentClient.charge(PaymentClient.java:{chg})\n"
        "\tat java.base/java.lang.Thread.run(Thread.java:833)")
    return "\n".join([
        "2026-07-11 14:20:01.000 INFO  [order-service] c.e.o.c.OrderController : create order o-1001",
        f"{NPE_FIRST_SEEN} ERROR [order-service] c.e.o.w.GlobalExceptionHandler : create order failed",
        npe_stack,
        "2026-07-11 14:23:40.552 INFO  [order-service] c.e.o.c.OrderController : create order o-1002",
        "2026-07-11 14:23:41.000 ERROR [order-service] c.e.o.w.GlobalExceptionHandler : create order failed",
        npe_stack,
        "2026-07-11 14:24:11.001 ERROR [order-service] c.e.o.w.GlobalExceptionHandler : pay failed",
        timeout_stack,
        "2026-07-11 14:24:30.000 ERROR [order-service] c.e.o.w.GlobalExceptionHandler : create order failed",
        npe_stack,
        "",
    ])


AUDIT_ENTRIES = [
    {   # 无关服务的配置变更：不得被归因
        "id": "CFG-8801", "type": "config", "service": "inventory-service",
        "timestamp": "2026-07-11T09:00:00+00:00",
        "summary": "inventory-service 调整缓存 TTL", "author": "ops",
        "keys": ["cache.ttl"],
    },
    {   # 同服务但发生在错误首现之后：必须被窗口规则排除
        "id": "CFG-8802", "type": "config", "service": "order-service",
        "timestamp": "2026-07-11T15:00:00+00:00",
        "summary": "order-service 应急调大超时", "author": "ops",
        "keys": ["timeout.ms"],
    },
]
