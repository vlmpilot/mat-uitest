import { Button, Card, Tooltip } from "antd";
import { ReactNode, useEffect, useRef, useState } from "react";

// 单设备投屏(发起人需求:只有被选中的设备进行投屏):
// 固定屏宽按手机比例显示;轮询纪律沿用 DeviceCards(1s/帧、在途守卫、页面隐藏跳过、
// 连续 3 次失败转离线)。
// ⚠️ 离线不停轮询(发起人 2026-10-12):img 常驻挂载、离线时仅视觉隐藏——
// 此前离线即卸载 img,「每 5 秒自动重试」从未真正发出请求,永远卡离线。
// 「立即恢复」立即探测一次;若仍失败给出去向指引(iOS→概览页查 WDA,Android→查 adb),
// 不再沉默打回离线(用户视角=点了没反应)。
const POLL_MS = 1000;
const OFFLINE_POLL_MS = 5000;
const OFFLINE_AFTER = 3;
const SCREEN_WIDTH = 227;

export default function DeviceScreen({ serial, platform, extra }: {
  serial: string | null; platform: "android" | "ios"; extra?: ReactNode }) {
  const [live, setLive] = useState(true);
  const [ts, setTs] = useState(Date.now());
  const [fails, setFails] = useState(0);
  const [hint, setHint] = useState(false);
  const offline = fails >= OFFLINE_AFTER;
  const inFlight = useRef(true);
  const manual = useRef(false);   // 刚点过「立即恢复」→ 若再次转离线,给指引而非沉默

  useEffect(() => {  // 切换设备 ⇒ 重置轮询状态（只投屏被选中的设备）
    setLive(true); setFails(0); setHint(false);
    manual.current = false; inFlight.current = true; setTs(Date.now());
  }, [serial, platform]);

  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => {
      if (!document.hidden && !inFlight.current) { inFlight.current = true; setTs(Date.now()); }
    }, offline ? OFFLINE_POLL_MS : POLL_MS);
    return () => clearInterval(t);
  }, [live, offline, serial, platform]);

  // 立即恢复:立即探测一次(重置计数 + 换时间戳触发 img 重载)
  const resume = () => {
    manual.current = true; setHint(false);
    setFails(0); inFlight.current = true; setTs(Date.now());
  };
  useEffect(() => {   // 手动恢复后再转离线 ⇒ 说明不可用的原因与去向
    if (offline && manual.current) { manual.current = false; setHint(true); }
  }, [offline]);

  const qs = `serial=${encodeURIComponent(serial || "")}&platform=${platform}`;
  return (
    <Card size="small"
      title={<Tooltip title="仅对当前选中设备投屏，约 1s/帧刷新"><span>投屏</span></Tooltip>}
      extra={extra}>
      <div style={{ width: SCREEN_WIDTH, height: 400, background: "#fafafa", borderRadius: 8,
        display: offline ? "flex" : "none", flexDirection: "column", alignItems: "center",
        justifyContent: "center", color: "#999", border: "1px solid #f0f0f0" }}>
        设备离线(连续 {fails} 次截图失败)
        <div style={{ fontSize: 12, marginTop: 6, color: "#8a919f", padding: "0 14px",
          textAlign: "center" }}>
          每 5 秒自动重试——设备/WDA 就绪后自动恢复
        </div>
        {hint && (
          <div style={{ fontSize: 12, marginTop: 8, color: "#ad6800", padding: "0 14px",
            textAlign: "center" }}>
            {platform === "ios"
              ? "仍未恢复——大概率 WDA 未就绪：请到「概览」页检查 iOS WDA 状态，一键启动后回到本页等待自动恢复"
              : "仍未恢复——请到「概览」页检查设备连接与 adb 授权"}
          </div>
        )}
        <Button size="small" style={{ marginTop: 10 }} onClick={resume}>立即恢复</Button>
      </div>
      <img alt="投屏" style={{ width: SCREEN_WIDTH, borderRadius: 10,
        display: offline ? "none" : "block", background: "#000", border: "1px solid #e5e6eb" }}
        src={`/api/screenshot?${qs}&width=${SCREEN_WIDTH}&t=${ts}`}
        onError={() => { inFlight.current = false; setFails((f) => f + 1); }}
        onLoad={() => { inFlight.current = false; manual.current = false; setHint(false); setFails(0); }} />
    </Card>
  );
}
