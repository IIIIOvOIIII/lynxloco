<div align="center">

# Xiaomi Miloco

English | [简体中文](README.zh.md)

[![Latest release](https://img.shields.io/github/v/release/IIIIOvOIIII/lynxloco?label=release)](https://github.com/IIIIOvOIIII/lynxloco/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/IIIIOvOIIII/lynxloco/total)](https://github.com/IIIIOvOIIII/lynxloco/releases)
[![Stars](https://img.shields.io/github/stars/IIIIOvOIIII/lynxloco)](https://github.com/IIIIOvOIIII/lynxloco/stargazers)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/IIIIOvOIIII/lynxloco/pulls)

</div>

Xiaomi's open-source AI solution for the future of whole-home intelligence. It can use Mi Home cameras or standard RTSP camera streams as a full-modal perception gateway, use MiMo or an OpenAI-compatible LLM endpoint as its intelligent brain, and run as an Agent plugin on top of [OpenClaw](https://openclaw.ai) to orchestrate Mi Home and Home Assistant devices for a proactive, intelligent experience.

Miloco 2.0 perceives what happens at home, makes proactive decisions and controls devices based on common sense, breaks down "vague and long-term" goals into trackable household tasks, recognizes family members, and—drawing on home memory—delivers personalized service to each member: querying and controlling devices, tuning the home to each member's comfort, or offering useful reminders at the right moment.

<p align="center"><a href="https://www.bilibili.com/video/BV1p4jw6nEVX"><img src="assets/video_cover_en.jpeg" width="600" alt="Xiaomi Miloco video intro" /></a></p>

## What's New

- **LynxLoco build** — adds OpenAI-compatible LLM endpoints for Omni inference, RTSP camera access, and Home Assistant integration / control while keeping the original Miloco dashboard and OpenClaw plugin workflow.
- **2026-08-06** — Release v2026.8.6: hotfix for v2026.8.5 — reverts camera IP direct-connect, whose bundled native library could crash the camera process with SIGSEGV on some older camera models; everything else from v2026.8.5 stays, plus Smart Crop scaling reworked to fit the same-tier panorama frame per axis.
- **2026-08-05** — Release v2026.8.5: adds experimental pet recognition with a full registration and roster workflow, Smart Crop adaptive resolution that crops the active region before omni inference, and one-click self-upgrade from the dashboard; plus camera IP direct-connect with cross-NAT streaming diagnostics, an agent action ledger, process CPU / thread-count charts on the Perf page, and leaner default thread pools.
- **2026-07-17** — Release v2026.7.17: adds a Hermes Agent compatibility layer for pluggable agent runtimes, dual-camera multi-channel dual-stream perception, and omni multi-model support with runtime FPS hot-reload; plus a dedicated Tasks tab, per-camera mic toggle, and timezone / version / install hardening.
- **2026-07-03** — Release v2026.7.3: adds event-feedback packaging and a conversational first-run setup, proactively initiated on fresh installs; plus improvements to in-dashboard model-config management, perception stability (false-"person" detection guarding), camera lifecycle, and CLI diagnostics.
- **2026-06-18** — Miloco 2.0 officially released: re-architected as an OpenClaw plugin, adding general common sense, identity recognition, home memory, household tasks, proactive intelligence, and a home dashboard. See [Core Features](#core-features) below.

## Core Features

- **General Common Sense** — No preset rules required. Built-in common sense automatically detects hazards and raises tiered alerts (e.g. a child playing with knives, an elderly person falling).
- **Identity Recognition** — Fuses identity signals such as faces and body posture, with the large model recognizing family members. Supports proactively registering new members and identity-based personalized operations.
- **Home Memory** — Distills long-term habits and preferences of family members from perception and interaction, used as a reference when the Agent makes proactive decisions. Stable long-term habits can also trigger proactive reminders or be promoted into automatically executed household tasks.
- **Household Tasks** — Upgrades from single "condition-triggered rules" to complex, long-running household tasks: conditional automation ("turn on the lights when someone enters"), scheduled reminders ("remind me to take medicine every day"), habit tracking ("exercise half an hour daily"), and more. Once triggered, the Agent understands the intent and executes autonomously.
- **Proactive Intelligence** — Built on the four foundational capabilities—general common sense, identity recognition, home memory, and household tasks—the system observes, reasons, and intervenes at the right time like a butler with common sense who knows the family and can plan ahead, getting things done before the user even asks.
- **Flexible Camera Input** — Use Mi Home cameras when you have them, or add ordinary RTSP / RTSPS camera streams. RTSP cameras can be previewed in the dashboard and can be individually enabled or disabled for perception, just like native cameras.
- **OpenAI-compatible LLM Inference** — Besides MiMo, Miloco can call OpenAI-compatible model gateways. Use OpenAI Chat Completions for text-compatible providers, or OpenAI Responses for image-sequence visual perception when the endpoint can accept images and return structured JSON.
- **Home Assistant Integration** — Connect one Home Assistant instance, import selected entities into Miloco, and decide entity by entity which devices Miloco may only read and which ones it may control.
- **Home Dashboard** — A user-facing web dashboard for viewing a real-time overview of the home, Mi Home devices, family members and profiles, and the history of past events.

> [!TIP]
> **Raise your own Miloco.** Its out-of-the-box behavior won't always match your taste—just tell Miloco through OpenClaw (e.g. "don't remind me when the place is messy"), and it remembers your preference and adjusts what it does proactively. Every remark "raises" a Miloco that's tuned to your home, and it knows you better the longer you live with it.

## Prerequisites

- **Hardware**: ≥ 4GB RAM and ≥ 256GB storage recommended, running 24/7. A Mac mini is recommended.
- **Operating System**: macOS / Linux (run under WSL on Windows).
- **OpenClaw** — Miloco runs as a plugin on top of it, so [install it](https://openclaw.ai) first with version ≥ 2026.5.2.
- **Camera source** — Either a Xiaomi account with devices already added to Mi Home, or at least one reachable RTSP / RTSPS camera URL on your LAN.
- **Multimodal large model access** — [Xiaomi MiMo](https://platform.xiaomimimo.com) is still recommended: MiMo-v2.5 for perception, MiMo-v2.5-pro for the Agent (configured in OpenClaw). You may also use an OpenAI-compatible endpoint, such as a local gateway, Ollama-style gateway, vLLM proxy, or other provider proxy.
- **Optional Home Assistant instance** — If you want Miloco to see or control Home Assistant devices, prepare the Home Assistant base URL and a Long-Lived Access Token.

> [!CAUTION]
> **Cost note**: Miloco 2.0's perception and Agent rely primarily on cloud-based large models and will incur ongoing API usage costs—keep an eye on your usage. You can review token consumption on the "Models" page of the home dashboard.

## Install

### Option 1: Install via the Agent (recommended)

Works with both **OpenClaw** and **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — send this instruction to your agent:

```text
Please install the Miloco plugin for me: https://raw.githubusercontent.com/IIIIOvOIIII/lynxloco/main/scripts/install-guide.md
```

### Option 2: One-line command-line install

```bash
curl -LsSf https://github.com/IIIIOvOIIII/lynxloco/releases/latest/download/install.sh | bash
```

Default: OpenClaw. To install for Hermes Agent, specify it explicitly:

```bash
curl -LsSf https://github.com/IIIIOvOIIII/lynxloco/releases/latest/download/install.sh | bash -s -- --agent-platform=hermes
```

### Option 3: Build from source

From the project root, run:

```bash
bash scripts/install.sh --dev   # build from source (scripts/build.sh), then install locally
```

---

### Windows (WSL)

Whichever method you choose above, native Windows is not supported—install and run everything inside [WSL](https://learn.microsoft.com/en-us/windows/wsl/install).

> [!IMPORTANT]
> **Local camera streaming requires extra WSL networking setup.** The dashboard's live "right now at home" view pulls camera streams over the LAN, and WSL's default NAT mode blocks the UDP packets cameras send—so the feed won't load until you enable mirrored networking and allow the Hyper-V firewall.

1. **On Windows** — Add the following to `%USERPROFILE%\.wslconfig` (i.e. `C:\Users\<you>\.wslconfig`; create the file if missing), then run `wsl --shutdown` in PowerShell to restart WSL:

   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

2. **On Windows (elevated PowerShell)** — Allow inbound traffic to WSL:

   ```powershell
   Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
   ```

3. **In WSL** — Once Miloco is installed, verify with `miloco-cli doctor` (it checks both the firewall and WSL networking).

## Quick Start

After installation, restart the OpenClaw gateway so the plugin takes effect:

```bash
openclaw gateway restart
```

Then open the home dashboard to complete the initial setup:

```bash
miloco-cli dashboard   # open the home dashboard in your browser (or visit http://<host>:1810/ directly)
```

### Dashboard login

Miloco protects the web dashboard with local login accounts. On a fresh install,
open the dashboard and create the first administrator. After that, every browser
must sign in before it can see or operate cameras, devices, model settings, Home
Assistant, or logs. Administrators can add, disable, reset, or remove dashboard
users from the **Users** tab; at least one enabled administrator must remain.

The dashboard login is separate from the local service token in `config.json`.
CLI, OpenClaw, Hermes, and other automation continue to use that service token
for machine-to-machine API access. Do not give the service token to a browser or
share it with people; the browser uses its own local session after login.

Get started from the dashboard:

1. **Configure the model** — On the "Models" page, enter your MiMo `api_key`, or add an OpenAI-compatible profile by filling in the Base URL, model name, protocol, and API key if needed.
2. **Add cameras** — Bind your Xiaomi account for Mi Home cameras, or add RTSP cameras from the "RTSP cameras" section on the dashboard.
3. **Enable camera perception** — On the "Overview" page, turn on the switch for cameras you want perceived. Cameras left disabled can still be kept in the list but will not be analyzed.
4. **Optional: connect Home Assistant** — Open the "Home Assistant" page, test the connection, import the entities you want Miloco to see, and enable control only for the devices you trust it to operate.

You can also do this from the command line:

```bash
miloco-cli config set model.omni.api_key sk-xxx   # configure the model key (defaults to MiMo; usually the only thing needed)
miloco-cli account bind                           # bind your Xiaomi account
miloco-cli scope camera enable <did>              # enable perception for a specific camera
```

Once it's running, see the [User Manual](user_guide.md) for how to use Miloco day to day.

## Plain-language Configuration Guide

If you are not sure what to fill in, start with the dashboard. Use the CLI only when you are doing server-side setup or troubleshooting.

### 1. Use an OpenAI-compatible LLM endpoint

Think of this as telling Miloco, "use this model server as your eyes and brain."

1. Open the dashboard and go to **Models**.
2. Add a new Omni profile.
3. Fill in:
   - **Base URL**: the root OpenAI-compatible address, usually ending in `/v1`, for example `http://llm.local:11434/v1`.
   - **Model**: the exact model name returned by that server, for example `grok-4.6` or `qwen3.5:2b-mlx`.
   - **Protocol**: choose **OpenAI Responses** for camera / image perception. Choose **OpenAI Chat Completions** only for text-only compatible providers.
   - **API Key**: paste it if your gateway requires one. Some local gateways allow this to stay blank.
4. Click **Test** first. If it passes, click **Enable**.

Important: "it has a `/v1/responses` endpoint" is not enough. For Miloco perception, the model must actually read images and return non-empty text / JSON. If the dashboard says the visual or structured preflight returned empty output, fix the model gateway or switch to a vision-capable model.

CLI equivalent:

```bash
miloco-cli admin omni test \
  --label local-vlm \
  --model <model-name> \
  --base-url http://llm.local:11434/v1 \
  --api-protocol openai_responses \
  --api-key sk-xxx

miloco-cli admin omni create \
  --label local-vlm \
  --model <model-name> \
  --base-url http://llm.local:11434/v1 \
  --api-protocol openai_responses \
  --api-key sk-xxx \
  --activate
```

### 2. Add an RTSP camera

Think of this as telling Miloco, "this camera is not from Mi Home, but you can still watch this stream."

1. Make sure the camera stream works in a normal player first, such as VLC or FFmpeg.
2. Open the dashboard and find **RTSP cameras**.
3. Click **Add RTSP camera**.
4. Fill in:
   - **Name**: a friendly name, such as `Living Room Camera`.
   - **Room**: where it is, such as `Living Room`.
   - **RTSP URL**: for example `rtsp://camera.local/stream1`.
   - **Username / Password**: only if the camera requires login.
   - **Transport**: start with `tcp`; it is usually more stable on home networks.
   - **Audio**: leave on only if the stream has useful audio and your model path supports it.
5. Click **Test**. If the test passes, save it.
6. The camera is saved disabled first. Turn on its perception switch when you are ready for Miloco to analyze it.

CLI equivalent, using stdin so the password does not land in shell history:

```bash
printf '%s\n' '<camera-password>' | miloco-cli camera rtsp test \
  --uri 'rtsp://camera.local/stream1' \
  --username '<camera-user>' \
  --password-stdin \
  --transport tcp

printf '%s\n' '<camera-password>' | miloco-cli camera rtsp add \
  --name 'Living Room Camera' \
  --room 'Living Room' \
  --uri 'rtsp://camera.local/stream1' \
  --username '<camera-user>' \
  --password-stdin \
  --transport tcp

miloco-cli camera list
miloco-cli camera enable rtsp:<id-from-list>
```

### 3. Connect Home Assistant

Think of this as telling Miloco, "you may also see these Home Assistant devices, and only control the ones I explicitly allow."

1. In Home Assistant, create a **Long-Lived Access Token** from your user profile.
2. Open the Miloco dashboard and go to **Home Assistant**.
3. Fill in the Home Assistant URL, for example `http://homeassistant.local:8123`.
4. Paste the token, then click **Test**.
5. Save the connection and refresh entities.
6. Import only the entities you want Miloco to know about.
7. Keep sensitive devices read-only unless you really want Miloco to operate them. Enable control per entity, not globally.

CLI equivalent:

```bash
printf '%s\n' '<ha-long-lived-token>' | miloco-cli home-assistant test \
  --url http://homeassistant.local:8123 \
  --token-stdin

printf '%s\n' '<ha-long-lived-token>' | miloco-cli home-assistant connect \
  --url http://homeassistant.local:8123 \
  --token-stdin

miloco-cli home-assistant refresh --pretty
miloco-cli home-assistant import light.living_room
miloco-cli home-assistant enable-control light.living_room
```

## Project Structure

```text
miloco-plugin/
├── backend/             # uv workspace
│   ├── miloco/          # main service: perception engine, rules, MIoT gateway
│   └── miot/            # MIoT SDK (standalone subpackage)
├── cli/                 # miloco-cli command-line tool
├── plugins/
│   ├── openclaw/        # OpenClaw plugin (TypeScript)
│   └── skills/          # Agent Skill docs
├── web/                 # home dashboard (React 19 + Vite)
├── scripts/             # build.sh / install.sh / manifest.json
└── knowledge/           # project knowledge base
```

## Further Documentation

- [Backend service](backend/README.md) — FastAPI + perception engine + rules + MIoT gateway
- [Command-line miloco-cli](cli/README.md) — service, device, and config management
- [Home dashboard web](web/README.md) — deployment architecture and local development
- [Full knowledge base](knowledge/README.md) — architecture / modules / features / API quick reference

## Community

Run into issues, want to give feedback, or just chat about use cases? Scan the QR code to join our Feishu user group (the QR code never expires):

<img src="assets/Xiaomi_Miloco_Feishu_Group.png" width="240" alt="Xiaomi Miloco user group" />

## Acknowledgements

Miloco stands on the shoulders of the following open-source projects:

- [OpenClaw](https://openclaw.ai) — AI Agent runtime and plugin platform
- [jMuxer](https://github.com/samirkumardas/jmuxer) (MIT) — real-time video stream muxing for the home dashboard
- [BGE / bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) (BAAI, MIT) — text embedding model
- [Silero VAD](https://github.com/snakers4/silero-vad) (Silero Team, MIT) — voice activity detection, gating the perceived speech field

## License

See [LICENSE.md](LICENSE.md) for the full license terms.

**Important notice**: This project is for non-commercial use only. Without written authorization from Xiaomi Inc., it may not be used to develop applications (apps), web services, or other forms of software.
