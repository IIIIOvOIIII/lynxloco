import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { RtspCameraControls, RtspPerceptionSwitch } from "@/components/HeroNow";
import i18n from "@/i18n";
import type { CameraSummary } from "@/lib/types";

function camera(overrides: Partial<CameraSummary> = {}): CameraSummary {
  return {
    id: "rtsp:1",
    sourceType: "rtsp",
    name: "Kitchen RTSP",
    roomName: "Kitchen",
    enabled: false,
    connected: false,
    videoCodec: null,
    audioCodec: null,
    lastFrameUnixMs: null,
    hasPassword: true,
    errorCode: null,
    errorMessage: null,
    perceptionPrompt: "",
    ...overrides,
  };
}

describe("RtspPerceptionSwitch", () => {
  it("exposes switch state from the RTSP enabled flag", () => {
    const element = RtspPerceptionSwitch({
      camera: camera({ enabled: true }),
      busy: false,
      label: "感知",
      busyLabel: "启用前正在测试连接…",
      ariaLabel: "停用 Kitchen RTSP 的 RTSP 感知",
      title: "停用 RTSP 感知",
      onToggle: vi.fn(),
    });

    expect(element.type).toBe("button");
    expect(element.props.role).toBe("switch");
    expect(element.props["aria-checked"]).toBe(true);
    expect(element.props.disabled).toBe(false);
  });

  it("clicking requests the opposite enabled state through the existing callback", () => {
    const rtsp = camera({ enabled: false });
    const onToggle = vi.fn();
    const element = RtspPerceptionSwitch({
      camera: rtsp,
      busy: false,
      label: "感知",
      busyLabel: "启用前正在测试连接…",
      ariaLabel: "启用 Kitchen RTSP 的 RTSP 感知",
      title: "启用 RTSP 感知",
      onToggle,
    });

    element.props.onClick();

    expect(onToggle).toHaveBeenCalledWith(rtsp, true);
  });

  it("does not call the toggle callback while busy", () => {
    const onToggle = vi.fn();
    const element = RtspPerceptionSwitch({
      camera: camera({ enabled: false }),
      busy: true,
      label: "感知",
      busyLabel: "启用前正在测试连接…",
      ariaLabel: "启用 Kitchen RTSP 的 RTSP 感知",
      title: "启用 RTSP 感知",
      onToggle,
    });

    element.props.onClick();

    expect(element.props.disabled).toBe(true);
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("renders the perception notes button for RTSP camera controls", async () => {
    await i18n.changeLanguage("zh");

    const html = renderToStaticMarkup(
      <div>
        <RtspCameraControls
          camera={camera({ perceptionPrompt: "电视反光请忽略" })}
          busy={false}
          canToggle={true}
          onEdit={vi.fn()}
          onDelete={vi.fn()}
          onEditPrompt={vi.fn()}
          onToggle={vi.fn()}
        />
      </div>,
    );

    expect(html).toContain("编辑「Kitchen RTSP」的感知须知");
    expect(html).toContain("编辑感知须知（已配置）");
    expect(html).toContain("须知");
  });
});
