<div align="center">

# Xiaomi Miloco

[English](README.md) | 简体中文

[![最新版本](https://img.shields.io/github/v/release/IIIIOvOIIII/lynxloco?label=release)](https://github.com/IIIIOvOIIII/lynxloco/releases/latest)
[![下载量](https://img.shields.io/github/downloads/IIIIOvOIIII/lynxloco/total)](https://github.com/IIIIOvOIIII/lynxloco/releases)
[![Star 数](https://img.shields.io/github/stars/IIIIOvOIIII/lynxloco)](https://github.com/IIIIOvOIIII/lynxloco/stargazers)
[![欢迎 PR](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/IIIIOvOIIII/lynxloco/pulls)

</div>

小米面向未来的全屋智能 AI 开源方案，可用米家摄像头或标准 RTSP 摄像头流作为全模态感知入口，可用 MiMo 或 OpenAI 兼容 LLM 端点作为智能大脑，并以 Agent 插件形式运行在 [OpenClaw](https://openclaw.ai) 之上，联动米家与 Home Assistant 设备带来主动智能体验。

Miloco 2.0 能感知家中发生的事件，能基于常识主动判断并操控设备，能将"模糊又长期"的目标拆解成可追踪的家庭任务，能识别家庭成员、依托家庭记忆为每位成员提供个性化服务——查询和控制设备、把家调到成员舒适的状态，或在合适的时机给出有用的提醒。

<p align="center"><a href="https://www.bilibili.com/video/BV1fALo6hEkc"><img src="assets/video_cover.png" width="600" alt="Xiaomi Miloco 视频介绍" /></a></p>

## 最新动态

- **LynxLoco 构建** — 在原有 Miloco 家庭面板与 OpenClaw 插件流程基础上，补充支持 OpenAI 兼容 LLM 端点进行 Omni 推理、RTSP 摄像头接入，以及 Home Assistant 集成与控制。
- **2026-08-06** — 发布 v2026.8.6：v2026.8.5 的紧急修复版，回退相机 IP 直连（随其更新的原生库会让部分老款相机的相机进程 SIGSEGV 崩溃）；v2026.8.5 其余内容保持不变，并将 Smart Crop 缩放改为逐轴贴住同档全景画面。
- **2026-08-05** — 发布 v2026.8.5：新增实验性宠物识别（含完整注册与花名册流程）、Smart Crop 自适应分辨率（Omni 推理前裁切活动区域）、面板一键自升级；并新增相机 IP 直连与跨 NAT 拉流诊断、agent 行动台账、Perf 页进程 CPU / 线程数图表，以及削减默认空闲线程池。
- **2026-07-17** — 发布 v2026.7.17：新增 Hermes Agent 兼容层（可插拔 agent runtime）、双摄多通道双流感知、Omni 多模型支持与运行时帧率热更；并新增独立任务 Tab、相机级麦克风开关，以及时区 / 版本 / 安装加固。
- **2026-07-03** — 发布 v2026.7.3：新增事件反馈打包，以及全新安装时主动发起的对话式初始化引导；并改进面板内模型配置管理、感知稳定性（非人误检防护）、相机生命周期与 CLI 诊断。
- **2026-06-18** — Miloco 2.0 正式发布：重构为 OpenClaw 插件，新增通用常识、身份识别、家庭记忆、家庭任务、主动智能、家庭面板。详见下方[核心特性](#核心特性)。

## 核心特性

- **通用常识** — 无需预设规则，基于系统内建的通用常识自动识别危险隐患并分级预警（如孩子玩刀具、老人跌倒）。
- **身份识别** — 融合人脸、体态等身份信息，由大模型实现家庭成员的身份识别，支持主动注册新成员，以及基于身份的个性化操作。
- **家庭记忆** — 从感知与交互中沉淀家庭成员的长期习惯与偏好，作为 Agent 主动决策时的参考依据；长期稳定的习惯还可主动提醒，或升级为家庭任务自动执行。
- **家庭任务** — 从单一的「条件触发规则」升级为可长期运转的复杂家庭任务：条件自动化（"有人进门就开灯"）、定时提醒（"每天提醒吃药"）、习惯统计（"每天运动半小时"）等，触发后由 Agent 理解意图并自主执行。
- **主动智能** — 以通用常识、身份识别、家庭记忆、家庭任务四大能力为基础，让系统像有常识、懂家人、会规划的管家一样主动观察、判断并适时干预，在用户开口前把事做好。
- **灵活摄像头输入** — 有米家摄像头就直接用米家；也可以添加普通 RTSP / RTSPS 摄像头流。RTSP 摄像头能在面板预览，也能像原生摄像头一样单独打开或关闭感知。
- **OpenAI 兼容 LLM 推理** — 除了 MiMo，Miloco 也可以调用 OpenAI 兼容模型网关。文本兼容服务可走 OpenAI Chat Completions；要做摄像头 / 图片感知，请使用能接收图片并返回结构化 JSON 的 OpenAI Responses 端点。
- **Home Assistant 集成** — 可连接一个 Home Assistant 实例，把选中的 entity 导入 Miloco，并逐个决定哪些设备只允许查看、哪些设备允许 Miloco 控制。
- **家庭面板** — 面向用户的 Web 面板，查看家中实时概览、米家设备、家庭成员与家庭档案、历史事件日志。

> [!TIP]
> **养成你自己的 Miloco。** 它的初始表现未必合你心意——直接通过 OpenClaw 告诉 Miloco（如"家里乱不用提醒我"），它就记住你的偏好、相应调整主动行为。你每说一句，就是在"养成"一个更懂你家的 Miloco，越用越贴心。

## 前置条件

- **硬件**：建议内存 ≥ 4GB，存储 ≥ 256GB，7×24 常驻运行，推荐 Mac mini
- **操作系统**：macOS / Linux（Windows 请在 WSL 中运行）
- **OpenClaw** — Miloco 以插件形式运行其上，需先[安装](https://openclaw.ai)且版本 ≥ 2026.5.2
- **摄像头来源**：可以是小米账号中已接入米家的摄像头，也可以是局域网内可访问的 RTSP / RTSPS 摄像头地址
- **多模态大模型访问方式** — 仍推荐使用[小米 MiMo](https://platform.xiaomimimo.com)：感知用 MiMo-v2.5，Agent 用 MiMo-v2.5-pro（在 OpenClaw 中配置）。也可以改用 OpenAI 兼容端点，例如本地网关、Ollama 风格网关、vLLM 代理或其它 provider proxy
- **可选 Home Assistant 实例**：如果希望 Miloco 查看或控制 Home Assistant 设备，请提前准备 Home Assistant 地址和 Long-Lived Access Token

> [!CAUTION]
> **成本提示**：Miloco 2.0 的感知与 Agent 主要依赖云端大模型，会持续产生 API 调用费用，请关注用量。可在家庭面板「模型」页查看 token 消耗。

## 安装

### 方式一：通过 Agent 安装（推荐）

适用于 **OpenClaw** 和 **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** —— 向你的 Agent 发送以下指令：

```text
帮我安装 Miloco 插件：https://raw.githubusercontent.com/IIIIOvOIIII/lynxloco/main/scripts/install-guide.md
```

### 方式二：命令行一键安装

```bash
curl -LsSf https://github.com/IIIIOvOIIII/lynxloco/releases/latest/download/install.sh | bash
```

默认为 OpenClaw。如果要装给 Hermes Agent，显式指定：

```bash
curl -LsSf https://github.com/IIIIOvOIIII/lynxloco/releases/latest/download/install.sh | bash -s -- --agent-platform=hermes
```

### 方式三：从源码构建

在项目根目录执行：

```bash
bash scripts/install.sh --dev   # 从源码构建（scripts/build.sh）后本地安装
```

---

### Windows（WSL）

无论选用上面哪种方式，都暂不支持原生 Windows，请在 [WSL](https://learn.microsoft.com/zh-cn/windows/wsl/install) 中安装并运行。

> [!IMPORTANT]
> **本地拉流需额外配置 WSL 网络。** 家庭面板「家里此刻」的实时画面靠局域网拉取摄像头流，而 WSL 默认 NAT 模式会拦截摄像头发来的 UDP 包——不配置则画面加载不出来，需启用镜像网络模式并放行 Hyper-V 防火墙。

1. **在 Windows 侧** —— 在 `%USERPROFILE%\.wslconfig`（即 `C:\Users\<你的用户名>\.wslconfig`，没有则新建）中加入以下内容，再在 PowerShell 执行 `wsl --shutdown` 重启 WSL：

   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

2. **在 Windows 侧（管理员 PowerShell）** —— 放行入站流量：

   ```powershell
   Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
   ```

3. **在 WSL 内** —— 装好 Miloco 后执行 `miloco-cli doctor` 验证（会检查防火墙与 WSL 网络配置）。

## 快速开始

安装完成后，先重启 OpenClaw 网关让插件生效：

```bash
openclaw gateway restart
```

随后打开家庭面板完成首次配置：

```bash
miloco-cli dashboard   # 在浏览器打开家庭面板（或直接访问 http://<host>:1810/）
```

在面板中按下面几步上手：

1. **配置模型** — 在「模型」页填入 MiMo 的 api_key；或者新增一套 OpenAI 兼容模型配置，填 Base URL、模型名、协议和 API Key（如需要）。
2. **添加摄像头** — 有米家摄像头就绑定小米账号；有普通摄像头就到面板里的「RTSP 摄像头」区域添加。
3. **开启摄像头感知** — 在「概览」页为需要感知的摄像头打开开关。没打开的摄像头可以留在列表里，但不会被分析。
4. **可选：连接 Home Assistant** — 打开「Home Assistant」页，测试连接，导入想让 Miloco 看到的 entity，再只给可信设备开启控制权限。

也可改用命令行完成：

```bash
miloco-cli config set model.omni.api_key sk-xxx   # 配置模型密钥（默认即 MiMo，通常只需这一项）
miloco-cli account bind                           # 绑定小米账号
miloco-cli scope camera enable <did>              # 开启指定摄像头感知
```

跑起来之后，日常怎么用见 [使用说明书](user_guide_zh.md)。

## 大白话配置指引

如果不知道该填什么，优先用网页面板操作；CLI 更适合服务器初始化、批量配置或排查问题时使用。

### 1. 使用 OpenAI 兼容 LLM 端点

这一步可以理解成：告诉 Miloco「以后用这个模型服务器来帮你看图、思考和输出结果」。

1. 打开家庭面板，进入「模型」页。
2. 新增一套 Omni 模型配置。
3. 按下面填：
   - **Base URL**：模型服务的 OpenAI 兼容地址，一般以 `/v1` 结尾，例如 `http://llm.local:11434/v1`。
   - **模型名**：模型服务里真实存在的名字，例如 `grok-4.6` 或 `qwen3.5:2b-mlx`。
   - **协议**：摄像头 / 图片感知请选 **OpenAI Responses**；只有纯文本能力的兼容服务才选 **OpenAI Chat Completions**。
   - **API Key**：模型网关需要就填；某些本地网关可以留空。
4. 先点「测试」。测试通过后，再点「启用」。

注意：有 `/v1/responses` 接口，不代表一定能给 Miloco 用。Miloco 感知需要模型真的能看图，并且能返回非空文本 / JSON。如果页面提示视觉预检或结构化预检返回空结果，就要先修模型网关，或换成真正支持视觉的模型。

CLI 等价操作：

```bash
miloco-cli admin omni test \
  --label local-vlm \
  --model <模型名> \
  --base-url http://llm.local:11434/v1 \
  --api-protocol openai_responses \
  --api-key sk-xxx

miloco-cli admin omni create \
  --label local-vlm \
  --model <模型名> \
  --base-url http://llm.local:11434/v1 \
  --api-protocol openai_responses \
  --api-key sk-xxx \
  --activate
```

### 2. 添加 RTSP 摄像头

这一步可以理解成：告诉 Miloco「这个摄像头不是米家的，但你也可以看它的直播流」。

1. 先用 VLC、FFmpeg 或摄像头厂商 App 确认 RTSP 流本身能播放。
2. 打开家庭面板，找到「RTSP 摄像头」区域。
3. 点击「添加 RTSP 摄像头」。
4. 按下面填：
   - **名称**：你看得懂的名字，例如「客厅摄像头」。
   - **房间**：摄像头所在位置，例如「客厅」。
   - **RTSP URL**：例如 `rtsp://camera.local/stream1`。
   - **用户名 / 密码**：摄像头需要登录时才填。
   - **传输方式**：优先选 `tcp`，家用网络里通常更稳。
   - **音频**：只有这路流有可用音频、且你的模型链路需要音频时再打开。
5. 先点「测试」。测试通过后保存。
6. 新增 RTSP 摄像头默认是关闭感知的；确认画面正常后，再打开它的感知开关。

CLI 等价操作。密码用 stdin 传入，避免留在 shell history 里：

```bash
printf '%s\n' '<摄像头密码>' | miloco-cli camera rtsp test \
  --uri 'rtsp://camera.local/stream1' \
  --username '<摄像头用户名>' \
  --password-stdin \
  --transport tcp

printf '%s\n' '<摄像头密码>' | miloco-cli camera rtsp add \
  --name '客厅摄像头' \
  --room '客厅' \
  --uri 'rtsp://camera.local/stream1' \
  --username '<摄像头用户名>' \
  --password-stdin \
  --transport tcp

miloco-cli camera list
miloco-cli camera enable rtsp:<上一步列表里的 id>
```

### 3. 接入 Home Assistant

这一步可以理解成：告诉 Miloco「Home Assistant 里这些设备你可以看，其中一部分我也允许你控制」。

1. 在 Home Assistant 的用户资料页创建一个 **Long-Lived Access Token**。
2. 打开 Miloco 家庭面板，进入「Home Assistant」页。
3. 填 Home Assistant 地址，例如 `http://homeassistant.local:8123`。
4. 粘贴 token，点击「测试」。
5. 测试通过后保存连接，并刷新实体列表。
6. 只导入你想让 Miloco 看到的 entity。
7. 默认先保持只读；确认可信后，再逐个给设备开启控制权限，不建议一上来全开。

CLI 等价操作：

```bash
printf '%s\n' '<HA 长期访问令牌>' | miloco-cli home-assistant test \
  --url http://homeassistant.local:8123 \
  --token-stdin

printf '%s\n' '<HA 长期访问令牌>' | miloco-cli home-assistant connect \
  --url http://homeassistant.local:8123 \
  --token-stdin

miloco-cli home-assistant refresh --pretty
miloco-cli home-assistant import light.living_room
miloco-cli home-assistant enable-control light.living_room
```

## 项目结构

```text
miloco-plugin/
├── backend/             # uv workspace
│   ├── miloco/          # 主服务：感知引擎、规则、MIoT 网关
│   └── miot/            # MIoT SDK（独立子包）
├── cli/                 # miloco-cli 命令行工具
├── plugins/
│   ├── openclaw/        # OpenClaw 插件（TypeScript）
│   └── skills/          # Agent Skill 文档
├── web/                 # 家庭面板（React 19 + Vite）
├── scripts/             # build.sh / install.sh / manifest.json
└── knowledge/           # 项目知识库
```

## 深入文档

- [后端服务](backend/README.md) — FastAPI + 感知引擎 + 规则 + MIoT 网关
- [命令行 miloco-cli](cli/README.md) — 服务、设备、配置管理
- [家庭面板 web](web/README.md) — 部署架构与本地开发
- [完整知识库](knowledge/README.md) — 架构 / 模块 / 功能 / API 速查

## 交流群

遇到问题、想反馈或交流玩法，欢迎扫码加入飞书用户群（二维码永久有效）：

<img src="assets/Xiaomi_Miloco_Feishu_Group.png" width="240" alt="Xiaomi Miloco 用户群" />

## 致谢

Miloco 站在以下开源项目之上：

- [OpenClaw](https://openclaw.ai) — AI Agent 运行时与插件平台
- [jMuxer](https://github.com/samirkumardas/jmuxer)（MIT）— 家庭面板实时视频流封装
- [BGE / bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)（智源研究院，MIT）— 文本向量化模型
- [Silero VAD](https://github.com/snakers4/silero-vad)（Silero Team，MIT）— 语音活动检测，门控感知语音字段

## 许可证

完整许可条款见 [LICENSE.md](LICENSE.md)。

**重要声明**：本项目仅限非商业用途。未经小米公司书面授权，不得用于开发应用程序（APP）、Web 服务或其他形式的软件。
