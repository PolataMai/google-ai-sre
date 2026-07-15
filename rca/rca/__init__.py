"""rca — 线上故障根因定位引擎。

三条线：日志取证(log_forensics) + 变更取证(change_sources) 并行，
汇合到裁决器(adjudicator) 基于 code graph 做集合运算式根因定位，
结论回写知识库(knowledge_base)，报告分离"止血建议"与"根因分析"。
"""

__version__ = "0.1.0"
