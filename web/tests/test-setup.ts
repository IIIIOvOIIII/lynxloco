// vitest 全局 setup：node 环境为组件和 i18n 提供最小 window 桩。
if (typeof (globalThis as { window?: unknown }).window === "undefined") {
  (globalThis as unknown as { window: Record<string, unknown> }).window = {};
}
