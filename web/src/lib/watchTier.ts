/**
 * 状态条「看家」项的分档判据。
 *
 * 抽成纯函数是为了能被单测钉住——组件本身不在测试范围内（仓库没装渲染库），
 * 判据留在组件里就只能靠复刻一份来测，而复刻件与实现漂移之后测试照样绿。
 *
 * 米家授权失效会停止米家相机感知；已连接并启用的 RTSP 相机独立于米家授权。
 */

import { miotTone } from "./miotTone";

export type WatchTier = "auth-stopped" | "watching" | "standby" | "not-ready" | "resting";

export function watchTier(input: {
  miot: { bound: boolean; authDegraded?: boolean };
  perception: { running: boolean; ready: boolean };
  allCamerasOff?: boolean;
  hasActiveRtspCamera?: boolean;
}): WatchTier {
  if (miotTone(input.miot) === "degraded" && !input.hasActiveRtspCamera) {
    return "auth-stopped";
  }
  if (!input.perception.running) return "resting";
  if (!input.perception.ready) return "not-ready";
  return input.allCamerasOff ? "standby" : "watching";
}
