"""explore 回退语义控件过滤测试：返回/关闭类控件不作探索候选（链可重放性）。"""

from __future__ import annotations

from insta_engine.explore import _candidate, _is_back_control
from insta_engine.ui_tree import parse_nodes


def _node(text="", rid="", desc="", clickable="true", cls="android.widget.Button"):
    xml = (f'<node class="{cls}" text="{text}" content-desc="{desc}" '
           f'resource-id="{rid}" clickable="{clickable}" bounds="[0,0][10,10]" '
           f'package="com.x"/>')
    return parse_nodes(xml)[0]


def test_back_labels_exact_match():
    assert _is_back_control(_node(text="返回")) is True
    assert _is_back_control(_node(text="关闭")) is True
    assert _is_back_control(_node(desc="Back")) is True  # desc 兜底也判
    # 复合词**不误伤**（判据刻意保守，见 explore._BACK_LABELS 说明）
    assert _is_back_control(_node(text="返回首页")) is False
    assert _is_back_control(_node(text="关闭广告")) is False


def test_back_res_id_word_boundary():
    assert _is_back_control(_node(text="→", rid="com.x:id/title_bar_btn_back")) is True
    assert _is_back_control(_node(rid="com.x:id/close_btn")) is True
    assert _is_back_control(_node(rid="com.x:id/btn_backdrop")) is False  # 非词边界
    assert _is_back_control(_node(text="搜索", rid="com.x:id/search_backdoor")) is False


def test_candidate_filters_back_controls():
    xml = (
        '<node class="Button" text="返回" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
        '<node class="Button" text="搜索" resource-id="com.x:id/btn_back" clickable="true" '
        'bounds="[0,10][20,20]" package="com.x"/>'
        '<node class="Button" text="发现" clickable="true" bounds="[0,20][30,30]" package="com.x"/>'
    )
    labels = [c["label"] for c in _candidate(xml, "com.x")]
    assert "返回" not in labels          # 文案回退被过滤
    assert "搜索" not in labels          # res-id 含 back 的也被过滤（rid 信号强于文案）
    assert "发现" in labels              # 正常前进候选保留
