import { Breadcrumb, Button, Input, Modal, Radio, Space, Typography, message } from "antd";
import { useEffect, useState } from "react";
import { FsListing, getFs, StateResp, switchWorkspace } from "../api";

// 工作区切换(发起人需求)→ 2026-10-11 改版:顶部入口移除,
// 弹窗改为受控组件,由概览页「工作区」模块的「切换」按钮唤起(效果与原顶部入口一致)。
// 本地文件夹选择窗(服务端目录浏览器——浏览器拿不到本地绝对路径)+ 迁移档位提示。
// 分享(--share)模式下后端禁用。
export default function WorkspaceSwitcher({ state, open, onClose }: {
  state: StateResp; open: boolean; onClose: () => void }) {
  const [browsePath, setBrowsePath] = useState<string>("");
  const [dirs, setDirs] = useState<string[]>([]);
  const [parent, setParent] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [migrate, setMigrate] = useState<"none" | "graph" | "full">("graph");
  const [busy, setBusy] = useState(false);

  const hasData = state.cases.length > 0 || state.runs.length > 0;
  const home = state.cwd; // 服务端从家目录起跳太远;从当前工作区起跳更贴近用户

  const browse = (path: string): void => {
    getFs(path).then((d: FsListing) => {
      setBrowsePath(d.path); setDirs(d.dirs); setParent(d.parent);
    }).catch((e) => message.error(String(e)));
  };
  useEffect(() => {
    if (open) { setSelected(""); setMigrate(hasData ? "graph" : "none"); browse(""); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const doSwitch = async () => {
    if (!selected) { message.error("先选择目标目录"); return; }
    setBusy(true);
    try {
      const r = await switchWorkspace({ path: selected, migrate });
      message.success(`已更换到 ${r.root}${r.migrated.length ? `（迁移 ${r.migrated.join("、")}）` : ""}`);
      onClose();
      setTimeout(() => window.location.reload(), 600);  // 全量重取 state/用例/报告
    } catch (e) { message.error(String(e)); } finally { setBusy(false); }
  };

  return (
    <Modal title="更换工作区" open={open} onCancel={onClose} width={560}
        footer={<Space>
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" loading={busy} disabled={!selected} onClick={doSwitch}>
            更换到 {selected ? selected.split("/").filter(Boolean).pop() : "…"}
          </Button>
        </Space>}>
        <Space direction="vertical" style={{ width: "100%" }} size={10}>
          <div>
            <div style={{ marginBottom: 6, fontSize: 13 }}>浏览目录（或直接输入绝对路径）</div>
            <Space.Compact style={{ width: "100%" }}>
              <Input value={selected || browsePath} onChange={(e) => setSelected(e.target.value)}
                placeholder="输入或点选目标工作区的绝对路径" />
              <Button onClick={() => browse(browsePath)}>刷新</Button>
            </Space.Compact>
          </div>
          <div style={{ border: "1px solid #f0f0f0", borderRadius: 8, padding: 8, maxHeight: 220, overflow: "auto" }}>
            {parent && <Breadcrumb items={[{ title: <a onClick={() => browse(parent)}>上一级</a> }]}
              style={{ marginBottom: 6 }} />}
            <Space direction="vertical" size={2} style={{ width: "100%" }}>
              {dirs.map((d) => (
                <Button key={d} type="text" size="small" style={{ display: "block", textAlign: "left", width: "100%" }}
                  onClick={() => browse(`${browsePath.replace(/\/$/, "")}/${d}`)}>
                  📁 {d}
                </Button>))}
              {dirs.length === 0 && <span style={{ color: "#8a919f", fontSize: 12 }}>（无子目录）</span>}
            </Space>
          </div>
          {/* 选定入口（2026-10-22 修）：原条件 selected && 是鸡生蛋——selected 只由输入框
              赋值，纯浏览目录时恒空，链接永不出现、确认按钮永远灰着。改为有浏览路径且
              尚未选中时显示。 */}
          {browsePath && browsePath !== selected && (
            <Button size="small" type="link" style={{ padding: 0 }}
              onClick={() => setSelected(browsePath)}>选定当前浏览目录：{browsePath}</Button>
          )}
          {hasData && (
            <div>
              <div style={{ marginBottom: 6, fontSize: 13 }}>迁移原有工作区数据（当前工作区有用例/执行数据）：</div>
              <Radio.Group value={migrate} onChange={(e) => setMigrate(e.target.value)}>
                <Space direction="vertical">
                  <Radio value="none">不迁移——新工作区从空白开始</Radio>
                  <Radio value="graph">迁移用例 + 页面图谱（推荐，体积小）</Radio>
                  <Radio value="full">完整迁移（用例 + 全部 artifacts 含报告，可能很大）</Radio>
                </Space>
              </Radio.Group>
            </div>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            当前：{state.cwd}。更换不删除原目录数据（仅拷贝）；运行中的旧任务会继续写在原工作区直到结束。
          </Typography.Text>
        </Space>
    </Modal>
  );
}
