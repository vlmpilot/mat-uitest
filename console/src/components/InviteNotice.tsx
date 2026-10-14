import { Alert, Button } from "antd";
import { useEffect, useState } from "react";

// 概览页的「邀请奖励到账」提示。
//
// 两条纪律：
//  ① **只在到账时冒一条，不常驻** —— 常驻提示会变成噪音，用户很快就不看了；
//     所以点「知道了」会写 ack（POST action=ack，纯本地动作）。
//  ② 读的是 GET /api/invite，**只读本地快照、不联网**（与 `mat status` 同源同纪律）。
export default function InviteNotice() {
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/invite")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setNotice(d?.notice ?? null))
      .catch(() => setNotice(null));
  }, []);

  if (!notice) return null;

  return (
    <Alert type="success" showIcon style={{ marginBottom: 16 }} message={notice}
      action={<Button size="small" onClick={() => {
        void fetch("/api/invite", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "ack" }),
        });
        setNotice(null);
      }}>知道了</Button>} />
  );
}
