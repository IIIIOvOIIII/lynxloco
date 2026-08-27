# RTSP 摄像机与 Responses 支持进度

## 2026-08-28 00:19 SGT

- Current work: 完成上游仓库拉取、源码架构检查、需求澄清和三段式设计确认；正在把批准内容写入正式设计文档。
- Expected result: 形成无占位符、边界明确、可由后续 writing-plans 拆解执行的设计，并只提交文档、不修改实现代码。
- Result: Partial。用户已批准架构、数据流、失败处理、界面、测试和三批交付顺序；设计文档正在写入与自检。
- Next step: 自检设计文档，提交本地 Git，然后请用户审阅已提交设计；获得书面确认后再创建详细实施计划。

## 2026-08-28 00:19 SGT

- Current work: 对正式设计执行占位符、内部一致性、范围和歧义自检。
- Expected result: 修正任何未量化边界或相互矛盾的交付承诺，使设计可直接进入实施计划拆解。
- Result: Achieved。未发现占位符；修正 RTSPS 拼写，固定 Responses 图片总上限为 12，明确首版不发送可选采样字段，并把推送约束改为仅在用户提供或批准可写远端后执行。
- Next step: 运行文档差异检查，提交设计与进度文档到本地 Git，然后等待用户审阅。

## 2026-08-28 00:20 SGT

- Current work: 完成正式设计文档本地 Git 提交并进入用户审阅门禁。
- Expected result: 仓库只包含已批准的设计与进度文档变更，工作区干净，不向无授权上游写入。
- Result: Achieved。设计与进度文档已提交；未修改实现代码，未推送到小米上游。
- Next step: 等待用户审阅正式设计文档；用户明确批准文档后，调用 writing-plans 创建三个独立批次的详细实施计划。

## 2026-08-28 00:32 SGT

- Current work: 用户已书面批准设计；使用 writing-plans 将工作拆成 RTSP 感知基础、RTSP 实时预览、OpenAI Responses Omni 三份可独立实施和回滚的计划，并执行跨计划自检。
- Expected result: 每份计划具备准确文件路径、接口契约、TDD 步骤、逐任务验证命令、提交边界和最终验收门禁；不修改业务代码。
- Result: Achieved。三份计划共 1,033 行、196 个可勾选步骤；未发现 TODO/TBD/FIXME/未决路径，占位符扫描与 `git diff --check` 通过。计划固定了 MIoT DID 兼容、单 RTSP 会话复用、H.265 按观看者转码、Responses 12 图硬上限、空 Key/Bearer 鉴权、协议禁止自动回退及真实 E2E `not_measured` 边界。
- Next step: 本地提交设计状态、三份实施计划与本进度记录；随后由用户选择在当前会话按 subagent-driven-development 执行，或在单独会话按 executing-plans 分批执行。

## 2026-08-28 00:48 SGT

- Current work: 用户选择 subagent-driven-development 并授权完成后部署到 `ai-lab01.esxi` / `ai-lab02.esxi`；已建立隔离 worktree 与 `feature/rtsp-responses-support` 分支，按锁文件安装依赖，完成 RTSP 感知基础计划的执行前一致性扫描。
- Expected result: 获得可复现的干净功能基线、独立任务账本和明确的跨任务接口裁决，再开始 Task 1 的 TDD 实现。
- Result: Partial。Hermes 185 passed/2 skipped，其余前端/插件/脚本基线门禁通过；后端在零业务改动基线上出现 3 个已知 macOS node-monitor 分支失败，以及 1 个 ReID 首次冷加载 600ms 阈值失败。聚焦复跑后 ReID 通过；3 个 node-monitor 失败确认是 Darwin 走 psutil fallback、测试却 patch Linux `parse_smaps` 的既有平台问题。已记录精确基线排除，不修改或放宽无关测试。
- Next step: 提交本次执行启动记录，然后分派 RTSP 感知基础 Task 1；每个任务严格执行 RED→GREEN、实现者自审、独立规格/质量审查和必要修复循环。

## 2026-08-28 04:45 SGT

- Current work: 完成 RTSP 感知基础批次的真实媒体集成测试、实验室 smoke 工具、全量回归与实施边界收口；未运行真实 RTSP 网络测试，未开始实时预览或 Responses 批次。
- Expected result: H.264/H.265 fixture 必须经 `RtspSession -> RtspCameraSource -> CameraDeviceAdapter -> MultimodalCollector` 进入 `DeviceData`，可选音频成为 16 kHz mono `int16` PCM；所有凭据保持去敏，仓库既有门禁缺口和未测量项必须显式记录。
- Result: Achieved with baseline limitations。初始集成 RED 暴露异步 session 尚未 connected 即被 adapter 删除的真实生命周期缺口，修复并独立审查后为 2 passed；后续复审又修正 H.264 视频先到、音频稍后到时测试过早返回的调度竞态。RTSP/摄像机聚焦套件最终 148 passed、CLI 629 passed、`local-ci --tests` 6 项通过（backend 仅保留脚本已知的 3 项 macOS node-monitor/smaps 排除）。无 URL smoke 以 exit 2 fail closed，未产生配置写入且不回显合成凭据。changed-path `ty` 清理后仅余 `perception/service.py:310` 与 `:325` 两条本计划未改动的既有诊断；一次只读全库检查为 946 diagnostics，较早 HEAD 的全库 `task check` 曾为 1025，数量变化来自期间提交和检查范围/版本状态，不作为本批次功能回归。只读 Ruff lint 通过，最终 format baseline 仍有 303 个文件未格式化；3 项 macOS node-monitor、真实 RTSP 网络、启动延迟、断线重连、CPU 和 fps 均明确为 `not_measured` 或既有平台基线。
- Next step: 提交 Task 8 授权文件并执行独立任务审查；随后对 RTSP 感知基础整个提交批次做总审查。批次 2 实时预览、批次 3 Responses、`ai-lab01/02.esxi` 部署与实验室验收继续保持独立后续阶段。

## 2026-08-28 04:45 SGT — 范围事件

- Current work: 记录 Task 8 强制 lint 命令的副作用及恢复结果。
- Expected result: 任何越出 Task 8 授权路径的修改不得进入提交。
- Result: Achieved after recovery。计划给出的 `uv run task lint` 实际展开为 `ruff check --fix .; ruff format .`，曾自动格式化 304 个未授权 tracked 文件；执行者立即停止、未 stage/commit 并上报。controller 随后按当时 HEAD 精确反向恢复全部越界 tracked 改动，仅保留 Task 8 自有测试与 smoke 文件；后续只使用 `ruff check .` 和 `ruff format --check .` 两个只读等价检查，未再次运行写入型 lint。
- Next step: 只 stage 集成测试、smoke 脚本、本进度文档、设计状态和 Task 8 实施报告，提交前复核路径白名单。
