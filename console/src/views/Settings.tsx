import {
  Alert, Button, Descriptions, Input, Modal, Space, Tag, Tooltip, Typography, Tabs, message,
} from "antd";
import {
  CloudDownloadOutlined, InfoCircleOutlined, KeyOutlined, SafetyOutlined, TeamOutlined,
} from "@ant-design/icons";
import { useEffect, useState } from "react";
import {
  getCreds, getJob, getSeats, SeatsResp, seatRemove, startExport, StateResp, writeCred,
} from "../api";
import PageHeader from "../components/PageHeader";

// 设置页：顶部切 tab（2026-10-12 发起人要求改回 Tabs），四个标签页：
// 使用方式（套餐+团队成员+激活/续期）→ 用例凭据 → 本地数据导出 → 关于。
// 移除的 tab：AI（与概览重复——额度/就绪概览统计卡已有，GEN_* .env 配置指引 EnvDoctor 已覆盖）、
// 设备（只读 serial/型号/状态清单，概览设备卡片已有实时画面与在线状态，纯冗余）。

function SubscribeTab({ state, onRefresh }: { state: StateResp; onRefresh?: () => void }) {
  const [code, setCode] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const activate = async () => {
    if (!code.trim()) return;
    try {
      const resp = await fetch("/api/activate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const data = await resp.json();
      if (resp.ok) {
        setMsg({ ok: true, text: `已激活 ${data.plan},有效期至 ${new Date(data.expires_at * 1000).toLocaleDateString()}` });
        setCode("");
        onRefresh?.();  // 只重拉 state（套餐/额度随 props 更新），不再整页 reload（发起人 2026-10-12）
      } else {
        setMsg({ ok: false, text: data.error || "激活失败" });
      }
    } catch (e) {
      setMsg({ ok: false, text: String(e) });
    }
  };
  return (
    <>
      <Descriptions size="small" column={1} bordered>
        {/* 两个独立维度（2026-10-24 修正）：此前把「模式」和「AI 接入」压成一维，
            让「标准模式」看起来只能自带 API Key。实际标准模式走官方代理也合法。
            功能不分档全员一致，商业化只控官方代理额度与团队人数——不裸露 free/pro */}
        <Descriptions.Item label="使用方式">
          <Tag color={state.license?.plan === "free" || !state.license?.plan ? "default" : "gold"}
            style={{ marginInlineEnd: 0 }}>
            {usageModeLabel(state.license?.plan)}
          </Tag>
          {/* 到期日（2026-10-13）：标准模式才显示，绿色强调；激活/续期（含时长卡核销
              换码）写盘后 onRefresh 重拉 state，这里自然更新 */}
          {state.license?.plan === "pro" && state.license?.expires_at ? (
            <Typography.Text style={{ color: "#389e0d", fontWeight: 600, marginLeft: 8 }}>
              有效期至 {new Date(state.license.expires_at * 1000).toLocaleDateString()}
            </Typography.Text>
          ) : null}
        </Descriptions.Item>
        <Descriptions.Item label="AI 接入">
          <Tag color={state.ai_mode === "server" ? "default" : "gold"} style={{ marginInlineEnd: 8 }}>
            {state.ai_mode === "server" ? "官方代理" : "自带 API Key"}
          </Tag>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            {state.ai_mode === "server"
              ? "有月度上限；切自带 API Key 就不设上限"
              : "本站不设上限，请求直连你选的服务商"}
          </Typography.Text>
        </Descriptions.Item>
        <Descriptions.Item label="激活/续期">
          公测期在<a href="https://mobileaitest.com/#contact" target="_blank" rel="noreferrer">官网联系</a>获取标准模式激活码（更高的官方代理 AI 额度）。
          <Space.Compact style={{ marginTop: 8, display: "flex" }}>
            <Input placeholder="MAT-pro.…" value={code}
                   onChange={(e) => setCode(e.target.value)}
                   onPressEnter={activate} />
            <Button type="primary" onClick={activate}>激活</Button>
          </Space.Compact>
          {msg && <div style={{ marginTop: 6, color: msg.ok ? "#389e0d" : "#cf1322" }}>{msg.text}</div>}
        </Descriptions.Item>
      </Descriptions>
      {/* 团队成员并入使用方式 tab（2026-10-12 发起人）：商业化只控额度+人数，看套餐就该看到成员名额 */}
      <div style={{ marginTop: 16 }}>
        <SeatsCard />
      </div>
    </>
  );
}

// AccountTabWithInvite 半成品已移除(2026-10-13):引用的 AccountTab/InviteBlock 组件
// 从未落地且全仓无引用,导致 tsc 编译失败(TS2304)阻塞整个 console 构建——邀请卡片
// 如需恢复,补齐组件后重新接入使用方式 tab。

function AboutTab({ state }: { state: StateResp }) {
  return (
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label="版本">MAT v{state.version}({state.console})</Descriptions.Item>
      <Descriptions.Item label="官网"><a href="https://mobileaitest.com" target="_blank" rel="noreferrer">mobileaitest.com</a></Descriptions.Item>
      <Descriptions.Item label="开源仓库"><a href="https://github.com/vlmpilot/mat-uitest" target="_blank" rel="noreferrer">github.com/vlmpilot/mat-uitest</a>(AGPL-3.0)</Descriptions.Item>
      <Descriptions.Item label="文档">仓库 docs/(使用手册 13 · 引擎设计 22)</Descriptions.Item>
    </Descriptions>
  );
}

function CredsTab() {
  const [names, setNames] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const load = () => getCreds().then((d) => setNames(d.names)).catch(() => setNames([]));
  useEffect(() => { load(); }, []);

  const add = async () => {
    if (!name.trim() || !value) { message.error("名称与值都必填"); return; }
    try { await writeCred({ action: "set", name: name.trim(), value });
      message.success(`已保存 ${name.trim()}（值不回显）`); setName(""); setValue(""); load();
    } catch (e) { message.error(String(e)); }
  };
  const del = async (n: string) => {
    try { await writeCred({ action: "del", name: n }); load(); } catch (e) { message.error(String(e)); }
  };
  return (
    <>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message="用例凭据是做什么的"
        description="把测试账号、密码、Token 等敏感信息与用例解耦：保存后只存本机（~/.mat/credentials.json，0600 权限），用例参数里以 ${cred.名称} 引用，执行时由引擎注入真实值，报告与日志不回显。多条用例复用同一凭据名，换环境只改一处，也避免敏感信息明文硬编码进用例文件。" />
      <Space.Compact style={{ width: "100%", marginBottom: 12 }}>
        <Input placeholder="名称（如 uat_account）" value={name} onChange={(e) => setName(e.target.value)} />
        <Input.Password placeholder="值" value={value} onChange={(e) => setValue(e.target.value)} />
        <Button type="primary" onClick={add}>保存</Button>
      </Space.Compact>
      {names.length === 0 ? <span style={{ color: "#999" }}>暂无凭据</span> : (
        <Space wrap>
          {names.map((n) => (
            <Tag key={n} closable onClose={() => del(n)} color="blue">{n}</Tag>))}
        </Space>
      )}
    </>
  );
}

function ExportTab() {
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState<string | null>(null);
  const doExport = async (media: boolean) => {
    setBusy(true);
    try {
      const { job_id } = await startExport(media);
      for (let i = 0; i < 200; i++) {
        const job = await getJob(job_id);
        if (job.status !== "running") {
          if (job.status === "done") { message.success(job.summary || "导出完成"); setLink(job.link || null); }
          else message.error(job.error || `任务${job.status}`);
          break;
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
    } catch (e) { message.error(String(e)); } finally { setBusy(false); }
  };
  return (
    <>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message="PIPL 数据可携带权：把本工作区的用例、页面图谱、执行记录（可选截图）打包为 zip 下载。" />
      <Space>
        <Button type="primary" loading={busy} onClick={() => doExport(false)}>导出数据（不含截图）</Button>
        <Button loading={busy} onClick={() => doExport(true)}>导出数据（含截图）</Button>
      </Space>
      {link && <div style={{ marginTop: 12 }}>
        <a href={link} download>⬇ 下载 {link}</a>
      </div>}
    </>
  );
}

// 团队成员（2026-10-25 产品定位调整）：功能不分档，商业化只控额度 + 人数；
// **团队共享只随团队授权提供**——标准/体验授权面向单人使用，`--share` 下上限也恒 1。
// 名单落 artifacts/team_seats.json，任何成员在此都能移除名单里的人（无权限分级：这是内网
// 协作的取舍，不是权限系统）——名额不是权限，别当安全边界讲；误填/离职的名字要能被人清掉，
// 所以不限制"只能删自己"。
// 「联系客服」跳官网联系区（发起人 2026-10-15）：二维码素材不随包分发（PyPI/公开仓均不含），
// 官网 #contact 联系区有公众号二维码，点击跳转扫码即可。
/** 使用方式（与「AI 接入」是两个维度）：free=体验模式、pro=标准模式、team/private=团队模式 */
function usageModeLabel(plan?: string): string {
  const p = (plan || "free").toLowerCase();
  if (p === "free") return "体验模式";
  if (p === "team" || p === "private") return "团队模式";
  return "标准模式";
}

function WxContact() {
  return <a href="https://mobileaitest.com/#contact" target="_blank" rel="noreferrer">联系客服</a>;
}

function SeatsCard() {
  const [data, setData] = useState<SeatsResp | null>(null);
  // 拿不到就按"本机单人"渲染：本机控制台真就只有安装者一个人（2026-10-23）
  const load = () => getSeats().then(setData).catch(() => setData({ seats: [], limit: 1, shared: false }));
  useEffect(() => { load(); }, []);
  // 移除需确认（发起人 2026-10-14）：防误点 × 号直接释放名额。
  // ⚠️ antd Tag 点 × 默认自带消失动画（onClose 只是通知），必须 preventDefault 拦下，
  // Tag 的真正消失由确认后 seatRemove → setData 列表刷新驱动，取消则原地不动
  const remove = (name: string) => {
    Modal.confirm({
      title: `移除成员「${name}」？`,
      content: "移除后该名字立即释放名额；已产生的历史报告不受影响。",
      okText: "移除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => seatRemove(name).then(setData).catch((e) => message.error(String(e))),
    });
  };
  const used = data?.seats.length ?? 0;
  const full = data ? used >= data.limit : false;
  return (
    <>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message={<span><TeamOutlined style={{ color: "#4F5BFF", marginRight: 8 }} />成员管理</span>}
        description={data && !data.shared ? (
          <span>本机单人模式：这个控制台只有你（安装者）。要和同事共用同一个控制台，
            用 <Typography.Text code>mat console --share</Typography.Text> 启动——团队共享随团队授权提供（标准/体验授权面向单人使用）。</span>
        ) : data && data.shared && !data.team_authorized ? (
          <span>团队共享随团队授权提供：当前授权面向单人使用，<Typography.Text code>--share</Typography.Text> 下也只有你 1 人。若名单里的名字就是你自己（换了浏览器/设备，或改过名字）：点右侧 <Typography.Text code>×</Typography.Text> 移除旧名字即可进入；要与同事共用同一个控制台，请联系我们获取团队授权（<WxContact />，或加微信 lazy_vizen）。</span>
        ) : (
          <span>团队模式按成员管理：同事首次进入填名字即占一位，当前 {used}/{data?.limit ?? 5}{full ? "——已满，请" : "，如需增加名额请"}<WxContact />。</span>
        )} />
      {data && data.seats.length > 0 ? (
        <Space size={[6, 6]} wrap>
          {/* 行首前缀（发起人 2026-10-13）：「团队成员：」说明这行 Tag 是成员名单 */}
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>团队成员：</Typography.Text>
          {data.seats.map((s) => (
            <Tag key={s.name} closable
              onClose={(e) => { e.preventDefault(); remove(s.name); }}
              style={{ fontSize: 13, padding: "3px 10px" }}>
              {/* 悬停显示登记时间：包在名字上（发起人 2026-10-14：Tag 里不再跟「席」字） */}
              <Tooltip title={`首次 ${s.first_seen?.replace("T", " ")} · 最近 ${s.last_seen?.replace("T", " ")}`}>
                <span>{s.name}</span>
              </Tooltip>
            </Tag>
          ))}
        </Space>
      ) : (
        <Typography.Text type="secondary">
          {data && !data.shared
            ? "本机单人模式：名单里只有安装者本人。"
            : "暂无成员登记——同事打开控制台填名字后出现在这里。"}
        </Typography.Text>
      )}
    </>
  );
}

export default function Settings({ state, onRefresh }: {
  state: StateResp; onRefresh?: () => void }) {
  const items = [
    { key: "subscribe", label: <span><SafetyOutlined style={{ color: "#4F5BFF", marginRight: 6 }} />使用方式</span>,
      children: <SubscribeTab state={state} onRefresh={onRefresh} /> },
    { key: "creds", label: <span><KeyOutlined style={{ color: "#4F5BFF", marginRight: 6 }} />用例凭据</span>,
      children: <CredsTab /> },
    { key: "export", label: <span><CloudDownloadOutlined style={{ color: "#4F5BFF", marginRight: 6 }} />本地数据导出</span>,
      children: <ExportTab /> },
    { key: "about", label: <span><InfoCircleOutlined style={{ color: "#4F5BFF", marginRight: 6 }} />关于</span>,
      children: <AboutTab state={state} /> },
  ];
  return (
    <>
      <PageHeader title="设置" desc="使用方式、用例凭据、本地数据导出与关于;写盘型配置经 .env / CLI 生效。" />
      <Tabs items={items} />
    </>
  );
}
