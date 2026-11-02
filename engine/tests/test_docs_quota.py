"""用户可见文档里的额度数字，必须与 `policy/quota.json` 一致。

为什么单开一条（2026-11-02）：这已经是**第二次**漂。
- 10-29 `04f7cbb`「统一额度/档位口径——README 与话术还停在 2000/300」修过一次；
- 11-02 `5692faa` 把单人档从 60/20 提到 300/100，只改了官网三页与策略文件，
  README / 控制台内置手册 / docs/13 仍写「生成 60 / 视觉 20」「标准版 2,000」。

数字错了不会让任何测试变红，只会让用户按 60 次去规划、按 2000 次去承诺。
所以这里把**文档 ↔ 策略**钉在一起：策略变了、文档没跟上，测试就红。
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 用户会读到的文件（README 进公开仓；控制台手册随桌面端打包；docs/13 是使用手册底稿）
USER_FACING = ("README.md", "console/public/manual.html", "docs/13-产品使用手册.md")


def _plans() -> dict:
    data = json.loads((ROOT / "policy" / "quota.json").read_text(encoding="utf-8"))
    return data["plans"]


def test_readme_states_the_current_single_and_team_quota():
    plans = _plans()
    free, team = plans["free"], plans["team"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"生成 {free['gen']} / 视觉 {free['vision']}" in readme, (
        f"README 的单人档额度与策略不一致（策略是 生成 {free['gen']} / 视觉 {free['vision']}）")
    assert f"生成 {team['gen']:,} / 视觉 {team['vision']:,}" in readme, (
        f"README 的团队档额度与策略不一致（策略是 生成 {team['gen']} / 视觉 {team['vision']}）")


def test_console_manual_states_the_current_quota():
    plans = _plans()
    free, team = plans["free"], plans["team"]
    manual = (ROOT / "console" / "public" / "manual.html").read_text(encoding="utf-8")

    assert f"生成 {free['gen']} / 视觉 {free['vision']}" in manual, (
        "控制台内置手册的额度与策略不一致（它会随桌面端打包，用户看得到）")
    assert f"生成 {team['gen']:,} / 视觉 {team['vision']:,}" in manual


def test_no_stale_free_tier_numbers_in_user_facing_docs():
    """旧值（60 / 20、2000 / 300）不该再出现在用户可见文档里。

    这条比上面两条更"粗"，但它挡的是另一类错：**改了一处、漏了另一处**。
    上面两条只钉住"表格里那个数字"，这条钉住"任何角落都别留旧值"。
    """
    stale = ("生成 60 / 视觉 20", "体验模式 60 次/月", "视觉 20 次/月",
             "生成 2,000", "2,000 次/月")
    for rel in USER_FACING:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for bad in stale:
            assert bad not in text, (
                f"{rel} 还写着旧额度 {bad!r}——策略已改为 "
                f"生成 {_plans()['free']['gen']} / 视觉 {_plans()['free']['vision']}")
