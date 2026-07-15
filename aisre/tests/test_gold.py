"""Gold 数据流程:值班人关单时零负担回流高质量标注。

对应文章的 Golden Data Generation Workflow:
- 事故缓解后,系统从实际执行的动作预填建议(cause_code + 动作三元组);
- 值班人接受/修改/拒绝——接受与修改都产出 GoldLabel,拒绝不产出;
- GoldStore 追加式落盘,重启可恢复,按事故取最新标注。
"""
import tempfile
import unittest

from aisre.gold import GoldStore, accept, modify, reject, suggest_from_execution
from tests.test_actions import make_rollback_release

AT = "2026-07-15T11:00:00Z"


def make_suggestion():
    return suggest_from_execution(
        incident_id="inc-001",
        executed_plan=make_rollback_release(),
        top_cause="RECENT_RELEASE_REGRESSION")


class TestSuggestion(unittest.TestCase):
    def test_suggestion_prefilled_from_executed_plan(self):
        s = make_suggestion()
        self.assertEqual(s["incident_id"], "inc-001")
        self.assertEqual(s["cause_code"], "RECENT_RELEASE_REGRESSION")
        self.assertEqual(s["action"]["action_type"], "rollback_release")
        self.assertEqual(s["action"]["parameters"]["rollback_to_version"], "v41")
        self.assertEqual(s["status"], "suggested")

    def test_accept_produces_gold_label(self):
        label = accept(make_suggestion(), by="alice", at=AT)
        self.assertEqual(label.source, "accepted")
        self.assertEqual(label.cause_code, "RECENT_RELEASE_REGRESSION")
        self.assertEqual(label.action["action_type"], "rollback_release")
        self.assertEqual((label.labeled_by, label.labeled_at), ("alice", AT))

    def test_modify_overrides_fields(self):
        label = modify(make_suggestion(), by="bob", at=AT,
                       cause_code="CAPACITY_SATURATION")
        self.assertEqual(label.source, "modified")
        self.assertEqual(label.cause_code, "CAPACITY_SATURATION")
        # 未覆盖的字段保留预填值
        self.assertEqual(label.action["action_type"], "rollback_release")

    def test_modify_with_unknown_cause_rejected(self):
        with self.assertRaises(ValueError):
            modify(make_suggestion(), by="bob", at=AT, cause_code="GHOST")

    def test_reject_produces_nothing(self):
        self.assertIsNone(reject(make_suggestion()))


class TestGoldStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GoldStore(self.tmp.name)

    def test_add_count_and_persistence(self):
        self.store.add(accept(make_suggestion(), by="alice", at=AT))
        reopened = GoldStore(self.tmp.name)
        self.assertEqual(reopened.count(), 1)
        label = reopened.for_incident("inc-001")
        self.assertEqual(label.cause_code, "RECENT_RELEASE_REGRESSION")

    def test_latest_label_wins_per_incident(self):
        self.store.add(accept(make_suggestion(), by="alice", at=AT))
        self.store.add(modify(make_suggestion(), by="bob",
                              at="2026-07-15T12:00:00Z",
                              cause_code="CAPACITY_SATURATION"))
        self.assertEqual(self.store.for_incident("inc-001").cause_code,
                         "CAPACITY_SATURATION")
        self.assertEqual(self.store.count(), 2)   # 历史追加保留

    def test_unknown_incident_returns_none(self):
        self.assertIsNone(self.store.for_incident("inc-ghost"))
