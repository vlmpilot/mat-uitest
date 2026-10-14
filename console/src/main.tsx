import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

// 主题:主色不变(#4F5BFF 系列报告蓝),统一圆角/字号层级——AntD 默认直角卡片
// 显得生硬,8px 圆角 + 常规字重标题是「内部工具→可对外演示产品」的第一步。
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{
      token: {
        colorPrimary: "#4F5BFF",
        borderRadius: 8,
        fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", Roboto, sans-serif',
      },
    }}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
);
