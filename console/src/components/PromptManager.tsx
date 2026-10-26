import {
  Alert, Button, Input, Modal, Select, Space, Tag, Tooltip, Typography, message,
} from "antd";
import { useEffect, useState } from "react";
import {
  getPromptContent, getPrompts, PromptsResp, PromptSlot, savePrompt, selectPrompt, upgradePrompt,
} from "../api";

// Prompt 文件管理（发起人 2026-10-14：控制台里可选择使用的 prompt 文件）。
// 两个槽位（用例生成 gen / 用例转换 convert），每个槽位：
//   · 选择生效文件 —— 落工作区 prompts/.choice.json，解析优先级最高（promptstore.resolve）
//   · 查看/编辑/新建 —— 保存写入工作区 prompts/<文件名>（选中的同名文件保存即生效）
//   · 下拉只列本槽位「同族」文件（gen-v2.prompt / my-gen.prompt…，后端已过滤），
//     选中框直接显示当前生效的模版名——不再有「默认顺序」伪选项（发起人 2026-10-14）
// server 模式下 prompt 由轻云下发，本地选择**不参与生效**——顶部 Alert 说明并禁用操作，
// 但选择仍可保存（记住意图，切回 local 即生效）。

const { Text } = Typography;

const MONO = { fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace" } as const;

function SlotSection({ slot, disabled, onChanged }: {
  slot: PromptSlot; disabled: boolean; onChanged: () => void }) {
  const [editing, setEditing] = useState<{ filename: string; content: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const stale = !!slot.selected && !slot.candidates.some((c) => c.file === slot.selected);
  // 追加段编辑器（2026-10-25）：prompts/<name>.append.md——用户定制拼在模板尾部,
  // 不复制整模板(那会把官方迭代关在门外);server 模式同样生效(随请求发给服务端拼装)
  const [appendOpen, setAppendOpen] = useState(false);
  const [appendText, setAppendText] = useState("");
  const [appendBusy, setAppendBusy] = useState(false);

  const doUpgrade = async () => {
    setBusy(true);
    try {
      const r = await upgradePrompt(slot.name);
      message.success(`已升级到包内新版模板;旧副本备份为 ${r.backup}(你的追加段不受影响)`);
      onChanged();
    } catch (e) { message.error(String(e)); }
    finally { setBusy(false); }
  };

  const openAppend = async () => {
    setAppendOpen(true);
    try {
      const r = await getPromptContent(slot.append_file);
      setAppendText(r.content);
    } catch { setAppendText(""); }   // 还没有追加段文件:空编辑器
  };

  const saveAppend = async () => {
    setAppendBusy(true);
    try {
      const text = appendText.trim();
      if (text) await savePrompt(slot.append_file, text);
      else message.info("内容为空——想清除追加段请直接删除文件,这里不保存空文件");
      setAppendOpen(false);
      onChanged();
    } catch (e) { message.error(String(e)); }
    finally { setAppendBusy(false); }
  };

  const doSelect = async (file: string) => {
    setBusy(true);
    try {
      await selectPrompt(slot.name, file);
      message.success(`已选择 ${file}，立即生效`);
      onChanged();
    } catch (e) { message.error(String(e)); }
    finally { setBusy(false); }
  };

  const openEditor = async (file: string | null) => {
    if (!file) { setEditing({ filename: "", content: "" }); return; }
    try {
      const r = await getPromptContent(file);
      setEditing({ filename: r.file, content: r.content });
    } catch (e) { message.error(String(e)); }
  };

  const save = async () => {
    if (!editing) return;
    const filename = editing.filename.trim();
    if (!filename) { message.warning("先填文件名（建议 <任务名>-v2.prompt，如 gen-v2.prompt）"); return; }
    if (!filename.endsWith(".prompt") && !filename.endsWith(".prompt.example")) {
      message.warning("文件名需以 .prompt 或 .prompt.example 结尾"); return;
    }
    // 与后端 _slot_family 同规则：不合族的名字不会出现在本槽位下拉里，存了也白存
    const stem = filename.replace(/\.prompt(\.example)?$/, "");
    const inFamily = stem === slot.name || stem.startsWith(`${slot.name}-`)
      || stem.startsWith(`${slot.name}_`) || stem.endsWith(`-${slot.name}`)
      || stem.endsWith(`_${slot.name}`);
    if (!inFamily) {
      message.warning(`文件名需与「${slot.title}」同族（如 ${slot.name}-v2.prompt / my-${slot.name}.prompt），否则不会出现在本槽位下拉里`);
      return;
    }
    setBusy(true);
    try {
      const r = await savePrompt(filename, editing.content);
      message.success(r.existed ? `已保存并覆盖 ${filename}` : `已新建 ${filename}`);
      setEditing(null);
      onChanged();
    } catch (e) { message.error(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <div style={{ marginBottom: 18 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <Text strong>{slot.title} prompt</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          当前生效：{slot.active ? slot.active.file : "未找到"}
          {slot.active?.source === "package" && "（包内模板）"}
        </Text>
        {slot.selected
          ? <Tooltip title={`显式选择：${slot.selected}${stale ? "（文件已不存在，自动回落默认模板）" : ""}`}>
              <Tag color={stale ? "orange" : "blue"}>已选择 {slot.selected}</Tag>
            </Tooltip>
          : null}
        {slot.stale && (
          <Tooltip title={`工作区副本 ${slot.stale.file} 落后于包内新版（模板被冻结收不到官方迭代）。升级 = 旧副本改名备份、解析回落包内新版；追加段不受影响。`}>
            <Tag color="red">官方模板已更新</Tag>
          </Tooltip>
        )}
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 6, alignItems: "center" }}>
        {/* 选中框直接显示当前生效的模版名（无「默认顺序」伪选项，发起人 2026-10-14） */}
        <Select style={{ minWidth: 280 }} size="small" disabled={disabled} loading={busy}
          value={slot.active?.file} placeholder="未找到可用 prompt"
          onChange={(v) => doSelect(v)}
          options={slot.candidates.map((c) => ({
            value: c.file,
            label: `${c.file}（${c.source === "package" ? "包内模板" : "工作区"}）`,
          }))} />
        <Button size="small" disabled={disabled}
          onClick={() => openEditor(slot.candidates.find((c) => c.active)?.file ?? null)}>
          查看生效内容
        </Button>
        <Button size="small" type="link" style={{ padding: 0 }} disabled={disabled}
          onClick={() => openEditor(null)}>新建</Button>
        {/* 追加段不受 disabled 限制:server 模式同样生效(随请求发给服务端拼装) */}
        <Button size="small" style={{ marginLeft: "auto" }}
          onClick={openAppend}>
          {slot.has_append ? "✎ 追加段(已配置)" : "＋ 追加段"}
        </Button>
        {slot.stale && (
          <Tooltip title={disabled
            ? "官方代理模式下本地模板不参与当前生效;升级会备份旧副本并回落包内新版,切回 local 模式即用上新模板"
            : "旧副本改名备份(可回退),解析回落包内新版;追加段不受影响"}>
            <Button size="small" danger loading={busy} disabled={disabled}
              onClick={doUpgrade}>升级到新版</Button>
          </Tooltip>
        )}
      </div>
      {appendOpen && (
        <div style={{ marginTop: 8, border: "1px solid #f0f0f0", borderRadius: 8, padding: 10 }}>
          <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
            追加段保存为工作区 {slot.append_file},每次生成/转换时拼在模板**尾部**——
            不复制整个模板(那会让官方迭代进不来);local 与官方代理(server)模式都生效。
          </Text>
          <Input.TextArea size="small" rows={5} style={MONO} value={appendText}
            onChange={(e) => setAppendText(e.target.value)}
            placeholder={"例如:\n- 生成步骤时 intent 一律用中文\n- 断言优先用 textContains"} />
          <Space style={{ marginTop: 6 }}>
            <Button size="small" type="primary" loading={appendBusy} onClick={saveAppend}>
              保存追加段
            </Button>
            <Button size="small" onClick={() => setAppendOpen(false)}>收起</Button>
          </Space>
        </div>
      )}
      {editing !== null && (
        <div style={{ marginTop: 8, border: "1px solid #f0f0f0", borderRadius: 8, padding: 10 }}>
          <Space.Compact style={{ width: "100%", marginBottom: 6 }}>
            <Input size="small" style={MONO} placeholder={`文件名（如 ${slot.name}-v2.prompt）`}
              value={editing.filename} disabled={disabled}
              onChange={(e) => setEditing({ ...editing, filename: e.target.value })} />
            <Button size="small" type="primary" loading={busy} disabled={disabled}
              onClick={save}>保存到工作区</Button>
            <Button size="small" onClick={() => setEditing(null)}>收起</Button>
          </Space.Compact>
          <Input.TextArea size="small" rows={12} style={MONO} value={editing.content}
            disabled={disabled} onChange={(e) => setEditing({ ...editing, content: e.target.value })} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            保存写入工作区 prompts/；文件名需与任务同族（如 {slot.name}-v2.prompt），否则不会出现在下拉里；选中的同名文件保存即生效。
          </Text>
        </div>
      )}
    </div>
  );
}

export default function PromptManager({ open, onClose, onChanged }: {
  open: boolean; onClose: () => void; onChanged: () => void }) {
  const [data, setData] = useState<PromptsResp | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const reload = () => {
    getPrompts().then((r) => { setData(r); setErr(null); })
      .catch((e) => setErr(String(e)));
  };
  useEffect(() => { if (open) reload(); }, [open]);

  const changed = () => { reload(); onChanged(); };

  return (
    <Modal title="Prompt 文件管理" open={open} onCancel={onClose} footer={null} width={720}>
      <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 10 }}>
        选择哪个 prompt 文件在生成/转换时生效；文件保存在工作区 prompts/ 目录（随工作区备份）。
      </Text>
      {err && <Alert type="error" showIcon style={{ marginBottom: 10 }} message={`加载失败：${err}`} />}
      {data?.server && (
        <Alert type="info" showIcon style={{ marginBottom: 10 }}
          message="当前为官方代理模式（GEN_PROMPT_MODE=server）：prompt 由服务端组装，本地选择暂不参与生效（选择会记住，改回 local 即生效）；「追加段」例外——它随请求发给服务端拼装，本模式同样生效。"
        />
      )}
      {data?.slots.map((s) => (
        <SlotSection key={s.name} slot={s} disabled={!!data?.server} onChanged={changed} />
      ))}
    </Modal>
  );
}
