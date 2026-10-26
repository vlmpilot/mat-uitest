import { Button, Input, Modal, Space, Typography, message } from "antd";
import { useEffect, useRef, useState } from "react";
import { FsListing, getFs, getJob, installApp, probeApp } from "../api";

// 安装测试包(测试准入):服务端目录浏览器选本机安装包 → adb/simctl install。
// 不做 HTTP 上传——配置台是本机服务,APK 就在用户磁盘上,选路径即可(免传 200MB)。
// 安装成功可选回显版本+包指纹(T76 准入证据链,需填包名)。
export default function InstallModal({ open, onClose, serial, platform, onInstalled }: {
  open: boolean; onClose: () => void;
  serial: string | null; platform: "android" | "ios";
  onInstalled?: (summary: string) => void;
}) {
  const isAndroid = platform === "android";
  // 真机（2026-10-19）：devicectl 安装，同样只接受 .app 目录（.ipa 需先解压）
  const isRealIos = platform === "ios" && !!serial && serial !== "simulator";
  const ext = isAndroid ? ".apk" : "";
  const [browsePath, setBrowsePath] = useState("");
  const [dirs, setDirs] = useState<string[]>([]);
  const [files, setFiles] = useState<string[]>([]);
  const [parent, setParent] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const [pkg, setPkg] = useState("");
  const [busy, setBusy] = useState(false);
  // 选中安装包自动识别包名（2026-10-13 发起人：装完不知道包名没法测试）。
  // lastProbed 记录上次自动填的值——用户手改过（与 lastProbed 不同且非空）就不覆盖。
  const [probeNote, setProbeNote] = useState("");
  const lastProbed = useRef("");

  const browse = (path: string): void => {
    getFs(path, ext || undefined).then((d: FsListing) => {
      setBrowsePath(d.path); setDirs(d.dirs); setFiles(d.files || []); setParent(d.parent);
    }).catch((e) => message.error(String(e)));
  };
  useEffect(() => {
    if (open) { setSelected(""); setPkg(""); setProbeNote(""); lastProbed.current = ""; browse(""); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, platform]);

  const join = (base: string, name: string) => `${base.replace(/\/$/, "")}/${name}`;

  // 选中安装包 → 解析包名自动回填（失败静默；用户手填过且非上次自动值则不覆盖）
  const selectAndProbe = (path: string) => {
    setSelected(path);
    setProbeNote("");
    probeApp(path).then((r) => {
      if (!r.package) return;
      lastProbed.current = r.package;
      setPkg((cur) => (cur.trim() === "" || cur.trim() === lastProbed.current ? r.package : cur));
      setProbeNote([r.package, r.label, r.version && `v${r.version}`].filter(Boolean).join(" · "));
    }).catch(() => { /* 解析不出就让用户手填，不打扰 */ });
  };

  const doInstall = async () => {
    if (!selected) { message.error("先选择安装包"); return; }
    setBusy(true);
    try {
      const { job_id } = await installApp({ serial, platform, path: selected, package: pkg || undefined });
      message.info(isAndroid ? "正在安装（覆盖安装，保留数据）…"
        : isRealIos ? "正在安装到真机（devicectl）…" : "正在安装到模拟器…");
      for (let i = 0; i < 300; i++) {
        const job = await getJob(job_id);
        if (job.status !== "running") {
          if (job.status === "done") {
            message.success(job.summary || "安装成功");
            onInstalled?.(job.summary || "");
            onClose();
          } else message.error(job.error || `任务${job.status}`);
          break;
        }
        await new Promise((r) => setTimeout(r, 1500));
      }
    } catch (e) { message.error(String(e)); } finally { setBusy(false); }
  };

  return (
    <Modal title={`安装测试包（${isAndroid ? "Android · adb install -r"
      : isRealIos ? "iOS 真机 · devicectl" : "iOS 模拟器 · simctl"}）`}
      open={open} onCancel={onClose} width={560}
      footer={<Space>
        <Button onClick={onClose}>取消</Button>
        <Button type="primary" loading={busy} disabled={!selected} onClick={doInstall}>安装</Button>
      </Space>}>
      <Space direction="vertical" style={{ width: "100%" }} size={10}>
        <div>
          <div style={{ marginBottom: 6, fontSize: 13 }}>
            {isAndroid ? "选择 .apk 安装包（可直接点选文件）" : "进入 .app 所在目录后，点该目录的「选为安装包」"}
          </div>
          <Space.Compact style={{ width: "100%" }}>
            <Input value={selected || browsePath} onChange={(e) => setSelected(e.target.value)}
              placeholder="安装包绝对路径" />
            <Button onClick={() => browse(browsePath)}>刷新</Button>
          </Space.Compact>
        </div>
        <div style={{ border: "1px solid #f0f0f0", borderRadius: 8, padding: 8, maxHeight: 220, overflow: "auto" }}>
          {parent && <Button block size="small" type="text" style={{ justifyContent: "flex-start" }}
            onClick={() => browse(parent)}>← 上一级</Button>}
          {dirs.map((d) => (
            <div key={d} style={{ display: "flex", alignItems: "center" }}>
              <Button block size="small" type="text" style={{ justifyContent: "flex-start", flex: 1 }}
                onClick={() => browse(join(browsePath, d))}>📁 {d}</Button>
              {!isAndroid && d.endsWith(".app") && (
                <Button size="small" type="link" onClick={() => selectAndProbe(join(browsePath, d))}>选为安装包</Button>)}
            </div>))}
          {files.map((f) => (
            <Button key={f} block size="small" type="text" style={{ justifyContent: "flex-start", display: "flex" }}
              onClick={() => selectAndProbe(join(browsePath, f))}>📦 {f}</Button>))}
          {dirs.length === 0 && files.length === 0 && (
            <span style={{ color: "#8a919f", fontSize: 12 }}>（空目录）</span>)}
        </div>
        <div>
          <div style={{ marginBottom: 6, fontSize: 13 }}>
            包名（选中安装包<b>自动识别</b>，可改；填了则在安装后回显<b>版本号+包指纹</b>）
          </div>
          <Input value={pkg} onChange={(e) => setPkg(e.target.value)}
            placeholder={isAndroid ? "如 com.example.app" : "如 com.example.app（bundleId）"} />
          {probeNote && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              已识别：<Typography.Text code style={{ fontSize: 12 }}>{probeNote}</Typography.Text>
            </Typography.Text>)}
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {isAndroid
            ? "覆盖安装（adb install -r -t）保留应用数据；iOS 模拟器需模拟器架构的 .app 构建。"
            : isRealIos
            ? "真机安装要求 .app 已签名且设备已开启开发者模式；App Store 的 .ipa 需先解压成 .app。"
            : "simctl 安装需模拟器架构的 .app 目录（App Store 的 ipa 装不上模拟器）。"}
        </Typography.Text>
      </Space>
    </Modal>
  );
}
