"""Agent 身份(F08 前提):独立、短时、机器可区分的主体令牌。

对应文章的 No Ambient Access & Least Privilege / Robust Agent Identity:
- Agent 主体与人类主体类型可区分(human-only 操作永远不能由 agent 执行);
- 令牌短时有效(TTL),签名防篡改,错误密钥验签失败;
- 验签失败/过期一律显式拒绝(fail closed 的身份环节)。
"""
import unittest

from aisre.identity import (IdentityAuthority, InvalidToken, Principal)

NOW = "2026-07-15T10:12:00Z"
LATER = "2026-07-15T10:30:00Z"


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.authority = IdentityAuthority(secret="unit-test-secret")

    def test_issue_and_verify_agent_token(self):
        token = self.authority.issue("ai-sre-orchestrator", "agent",
                                     issued_at=NOW, ttl_seconds=600)
        p = self.authority.verify(token, now=NOW)
        self.assertEqual(p, Principal(principal_id="ai-sre-orchestrator",
                                      principal_type="agent"))

    def test_human_principal_distinguishable(self):
        token = self.authority.issue("alice", "human",
                                     issued_at=NOW, ttl_seconds=600)
        self.assertEqual(self.authority.verify(token, now=NOW).principal_type,
                         "human")

    def test_expired_token_rejected(self):
        token = self.authority.issue("ai-sre-orchestrator", "agent",
                                     issued_at=NOW, ttl_seconds=600)
        with self.assertRaises(InvalidToken):
            self.authority.verify(token, now=LATER)   # 18 分钟后,TTL 10 分钟

    def test_tampered_token_rejected(self):
        token = self.authority.issue("ai-sre-orchestrator", "agent",
                                     issued_at=NOW, ttl_seconds=600)
        forged = token.replace("ai-sre-orchestrator", "root-agent")
        with self.assertRaises(InvalidToken):
            self.authority.verify(forged, now=NOW)

    def test_wrong_secret_rejected(self):
        token = self.authority.issue("ai-sre-orchestrator", "agent",
                                     issued_at=NOW, ttl_seconds=600)
        other = IdentityAuthority(secret="different-secret")
        with self.assertRaises(InvalidToken):
            other.verify(token, now=NOW)

    def test_unknown_principal_type_rejected_at_issue(self):
        with self.assertRaises(ValueError):
            self.authority.issue("x", "service-account",
                                 issued_at=NOW, ttl_seconds=600)
