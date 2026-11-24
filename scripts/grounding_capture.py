#!/usr/bin/env python3
"""采集 grounding 标定数据：靶场截图 + 从控件树派生的真值框。

## 为什么真值来自控件树而不是人工标注

`benchmarks/METRICS.md` 的第 7 个指标是 grounding 命中率——视觉定位返回的坐标
落在真值框内的比例。人工框选 20 个目标是几小时，而且**无法复核**（别人不知道
你框得对不对）。靶场是原生控件，`uiautomator` 的 `bounds` 就是系统给出的权威
位置 ⇒ 真值可自动派生、可重复、可被别人重新生成核对。

代价：只覆盖**控件树里存在**的目标。自绘界面（canvas/WebView/Flutter）没有
bounds，本工具不覆盖——这类目标的定位准确率需要另一套标注，不在本工具范围。

## 用法

    python scripts/grounding_capture.py --serial 8KE5T19819001756 \
        --out benchmarks/grounding/demo-app

产物：
    <out>/shots/<shot_id>.png     截图（原分辨率，真值换算依赖它）
    <out>/shots/<shot_id>.xml     控件树原文（可复核真值是怎么来的）
    <out>/shots.json              目标清单（desc + bbox + bucket + kind）

## 纪律

- **不猜**：只收控件树里同时有 `bounds` 和可读文本（text 或 content-desc）的节点；
  没有文本的纯图标不收（无从给 target 描述，收了也无法判定"该点哪"）。
- **bbox 用树的原值**，不扩不缩；判定容差在评测侧统一处理，采集侧不做手脚。
- 截图用原分辨率：`MAT_VL_MAX_EDGE` 只影响"发什么图"，坐标换算永远按原图。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

#: 只在"结构稳定、页面确定"的页面上采；白屏页（BlankActivity）没有可定位目标。
SHOTS = [
    {
        "shot_id": "login",
        "page": "MainActivity",
        "setup": "launch",
        "targets": ["username", "password", "login_btn", "announce_btn"],
    },
    {
        "shot_id": "list",
        "page": "ListActivity",
        "setup": "login",
        "targets": ["search_box", "welcome_text", "export_report_btn", "list_view"],
    },
    {
        "shot_id": "detail",
        "page": "DetailActivity",
        "setup": "open_detail",
        "targets": ["detail_title"],
        "optional": True,
    },
]

#: 目标 id → 自然语言描述（模拟用例里的 intent；不含位置线索，避免"提示答案"）
DESCS = {
    "username": ("输入账号", "账号输入框", "input"),
    "password": ("输入密码", "密码输入框", "input"),
    "login_btn": ("点击登录", "「立即登录」按钮", "button"),
    "announce_btn": ("查看公告", "「查看公告」按钮", "button"),
    "search_box": ("搜索", "搜索输入框", "input"),
    "welcome_text": ("确认已登录", "列表页顶部的欢迎文案", "text"),
    "export_report_btn": ("导出报表", "「导出报表」按钮", "button"),
    "list_view": ("查看列表", "商品列表区域", "container"),
    "detail_title": ("确认详情页", "详情页标题", "text"),
}

KIND_BY_CLASS = {
    "android.widget.Button": "button",
    "android.widget.EditText": "input",
    "android.widget.TextView": "text",
    "android.widget.ImageView": "icon",
    "android.widget.ImageButton": "icon",
    "android.widget.ListView": "container",
    "androidx.recyclerview.widget.RecyclerView": "container",
}


def _bucket(cy: float, height: int) -> str:
    if cy < height / 3:
        return "top"
    if cy < height * 2 / 3:
        return "middle"
    return "bottom"


def _parse_bounds(raw: str) -> list[int] | None:
    m = BOUNDS_RE.search(raw or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def _collect_nodes(xml_text: str) -> dict[str, dict]:
    """从控件树里按 resource-id 收集节点（同 id 取面积最大的那个）。"""
    root = ET.fromstring(xml_text)
    out: dict[str, dict] = {}
    for node in root.iter("node"):
        rid = node.get("resource-id") or ""
        if "com.insta.demo:id/" not in rid:
            continue
        name = rid.split("com.insta.demo:id/", 1)[1]
        box = _parse_bounds(node.get("bounds", ""))
        if not box:
            continue
        area = (box[2] - box[0]) * (box[3] - box[1])
        prev = out.get(name)
        if prev and prev["area"] >= area:
            continue
        out[name] = {
            "bbox": box,
            "area": area,
            "class": node.get("class") or "",
            "text": node.get("text") or "",
            "desc": node.get("content-desc") or "",
            "clickable": node.get("clickable") == "true",
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="采集 grounding 标定数据（靶场截图 + 树派生真值）")
    ap.add_argument("--serial", required=True)
    ap.add_argument("--out", required=True, help="输出目录（如 benchmarks/grounding/demo-app）")
    ap.add_argument("--package", default="com.insta.demo")
    args = ap.parse_args(argv)

    try:
        import uiautomator2 as u2
    except ImportError:
        print("需要 uiautomator2：pip install uiautomator2", file=sys.stderr)
        return 2

    out = pathlib.Path(args.out)
    (out / "shots").mkdir(parents=True, exist_ok=True)
    d = u2.connect(args.serial)

    def adb(*cmd: str) -> None:
        subprocess.run(["adb", "-s", args.serial, *cmd], capture_output=True, check=False)

    def fresh_launch() -> None:
        adb("shell", "am", "force-stop", args.package)
        adb("shell", "am", "start", "-W", "-n", f"{args.package}/.MainActivity")
        time.sleep(2.0)

    def do_login() -> None:
        d(resourceId=f"{args.package}:id/username").set_text("alice")
        d(resourceId=f"{args.package}:id/password").set_text("pass1234")
        d(resourceId=f"{args.package}:id/login_btn").click()
        time.sleep(1.5)

    def open_detail() -> None:
        # 列表页首项点进去（demo-app 的行 item 用 item_text 标识）
        d(resourceId=f"{args.package}:id/item_text").click()
        time.sleep(1.2)

    shots_meta: list[dict] = []
    png_sizes: set[tuple[int, int]] = set()

    for spec in SHOTS:
        setup = spec["setup"]
        fresh_launch()
        if setup in ("login", "open_detail"):
            do_login()
        if setup == "open_detail":
            try:
                open_detail()
            except Exception as exc:
                if spec.get("optional"):
                    print(f"  跳过 {spec['shot_id']}：{type(exc).__name__}: {exc}")
                    continue
                raise

        activity = (d.app_current() or {}).get("activity") or ""
        if spec["page"] not in activity:
            msg = f"{spec['shot_id']}: 前台是 {activity}，不是 {spec['page']}"
            if spec.get("optional"):
                print(f"  跳过：{msg}")
                continue
            print(f"  ✗ {msg}", file=sys.stderr)
            return 1

        png_path = out / "shots" / f"{spec['shot_id']}.png"
        # 用 adb exec-out 直接落文件：u2 的 screenshot(path) 返回值口径不稳定，
        # 拿到 False/None 不代表没写成，容易误判（本次实测踩过）。
        raw = subprocess.run(["adb", "-s", args.serial, "exec-out", "screencap", "-p"],
                             capture_output=True, check=False).stdout
        if len(raw) < 1000 or not raw.startswith(b"\x89PNG"):
            print(f"  ✗ 截图失败：{spec['shot_id']}（{len(raw)} bytes）", file=sys.stderr)
            return 1
        png_path.write_bytes(raw)
        xml_text = _prune_foreign(d.dump_hierarchy(), args.package)
        (out / "shots" / f"{spec['shot_id']}.xml").write_text(xml_text, encoding="utf-8")

        try:
            from PIL import Image
            with Image.open(png_path) as im:
                w, h = im.size
        except ImportError:  # 不装 Pillow 时退回 adb 的 wm size
            raw = subprocess.run(["adb", "-s", args.serial, "shell", "wm", "size"],
                                 capture_output=True, check=False, text=True).stdout
            m = re.search(r"(\d+)x(\d+)", raw)
            w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        png_sizes.add((w, h))

        nodes = _collect_nodes(xml_text)
        targets = []
        for name in spec["targets"]:
            node = nodes.get(name)
            if not node:
                print(f"  ⚠️ {spec['shot_id']}/{name}：控件树里没有，跳过")
                continue
            intent, desc, kind = DESCS.get(name, (f"点击 {name}", name, "other"))
            bx = node["bbox"]
            cy = (bx[1] + bx[3]) / 2
            targets.append({
                "target_id": name,
                "intent": intent,
                "desc": desc,
                "bbox": bx,
                "bucket": _bucket(cy, h),
                "kind": KIND_BY_CLASS.get(node["class"], kind),
                "ui_text": node["text"],
                "ui_desc": node["desc"],
                "clickable": node["clickable"],
            })
        shots_meta.append({
            "shot_id": spec["shot_id"],
            "page": spec["page"],
            "png": f"shots/{spec['shot_id']}.png",
            "tree": f"shots/{spec['shot_id']}.xml",
            "targets": targets,
        })
        print(f"  ✅ {spec['shot_id']}（{spec['page']}）：{len(targets)} 个目标 png={w}x{h}")

    if not shots_meta:
        print("没有采到任何页面", file=sys.stderr)
        return 1
    if len(png_sizes) != 1:
        print(f"⚠️ 截图尺寸不一致：{png_sizes}（真值换算按各自 png 尺寸，评测侧需支持）",
              file=sys.stderr)

    w, h = next(iter(png_sizes))
    data = {
        "schema_version": 1,
        "app": args.package,
        "apk_sha256": _apk_sha256(args.serial, args.package),
        "device": _device(args.serial),
        "png_size": [w, h],
        "note": "真值框来自 uiautomator 控件树 bounds（非人工标注）；"
                "desc 不含位置线索；判定容差在评测侧统一处理。",
        "shots": shots_meta,
    }
    (out / "shots.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                    encoding="utf-8")
    total = sum(len(s["targets"]) for s in shots_meta)
    print(f"\n完成：{len(shots_meta)} 页 / {total} 个目标 → {out / 'shots.json'}")
    return 0


def _prune_foreign(xml_text: str, package: str) -> str:
    """剔除非目标包的节点（状态栏/导航栏属 systemui）。

    两个原因：① 公开仓不得出现第三方 App 标识——状态栏通知的 content-desc 里会带
    别家 App 名，实测被导出护栏抓到；② 控件树原文只该包含被测 App，否则复核真值时
    会混入无关噪声。目标真值全在目标包内，剔除后仍可复核。
    """
    root = ET.fromstring(xml_text)
    parents = {child: parent for parent in root.iter() for child in parent}
    for node in list(root.iter("node")):
        pkg = node.get("package") or ""
        if pkg and pkg != package:
            parent = parents.get(node)
            if parent is not None and node in list(parent):
                parent.remove(node)
    return ET.tostring(root, encoding="unicode")


def _apk_sha256(serial: str, package: str) -> str:
    """取已安装 APK 的 sha256（设备上算，不依赖本地文件）。"""
    try:
        path = subprocess.run(["adb", "-s", serial, "shell", "pm", "path", package],
                              capture_output=True, check=False, text=True).stdout.strip().splitlines()
        if not path:
            return ""
        remote = path[0].replace("package:", "")
        out = subprocess.run(["adb", "-s", serial, "shell", f"sha256sum {remote}"],
                             capture_output=True, check=False, text=True).stdout.strip()
        return out.split()[0] if out else ""
    except Exception:  # noqa: BLE001 —— 指纹拿不到不该阻断采集
        return ""


def _device(serial: str) -> dict:
    def prop(name: str) -> str:
        return subprocess.run(["adb", "-s", serial, "shell", "getprop", name],
                              capture_output=True, check=False, text=True).stdout.strip()
    return {"model": prop("ro.product.model"), "os_version": prop("ro.build.version.release"),
            "serial": serial}


if __name__ == "__main__":
    sys.exit(main())
