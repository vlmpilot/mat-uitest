import { useEffect, useRef } from "react";

export type PollingOptions = {
  /** 上一轮未返回时不发起下一轮（默认 true）。 */
  singleFlight?: boolean;
  /** 卸载或 interval 变化时中止在途请求（默认 true）。 */
  abort?: boolean;
  /** 轮询失败回调；调用方应给出可见提示，不要静默吞掉。 */
  onError?: (err: unknown) => void;
};

/**
 * 统一轮询（2026-11-04）。
 *
 * 解决的问题：
 * - 旧代码 `setInterval(async () => ...)` 在接口慢于周期时会叠加请求，后到的旧响应
 *   覆盖新状态；
 * - `.catch(() => {})` 让断网/500 时界面永远停在最后一次状态，看起来一切正常。
 *
 * 约定：
 * - `interval=null` 关闭轮询（适合"只在某状态下轮询"）；
 * - 只落地最新一轮的结果（seq 校验），卸载后不再写 React 状态；
 * - 失败交给 `onError`，由调用方渲染横幅/提示。
 */
export function usePolling<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  interval: number | null,
  onResult: (value: T) => void,
  options: PollingOptions = {},
): void {
  const fnRef = useRef(fn);
  const resultRef = useRef(onResult);
  const errorRef = useRef(options.onError);
  fnRef.current = fn;
  resultRef.current = onResult;
  errorRef.current = options.onError;

  const singleFlight = options.singleFlight ?? true;
  const abort = options.abort ?? true;

  useEffect(() => {
    if (interval == null) return;
    const controller = new AbortController();
    let stopped = false;
    let inFlight = false;
    let seq = 0;

    const tick = async () => {
      if (stopped || controller.signal.aborted) return;
      if (singleFlight && inFlight) return;
      inFlight = true;
      const mine = ++seq;
      try {
        const value = await fnRef.current(controller.signal);
        if (stopped || mine !== seq) return; // 卸载或已被更新的一轮取代
        resultRef.current(value);
      } catch (err) {
        if (!stopped && !controller.signal.aborted) errorRef.current?.(err);
      } finally {
        inFlight = false;
      }
    };

    void tick();
    const timer = setInterval(() => { void tick(); }, interval);
    return () => {
      stopped = true;
      clearInterval(timer);
      if (abort) controller.abort();
    };
  }, [interval, singleFlight, abort]);
}
