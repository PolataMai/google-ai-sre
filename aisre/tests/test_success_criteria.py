"""结构化成功条件(偿还技术债):success_criteria 在契约层就是
{metric, op, threshold},非法格式在构造/解析时响亮报错,不再静默变成
Guardian 里的"永久未决→超时回滚"。

对照旧行为:evaluate_criterion("garbage", obs) 曾静默返回 None;
现在 SuccessCriterion.parse 对非法格式抛 ValueError,而 evaluate 只在
"指标缺失"这一合法情形返回 None。
"""
import unittest

from aisre.actions import SuccessCriterion


class TestParse(unittest.TestCase):
    def test_numeric_plain(self):
        c = SuccessCriterion.parse("slo_burn_rate<2")
        self.assertEqual((c.metric, c.op, c.threshold),
                         ("slo_burn_rate", "<", 2.0))

    def test_numeric_percent_resolves_to_fraction(self):
        c = SuccessCriterion.parse("error_rate<1%")
        self.assertEqual((c.metric, c.op, c.threshold),
                         ("error_rate", "<", 0.01))

    def test_all_numeric_operators(self):
        self.assertEqual(SuccessCriterion.parse("x<=5").op, "<=")
        self.assertEqual(SuccessCriterion.parse("x>=5").op, ">=")
        self.assertEqual(SuccessCriterion.parse("x>5").op, ">")

    def test_boolean_flag(self):
        c = SuccessCriterion.parse("sli_recovered_5m")
        self.assertEqual((c.metric, c.op, c.threshold),
                         ("sli_recovered_5m", "is_true", None))

    def test_malformed_raises_loudly(self):
        for bad in ("error_rate<", "error_rate<abc", "weird thing",
                    "a << b", "", "  ", "x<>1"):
            with self.assertRaises(ValueError, msg=bad):
                SuccessCriterion.parse(bad)


class TestConstructionValidation(unittest.TestCase):
    def test_numeric_op_requires_threshold(self):
        with self.assertRaises(ValueError):
            SuccessCriterion(metric="x", op="<", threshold=None)

    def test_is_true_forbids_threshold(self):
        with self.assertRaises(ValueError):
            SuccessCriterion(metric="x", op="is_true", threshold=1.0)

    def test_unknown_op_rejected(self):
        with self.assertRaises(ValueError):
            SuccessCriterion(metric="x", op="~=", threshold=1.0)


class TestEvaluate(unittest.TestCase):
    def test_numeric_true_false(self):
        c = SuccessCriterion.parse("error_rate<1%")
        self.assertIs(c.evaluate({"error_rate": 0.008}), True)
        self.assertIs(c.evaluate({"error_rate": 0.02}), False)

    def test_boolean_true_false(self):
        c = SuccessCriterion.parse("sli_recovered_5m")
        self.assertIs(c.evaluate({"sli_recovered_5m": True}), True)
        self.assertIs(c.evaluate({"sli_recovered_5m": False}), False)

    def test_missing_metric_is_none_only_legit_undecided(self):
        self.assertIsNone(SuccessCriterion.parse("error_rate<1%").evaluate({}))
        self.assertIsNone(SuccessCriterion.parse("sli_recovered_5m").evaluate({}))

    def test_bool_not_treated_as_number(self):
        # 观测里 error_rate 误给成 True 不应被当数值比较
        self.assertIsNone(SuccessCriterion.parse("error_rate<1%")
                          .evaluate({"error_rate": True}))


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        for s in ("error_rate<1%", "slo_burn_rate<2", "sli_recovered_5m"):
            c = SuccessCriterion.parse(s)
            again = SuccessCriterion.from_dict(c.to_dict())
            self.assertEqual((again.metric, again.op, again.threshold),
                             (c.metric, c.op, c.threshold))

    def test_from_dict_rejects_malformed(self):
        with self.assertRaises(ValueError):
            SuccessCriterion.from_dict({"metric": "x", "op": "<",
                                        "threshold": None})
