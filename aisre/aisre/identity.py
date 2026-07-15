"""Agent 身份:独立、短时、机器可区分的主体令牌(F08 前提)。

对应文章的 No Ambient Access & Least Privilege:Agent 不得使用人类的
常驻凭证——每个主体由 IdentityAuthority 签发短时令牌,principal_type
(agent/human)写进签名负载,人机永远可区分;审批等 human-only 操作在
网关侧按类型强制。

令牌格式:principal_id|principal_type|expires_at|HMAC-SHA256(前三段)。
验签失败、过期、格式错误一律 InvalidToken(fail closed)。
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

PRINCIPAL_TYPES = ("agent", "human")


class InvalidToken(ValueError):
    """验签失败/过期/格式错误——身份环节一律显式拒绝。"""


@dataclass(frozen=True)
class Principal:
    principal_id: str
    principal_type: str   # agent / human


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class IdentityAuthority:
    def __init__(self, secret: str):
        self._secret = secret.encode()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(),
                        hashlib.sha256).hexdigest()

    def issue(self, principal_id: str, principal_type: str,
              issued_at: str, ttl_seconds: int) -> str:
        if principal_type not in PRINCIPAL_TYPES:
            raise ValueError(
                f"未知主体类型 {principal_type},只允许 {PRINCIPAL_TYPES}")
        expires = (_parse_ts(issued_at)
                   + timedelta(seconds=ttl_seconds)).isoformat()
        payload = f"{principal_id}|{principal_type}|{expires}"
        return f"{payload}|{self._sign(payload)}"

    def verify(self, token: str, now: str) -> Principal:
        parts = token.split("|")
        if len(parts) != 4:
            raise InvalidToken("令牌格式错误")
        principal_id, principal_type, expires, signature = parts
        payload = f"{principal_id}|{principal_type}|{expires}"
        if not hmac.compare_digest(self._sign(payload), signature):
            raise InvalidToken("签名不匹配")
        if _parse_ts(now) >= _parse_ts(expires):
            raise InvalidToken(f"令牌已过期: {expires}")
        return Principal(principal_id=principal_id,
                         principal_type=principal_type)
