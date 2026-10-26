"""启动噪声清理的**成本**与**判据**（2026-10-25 提速）。

## 病灶（真机实测）

`_skip_splash_ad` 每次 launch 后都跑，实现是「10 个候选文案各 `find` 一次」
（6 个权限否定向 × 0.8s + 4 个跳过 × 1.0s）。`find` **未命中要轮询到超时**，
于是"这一屏根本没有广告"——最常见的情况——反而最慢：

    实测 14.0–14.4s / 次 ⇒ 10 条用例白扔 140s；日志里一个字都没有（全是超时，不是错）

## 改法

一次 `dump`（~0.3s）拿整棵树，在本地按**同一优先级顺序**挑控件（`_pick_label`），
命中才点（坐标取节点边界中心，与 u2 `node.click()` 等价）。未命中是纯内存判断。

## 这份测试守的几条线

1. **成本**：无广告的业务页只花 1 次 dump、0 次 find、0 次点击；
2. **判据不漂移**：优先级「跳过 > 跳过广告 > 关闭 > 以后再说」不变；
   权限否定向仍是**精确**匹配（`确定取消` 绝不能被当成 `取消` 点掉）；
3. **晚到广告仍能接住**：只靠两个信号决定要不要多等（广告线索词 / 标签稀疏），
   正常业务页不额外等；重试上限 3 轮；
4. **兜底不丢功能**：dump 拿不到树、bounds 退化时退回逐条 find；
   iOS（WDA 树不是 `<node>`）保持原路径，一次 dump 都不多做。
"""

from __future__ import annotations

from insta_engine import runner as runner_mod
from insta_engine.runner import (
    Runner,
    _parse_nodes_safe,
    _pick_label,
    _splash_likely,
)
from insta_engine.ui_tree import parse_nodes, rect

PKG = "com.demo"


# -- 假驱动 -------------------------------------------------------------------

def _n(text: str, bounds: str) -> str:
    return f'<node text="{text}" bounds="{bounds}" package="{PKG}"/>'


def _screen(*texts: str) -> str:
    """一屏：每个文案一块互不重叠的区域，便于反查"点到了哪个文案"。"""
    body = "".join(_n(t, f"[0,{i * 100}][100,{i * 100 + 80}]") for i, t in enumerate(texts))
    return f"<hierarchy>{body}</hierarchy>"


_BUSY = ("推荐", "视频", "直播", "热点", "财经", "科技", "体育", "娱乐", "汽车", "房产")


def _center_of(xml: str, text: str) -> tuple[int, int] | None:
    for node in parse_nodes(xml):
        if node.text == text:
            box = rect(node)
            if box:
                return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    return None


class _Driver:
    """最小驱动面：只够跑 `_skip_splash_ad`。

    `screens` 是 dump 的**依次返回**列表（用完重复最后一屏），`Exception` 实例表示
    该次 dump 抛错 —— 用来验"拿不到树就退回 find"。
    """

    platform = "android"
    serial = "SERIAL-1"

    def __init__(self, *screens: str | Exception, exists_for: tuple[str, ...] = ()):
        self._screens = list(screens) or [_screen(*_BUSY)]
        self._exists_for = set(exists_for)
        self._last_xml = ""
        self.dumps = 0
        self.clicks: list[tuple[int, int]] = []
        self.clicked_texts: list[str] = []
        self.finds: list[tuple[str, str, float]] = []
        self.find_clicks: list[str] = []

    def dump(self) -> str:
        idx = min(self.dumps, len(self._screens) - 1)
        self.dumps += 1
        screen = self._screens[idx]
        if isinstance(screen, Exception):
            raise screen
        self._last_xml = screen
        return screen

    def foreground_app(self) -> dict:
        return {"package": PKG}          # 不在 permissioncontroller 名单里 ⇒ 不 force-stop

    def click(self, x: int, y: int) -> None:
        label = ""
        for node in parse_nodes(self._last_xml):
            box = rect(node)
            if box and ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2) == (x, y):
                label = node.text
                break
        self.clicks.append((x, y))
        self.clicked_texts.append(label)

    def find(self, by: str, value: str, timeout: float = 0):
        self.finds.append((by, value, timeout))
        drv = self

        class _Node:
            exists = value in drv._exists_for

            def click(self) -> None:
                drv.find_clicks.append(value)

        return _Node()

    def press(self, key: str) -> None:
        pass

    def shell(self, cmd: str) -> str:
        return ""


class _IosDriver(_Driver):
    """有 `dismiss_system_alert` ⇒ 走 iOS 分支（WDA 树不是 `<node>`，不能用本地匹配）。"""

    platform = "ios"

    def dismiss_system_alert(self) -> str | None:
        return None


# -- 成本 ---------------------------------------------------------------------

def test_no_ad_busy_page_costs_one_dump_and_nothing_else(tmp_path):
    """最常见的场景（业务页、没广告）：1 次 dump，0 次 find，0 次点击。"""
    drv = _Driver(_screen(*_BUSY))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.dumps == 1, f"只该 dump 一次，实际 {drv.dumps}"
    assert drv.finds == [], "有了 dump 就不该再逐条 find（每次未命中都要吃满超时）"
    assert drv.clicks == []


def test_noise_cleanup_never_raises_when_device_is_unreachable(tmp_path):
    """dump 抛错 + find 全部未命中 ⇒ 静默返回，不把异常抛给用例。"""
    drv = _Driver(RuntimeError("device offline"))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)
    assert drv.finds, "dump 失败必须退回 find 链，不能直接放弃"


# -- 判据不漂移 ---------------------------------------------------------------

def test_skip_preferred_over_close(tmp_path):
    """优先级：跳过 > 关闭（反过来会把信息流广告的关闭叉当跳过按钮）。"""
    drv = _Driver(_screen("关闭", "跳过 5", *_BUSY))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)
    assert drv.clicked_texts == ["跳过 5"]


def test_close_used_when_no_skip_button(tmp_path):
    drv = _Driver(_screen("关闭", *_BUSY))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)
    assert drv.clicked_texts == ["关闭"]


def test_skip_clicked_at_node_center(tmp_path):
    """点击落点 = 该节点的边界中心（与 u2 `node.click()` 等价）。"""
    xml = _screen("互动广告", "跳过 5", "淘宝")
    drv = _Driver(xml)
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.clicks == [_center_of(xml, "跳过 5")]


def test_perm_deny_button_clicked_by_exact_text(tmp_path):
    """权限弹窗的否定向：精确命中才点（弹窗关掉后重新取树再找广告）。"""
    perm = _screen("要允许访问相册吗？", "不允许", "允许")
    drv = _Driver(perm, _screen(*_BUSY))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.clicked_texts == ["不允许"]
    assert drv.dumps == 2, "点掉弹窗后页面已变，必须重新取树"


def test_affirmative_button_is_not_matched_by_contains(tmp_path):
    """护栏：「确定取消」里含「取消」——**绝不能被当作取消点掉**（会真取消业务操作）。

    旧实现用 `text` 精确匹配正是为了这个；dump 路径必须给出同样的结论。
    """
    drv = _Driver(_screen("确定取消", *_BUSY))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.clicks == [], f"点到了 {drv.clicked_texts}"


# -- 晚到广告 -----------------------------------------------------------------

def test_late_ad_is_caught_when_screen_is_sparse(tmp_path):
    """开屏还在渲染（标签 <8）⇒ 多看一眼，第二次 dump 里出现「跳过」就点掉。"""
    drv = _Driver(_screen("加载中"), _screen("互动广告", "跳过 5"))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.clicked_texts == ["跳过 5"]
    assert drv.dumps == 2


def test_late_ad_is_caught_when_ad_hint_present(tmp_path):
    """业务页但出现「互动广告」⇒ 也算广告线索，继续等（即便标签不稀疏）。"""
    drv = _Driver(_screen(*_BUSY, "互动广告"), _screen("互动广告", "跳过 5"))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.clicked_texts == ["跳过 5"]
    assert drv.dumps == 2


def test_retry_is_capped(tmp_path):
    """一直稀疏且始终没广告 ⇒ 最多 3 轮，不会无限等。"""
    drv = _Driver(_screen("加载中"))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.dumps == runner_mod._SPLASH_RETRY_ATTEMPTS
    assert drv.clicks == []


def test_worst_case_wait_budget_is_under_four_seconds(tmp_path, monkeypatch):
    """最坏情况的**固定等待**必须留在 4s 以内（改动前是 14.0–14.4s）。"""
    waits: list[float] = []
    monkeypatch.setattr(runner_mod, "_pause", waits.append)

    drv = _Driver(_screen("加载中"))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert sum(waits) < 4.0, f"固定等待 {sum(waits):.2f}s 超标"


# -- 兜底 ---------------------------------------------------------------------

def test_degenerate_bounds_falls_back_to_element_click(tmp_path):
    """bounds 退化（[0,0][0,0]，点下去是状态栏）⇒ 按完整文案精确查找再点。"""
    xml = _screen("互动广告", "跳过 5").replace("[0,100][100,180]", "[0,0][0,0]")
    drv = _Driver(xml, exists_for=("跳过 5",))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.clicks == [], "退化 bounds 不许按坐标点"
    assert ("text", "跳过 5", 0.5) in drv.finds
    assert drv.find_clicks == ["跳过 5"]


def test_ios_branch_keeps_find_path(tmp_path):
    """iOS：WDA 的 dump 是 `XCUIElementType*` 树 ⇒ 本地匹配用不了，保持 find 链。"""
    drv = _IosDriver(_screen(*_BUSY))
    Runner(drv, tmp_path)._skip_splash_ad(PKG)

    assert drv.dumps == 0, "iOS 分支不该为此多做 dump"
    assert ("textContains", "跳过", 1.0) in drv.finds


# -- 纯函数（无设备） ---------------------------------------------------------

def test_pick_label_follows_priority_order():
    nodes = _parse_nodes_safe(_screen("关闭", "跳过"))
    picked = _pick_label(nodes, ("跳过", "关闭"), contains=True)
    assert picked is not None and picked.text == "跳过"


def test_pick_label_contains_is_off_for_exact_mode():
    nodes = _parse_nodes_safe(_screen("确定取消"))
    assert _pick_label(nodes, ("取消",), contains=False) is None
    assert _pick_label(nodes, ("取消",), contains=True) is not None


def test_splash_likely_false_on_empty_tree():
    """0 个标签不算"稀疏"：多半是 dump 拿到空白页，再等也是白等。"""
    assert _splash_likely([]) is False
    assert _splash_likely(_parse_nodes_safe(_screen("加载中"))) is True
    assert _splash_likely(_parse_nodes_safe(_screen(*_BUSY))) is False


def test_parse_nodes_safe_survives_attribute_less_node():
    """`parse_nodes` 对无属性 `<node>` 会抛 AttributeError —— 噪声清理不能因此中断。"""
    assert _parse_nodes_safe("<hierarchy><node></node></hierarchy>") == []
