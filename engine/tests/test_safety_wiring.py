"""safety.py 的接线守卫：页面爬取与自主探索必须共用同一份黑名单对象。

`safety.py` 的模块说明写着「接线由 test_safety_wiring 守卫」，但这个文件此前
不存在（2026-11-23 外部评审 P1-1）。没有它，两处可以各自 import 之后再重新赋值，
口径会再次漂移——历史上已经漂移过一次（9 词 vs 15 词），导致 `mat index` 会点
「退出登录 / 发布 / 评价」，把被测 App 登出或产生真实副作用。

守两件事：
1. **同一对象**：`explore._SENSITIVE` / `page_index._SENSITIVE` / `safety.SENSITIVE_WORDS`
   三者是同一个 tuple（不是内容相等的两份拷贝）。
2. **关键不可逆动作在表里**：删词同样危险，用固定清单兜住。
"""

from __future__ import annotations


def test_page_index_and_explore_share_the_same_blacklist_object():
    from insta_engine import explore, page_index, safety

    assert explore._SENSITIVE is safety.SENSITIVE_WORDS
    assert page_index._SENSITIVE is safety.SENSITIVE_WORDS
    assert explore._SENSITIVE is page_index._SENSITIVE


def test_sensitive_words_cover_irreversible_actions():
    """这些动作一旦被探测点到，后果不可逆（登出、真实支付、发布内容）。"""
    from insta_engine import safety

    for word in ("退出登录", "支付", "付款", "结算", "确认订单", "提交订单",
                 "充值", "转账", "发布", "评价"):
        assert word in safety.SENSITIVE_WORDS, f"敏感词表缺少不可逆动作：{word}"


def test_back_and_splash_words_stay_disjoint_from_sensitive():
    """回退/瞬态词表不能被误并进敏感词表——那会让探测放弃正常的返回导航。"""
    from insta_engine import safety

    for word in ("返回", "关闭", "取消"):
        assert word not in safety.SENSITIVE_WORDS
