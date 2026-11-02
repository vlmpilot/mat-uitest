"""读 APK 清单里的包名（纯标准库，不依赖 aapt / 设备）。

**为什么需要**：`mat verify --apk` 要在**装前**回答"这个 APK 是不是你说的那个包"。
只靠装后 `pm path <期望包>` 判断，在「APK 的实际包名与期望不同、且该包本来已经装着」
时必然失灵——覆盖安装不会新增包名，前后 diff 看不见任何变化，只能报「未知」
（2026-10-31 真机实测：`--package com.wrong.pkg` 配 `com.insta.demo` 的 APK ⇒
错误信息是"装到的是 未知"，用户无法定位自己写错了哪一侧）。

顺带把行为校正回文档的写法：v3 协议写的是「包名不一致 ⇒ **不安装**」，
此前的实现是「装后验不过即卸载」。装前能判就不用碰设备。

解析对象是编译后的二进制 XML（AXML，`AndroidManifest.xml` 在 APK 里是这个格式），
结构见 Android `ResourceTypes.h`：文件头 → 字符串池 → 资源映射 → 命名空间/元素。
这里只取**根元素**的 `package` 属性，不做通用 XML 解析。
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

_RES_XML_TYPE = 0x0003
_RES_STRING_POOL = 0x0001
_RES_XML_START_ELEMENT = 0x0102
_UTF8_FLAG = 0x100
_TYPE_STRING = 0x03
_NO_ENTRY = 0xFFFFFFFF


def _len8(buf: bytes, pos: int) -> tuple[int, int]:
    """AXML 的 UTF-8 变长长度：高位置 1 表示两字节。"""
    first = buf[pos]
    if first & 0x80:
        return ((first & 0x7F) << 8) | buf[pos + 1], pos + 2
    return first, pos + 1


def _string_pool(buf: bytes, off: int) -> list[str]:
    """解析 `ResStringPool_header` 指向的字符串池。"""
    _typ, hsize, _size = struct.unpack_from("<HHI", buf, off)
    count, _style_count, flags, strings_start, _styles = struct.unpack_from(
        "<IIIII", buf, off + 8)
    utf8 = bool(flags & _UTF8_FLAG)
    offsets = struct.unpack_from(f"<{count}I", buf, off + hsize)
    base = off + strings_start
    out: list[str] = []
    for rel in offsets:
        pos = base + rel
        if utf8:
            # 先是 utf16 字符数，再是 utf8 字节数——取第二个
            _n16, pos = _len8(buf, pos)
            n8, pos = _len8(buf, pos)
            out.append(buf[pos:pos + n8].decode("utf-8", "replace"))
        else:
            n16 = struct.unpack_from("<H", buf, pos)[0]
            out.append(buf[pos + 2:pos + 2 + n16 * 2].decode("utf-16-le", "replace"))
    return out


def read_package_name(apk: Path) -> str | None:
    """返回 APK 的包名；解析不了（非 AXML / 格式异常 / 无 package 属性）返回 None。

    **任何解析异常都必须收敛成 None**（2026-11-02 复核）：AXML 是二进制格式，
    截断/畸形文件会让 `struct.unpack_from` 抛 `struct.error`（实测），而调用方只把
    `ValueError` 当「参数不合法」——`struct.error` 会冒到 verify 顶层变成 traceback，
    破坏「所有失败都给 JSON 结论」的协议。这里整体兜底，交给调用方走装后校验。
    """
    try:
        return _parse_package_name(Path(apk))
    except (OSError, KeyError, ValueError, IndexError, struct.error, zipfile.BadZipFile):
        return None


def _parse_package_name(apk: Path) -> str | None:
    """解析本体（异常由 `read_package_name` 统一收敛）。"""
    with zipfile.ZipFile(apk) as zf:
        data = zf.read("AndroidManifest.xml")
    if len(data) < 16:
        return None
    typ = struct.unpack_from("<H", data, 0)[0]
    if typ != _RES_XML_TYPE:
        return None

    strings: list[str] | None = None
    off = struct.unpack_from("<H", data, 2)[0] or 8   # headerSize，通常是 8
    while off + 8 <= len(data):
        ctype, _chsize, size = struct.unpack_from("<HHI", data, off)
        if size < 8:
            return None
        if ctype == _RES_STRING_POOL:
            strings = _string_pool(data, off)
        elif ctype == _RES_XML_START_ELEMENT and strings:
            # ResXMLTree_attrExt 从 off+16 开始：ns(4) name(4) attributeStart(2)
            # attributeSize(2) attributeCount(2) idIndex(2) classIndex(2) styleIndex(2)
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, off + 24)
            for i in range(attr_count):
                pos = off + 16 + attr_start + i * attr_size
                if pos + 20 > len(data):
                    break
                _ns, name_idx, raw, _sz, _res0, dtype, ddata = struct.unpack_from(
                    "<IIIHBBI", data, pos)
                if name_idx >= len(strings) or strings[name_idx] != "package":
                    continue
                if raw != _NO_ENTRY and raw < len(strings):
                    return strings[raw]
                if dtype == _TYPE_STRING and ddata < len(strings):
                    return strings[ddata]
                return None
            # 根元素没有 package 属性（不该发生）
            return None
        off += size
    return None
