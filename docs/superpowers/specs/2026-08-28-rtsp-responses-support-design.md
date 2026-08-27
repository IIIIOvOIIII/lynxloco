# RTSP 摄像机与 OpenAI Responses 本地 Omni 支持设计

- 状态：已批准；RTSP 感知基础已实施，仓库级既有 type/format 与 macOS node-monitor 基线未清零；RTSP 实时预览与 Responses 尚未实施
- 日期：2026-08-28
- 基线提交：`e900529`
- 仓库：`XiaoMi/xiaomi-miloco`

## 1. 目标

本设计为 Miloco 增加两项可独立交付、独立回滚的能力：

1. 手工添加 RTSP 摄像机，并完成视频感知、可选音频采集、断线重连和家庭面板实时观看闭环。
2. 让 Miloco 的 Omni 感知模型通过 OpenAI Responses 协议调用本地视觉模型。

两项能力只在感知流水线汇合，不共享实现生命周期。RTSP 是新的媒体来源；Responses 是新的模型协议。它们不得合成一个不可拆分的大改动。

## 2. 已确认边界

### 2.1 RTSP 首版包含

- 用户手工添加、编辑、测试、启用、停用和删除 RTSP 源。
- `rtsp://` 与 `rtsps://` 地址。
- H.264 与 H.265 视频进入现有感知流水线。
- 可选音轨解码，继续供现有音频能量门控和 VAD 使用。
- 有界缓冲、断线检测、带抖动的指数退避重连。
- 家庭面板实时观看。
- H.264 优先直接复用；H.265 在有观看者时按需软件转码为 H.264。
- macOS 与 Linux 的跨平台软件功能基线。

### 2.2 Responses 首版包含

- 只替换 Miloco Omni 感知模型调用，不修改 OpenClaw 或 Hermes 的 Agent 推理模型。
- 使用标准 `/responses` 契约。
- 将感知窗口转换为文本与多张图片输入。
- 非流式与流式响应解析。
- 本地服务可无 API Key；配置 Key 时使用 Bearer 鉴权。
- 复用现有熔断、错误分类、用量、trace 和模型档案管理。
- 通过真实图片 preflight 验证目标具有视觉能力。

### 2.3 首版明确不包含

- ONVIF 自动发现。
- PTZ 控制。
- 摄像机录像、事件回放或 NVR 能力。
- WebRTC 或独立媒体网关。
- OpenClaw/Hermes Agent 模型配置。
- 本地语音转写。
- 向 Responses 模型发送摄像机声音。
- 厂商私有 `input_audio` 或原生视频扩展。
- 统一的 H.265 转码 CPU、并发或帧率承诺。
- 商业产品化或对外服务化。

## 3. 许可证边界

上游许可证只授予非商业用途下复制、使用、修改和分发 Xiaomi Miloco 的有限权利，并明确未授权将其用于开发 APP、Web 服务或其他形式的软件。该分叉只能按非商业用途实施和使用。

如果目标变为商业产品、收费服务、对外 Web 服务或其他商业软件，实施前必须取得小米书面授权。技术设计批准不构成许可证授权。

## 4. 当前架构与约束

### 4.1 摄像机路径

当前感知模块只构造一个 `CameraDeviceAdapter`。`MultimodalCollector` 又以 `device_type` 为字典键，因此不能直接并列注册两个都声明为 `camera` 的顶层 adapter；后注册者会覆盖前者。

现有 `CameraDeviceAdapter` 同时承担以下职责：

- 从 MiOT 发现与筛选摄像机。
- 建立 MiOT SDK 视频和音频订阅。
- 将已解码帧写入 `MultiTrackSyncBuffer`。
- 生成统一的 `DeviceData`。
- 维护稳定 DID、多通道后缀和摄像机状态。

规则、身份、作用域、事件和前端均已依赖现有 MIoT DID。为 MIoT DID 增加新前缀会破坏历史数据与持久化键，禁止这样迁移。

### 4.2 模型路径

当前 Omni 内部已经有 provider adapter，但协议选择主要由模型名决定：

- 默认走 OpenAI-compatible `/chat/completions`。
- 模型名包含 `qwen` 时走 Qwen 特化。
- 模型名包含 `gemini` 时走 Gemini 原生协议。

请求和响应最终都被归一化为 OpenAI Chat Completions 形态，后续 JSON 解析、usage、trace 与熔断均依赖该内部形态。新增 Responses 时应扩展 adapter 边界，而不是重写下游。

当前 preflight 只发送文本 `ping`。这不足以证明本地模型能处理图片，必须新增视觉 preflight。

## 5. 总体方案

采用“内部标准接口 + 双适配器”方案：

```text
MiOT SDK --------> MiotCameraSource ----+
                                       |
RTSP URL --------> RtspCameraSource ----+--> CameraDeviceAdapter
                                              |
                                              +--> MultiTrackSyncBuffer
                                              +--> Gate / Identity / Omni
                                              +--> Live stream fan-out

Omni packet --> protocol resolver --> Chat Completions adapter
                                  --> Responses adapter
                                  --> Gemini native adapter
                                              |
                                              +--> normalized {choices, usage}
```

保持单个 `CameraDeviceAdapter` 作为 collector 的唯一 `camera` 入口，在其下组合来源驱动。模型侧增加显式协议字段，并保留只用于旧配置迁移的 legacy resolver。

## 6. 摄像机源架构

### 6.1 `CameraSourceDriver`

新增内部来源接口，至少包含：

- `source_type`：`miot` 或 `rtsp`。
- `discover_devices()`：返回来源可见的摄像机集合。
- `connect_device()`：启动来源会话并注册视频、音频回调。
- `disconnect_device()`：停止来源会话并释放资源。
- `get_state()`：返回连接、编码和错误状态。
- `shutdown()`：清理该来源的所有会话。

该接口只负责来源差异。缓冲窗口、`DeviceData` 打包和下游语义仍由 `CameraDeviceAdapter` 统一负责。

### 6.2 MIoT 兼容

现有 MiOT 发现、作用域、频道与 SDK 回调迁入 `MiotCameraSource`，行为保持不变。

- 单通道 MIoT 摄像机继续使用裸 DID。
- 多通道继续使用 `{did}:ch{n}`。
- 不迁移已有黑名单、拾音白名单、prompt map、事件、规则或身份库键。
- 现有 `/api/miot/...` 路由继续保留。

### 6.3 RTSP 稳定身份

每个 RTSP 源创建一次稳定 ID：`rtsp:<uuid>`。

- 显示名称可重复，ID 不重复。
- 编辑名称、房间、地址或密码不改变 ID。
- 删除再新建同名源会得到新 UUID。
- 删除或停用不删除历史事件；历史事件仍按旧 ID 可读。

### 6.4 配置模型

沿用 `$MILOCO_HOME/config.json`，新增 `camera.rtsp_sources`：

```json
{
  "camera": {
    "rtsp_sources": [
      {
        "id": "rtsp:00000000-0000-0000-0000-000000000000",
        "name": "客厅摄像机",
        "room_name": "客厅",
        "uri": "rtsp://camera.local:554/stream1",
        "username": "camera-user",
        "password": "",
        "transport": "tcp",
        "audio_enabled": true,
        "enabled": false
      }
    ]
  }
}
```

约束如下：

- `uri` 不得包含 userinfo；用户名和密码必须分字段提交。
- 只接受 `rtsp` 与 `rtsps` scheme。
- API 不返回密码，只返回 `has_password`。
- 编辑时密码留空表示沿用该源原密码。
- 日志、trace、诊断包和前端错误不得包含完整 URI、用户名或密码。
- 所有写入复用原子 `tmpfile + os.replace` 路径。
- 本批次将共享配置文件与临时文件的权限收紧到 owner-only，并测试最终文件模式，避免 RTSP 密码及已有 API Key、后端 Token 被同机其他用户读取。

### 6.5 管理 API

新增通用摄像机接口，供前端同时消费 MIoT 与 RTSP：

- `GET /api/cameras`：聚合所有来源，返回统一展示模型。
- `POST /api/cameras/rtsp/test`：对未保存或已保存配置做只读探测。
- `POST /api/cameras/rtsp`：保存新的停用源。
- `PUT /api/cameras/rtsp/{camera_id}`：编辑源。
- `POST /api/cameras/{camera_id}/enable`：通过 preflight 后启用。
- `POST /api/cameras/{camera_id}/disable`：停用并释放资源。
- `DELETE /api/cameras/{camera_id}`：停用、释放并删除配置。
- `WS /api/cameras/{camera_id}/stream`：统一实时观看入口。

现有 MIoT 路由继续工作。前端迁到聚合读取接口后，旧 CLI 与旧客户端仍可使用原路由。

## 7. RTSP 会话与媒体处理

### 7.1 启用前探测

RTSP 源可以离线保存，但启用前必须通过探测。探测在固定总超时内验证：

- 地址解析和 TCP/RTSP 建连。
- 鉴权。
- 至少一个视频轨。
- 视频编码、宽高、帧率与 time base。
- 可选音轨、编码和采样率。
- 能解码至少一张视频帧。

探测不得把凭据写入异常文本。配置错误返回稳定错误码，临时网络错误返回可恢复错误码。

### 7.2 单会话复用

每个启用源最多拥有一个 `RtspSession`。该会话负责：

- PyAV/FFmpeg RTSP 解封装。
- 视频与音频解码。
- 将帧扇出给感知缓冲。
- 将可直播数据扇出给所有观看者。
- 维护 codec、分辨率、连接时间和最后一帧时间。
- 关闭时取消读取任务、清空队列并释放 codec/context。

感知与观看不得各自建立独立 RTSP 连接。

### 7.3 有界缓冲

所有 RTSP 输入和转码输出使用小容量有界队列：

- 正常情况下按时间顺序处理。
- 下游落后时优先丢弃旧帧，保留最新可解码数据。
- 不因模型变慢、浏览器断开或网络抖动无限累积。
- 记录 drop count、queue depth 和最后一次丢弃原因。

### 7.4 重连策略

EOF、连接重置、短时 DNS/网络失败和读超时触发带抖动的指数退避：1、2、4、8、16、32、60 秒，之后保持 60 秒上限。

以下错误停止自动重试，等待用户修改配置或手动重试：

- 认证失败。
- 非 RTSP/RTSPS scheme。
- 没有视频轨。
- 媒体参数无法解码。
- 明确的资源不存在。

单个 RTSP 源失败只改变该源状态，不停止 collector、其他摄像机或后端健康检查。

### 7.5 H.264 与 H.265 观看

- H.264 在浏览器可消费的 profile/level 与封装条件满足时直接复用，避免转码。
- H.264 不满足现有播放器条件时允许走同一个软件转码回退。
- H.265 感知直接使用解码帧。
- H.265 只有在至少一个观看者存在时启动 H.264 软件转码。
- 每个源最多一个转码实例，多名观看者共享输出。
- 最后一名观看者离开后立即停止转码并释放队列。
- 转码失败只影响实时观看，不停止感知。

首版跨平台基线只承诺功能、资源有界和正确释放。CPU、延迟、可持续帧率与并发观看数必须被测量并报告，但在没有指定硬件前不作为统一 veto gate。

## 8. Responses 模型协议

### 8.1 显式协议字段

`OmniModelSettings` 与 profile 增加可持久化协议字段：

- `openai_chat_completions`
- `openai_responses`
- `gemini_native`

旧配置缺少该字段时，运行时仅为迁移目的继续按现有模型名选择协议：Qwen/MiMo 保持 Chat Completions，Gemini 保持原生协议。任何通过新 UI 或 API 保存的档案必须写入显式值。

新代码不得用 Base URL 猜协议，也不得在运行失败后自动切换协议。

### 8.2 内部媒体能力

provider adapter 声明所需媒体形式：

- Chat Completions MiMo/Qwen：现有视频/音频 block。
- Gemini native：现有 inline video/audio。
- Responses：图片序列，不生成 MP4，不发送音频。

这样 prompt 构造器根据协议准备最小必要媒体，而不是先统一编码 MP4 再让 Responses 重新解码。

### 8.3 图片采样

Responses 模式从 gate 通过后的当前窗口均匀选取最多 6 张全景帧，并继续携带最多 6 张现有身份裁切图，总图片数硬上限为 12 张。

- 全景帧数默认复用 `camera.max_cache_images` 的 6 张上限。
- 全景与裁切图总数固定不超过 12；超过时保持全景帧的时间均匀性，并按现有身份查询优先级截断裁切图。
- 尺寸复用现有 Omni 输入短边/裁切策略，不新增一组重复分辨率配置。
- 图片编码为本地服务普遍接受的 JPEG data URL。
- 摄像机音频仍可用于本地能量 gate 和 VAD，但不会进入 Responses 请求。

### 8.4 请求映射

Responses 请求采用标准结构：

```json
{
  "model": "local-vlm",
  "instructions": "<system prompt>",
  "input": [
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": "<user content>"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,..."}
      ]
    }
  ],
  "max_output_tokens": 2048,
  "stream": false
}
```

首版 Responses 请求不发送 `temperature`、`top_p` 或厂商私有采样字段，避免通过猜测服务能力形成隐式分支；这些现有 Omni 参数在 Responses 模式下显示为不适用。首版也不依赖工具调用。

### 8.5 响应归一化

非流式响应遍历 `output[].content[]`，收集 `type=output_text` 的文本。usage 映射为当前内部字段：

- `input_tokens` → `prompt_tokens`
- `output_tokens` → `completion_tokens`
- `total_tokens` → `total_tokens`
- 可用的 cached token → `prompt_tokens_details.cached_tokens`

适配器最终仍返回 `{choices: [{message: {content}}], usage}`，后续事件 JSON 解析无需改变。

流式响应只处理 Responses 标准 SSE 事件：

- 文本 delta 事件产生内容增量。
- completed 事件补齐最终 usage。
- error/failed/incomplete 事件转成现有错误分类。
- 未识别事件忽略并记 debug 计数，不把原始图片或敏感请求体写日志。

OpenAI 官方文档将 Responses 图片输入描述为 `input_image`，并使用 SSE 事件承载流式文本。实现以这些公开契约为基线：

- <https://platform.openai.com/docs/quickstart/make-your-first-api-request>
- <https://platform.openai.com/docs/api-reference/responses-streaming>

### 8.6 鉴权

- `openai_responses` 允许 API Key 为空；为空时不发送 `Authorization`。
- Key 非空时发送 `Authorization: Bearer <key>`。
- 现有云端 Chat Completions 与 Gemini Key 要求不变。
- 编辑 profile 时，只有 Base URL 未变化才允许沿用旧 Key，继续复用现有跨 URL 凭据隔离。

### 8.7 视觉 preflight

Responses preflight 不能只发文本 ping。它必须：

1. 验证 Base URL scheme 与可达性。
2. 可选读取 `/models`；该端点缺失时以真实 `/responses` 调用为准。
3. 发送一张小型内置测试图片和短提示。
4. 要求返回非空短文本。
5. 验证响应结构与 usage 可解析性；usage 缺失可标记 warning，但文本结构错误必须失败。

这能拒绝“有 `/responses` 路由但只支持文本”的伪兼容服务。

## 9. 状态与错误处理

### 9.1 RTSP 状态

统一状态包括：

- `disabled`
- `testing`
- `connecting`
- `online`
- `reconnecting`
- `degraded`
- `config_error`

API 返回稳定错误码和去敏消息。用户可看到下一次重试时间，但看不到底层带凭据 URL。

### 9.2 模型错误

复用现有熔断分类：

- 401/403、非法模型、协议结构不兼容等进入配置型失败。
- 429 尊重 `Retry-After`。
- timeout、connect error 和 5xx 进入可恢复失败。
- Responses 输出无文本或形态错误进入 `bad_response`。

协议失败不得自动回退到 Chat Completions，避免静默改变请求语义或凭据目标。

## 10. 前端与 CLI

### 10.1 摄像机界面

家庭面板摄像机区域增加“添加 RTSP 摄像机”。表单字段：

- 名称
- 房间
- RTSP 地址
- 用户名
- 密码
- 传输方式（TCP/UDP）
- 是否拾音

卡片显示来源、连接状态、视频编码、分辨率及最近错误摘要。操作包括测试、启用、停用、编辑和删除。完整 URI 与密码不回显。

### 10.2 模型界面

模型档案增加协议选择。选择 Responses 时：

- API Key 可留空。
- 显示“图片序列视觉感知”说明。
- 明确提示首版不会把摄像机声音发送给模型。
- “测试连接”执行视觉 preflight。

### 10.3 CLI

CLI 至少支持：

- 列出 RTSP 源及去敏状态。
- 添加/编辑/测试/启用/停用/删除 RTSP 源。
- 配置 Omni profile 的显式协议。

CLI 输出不得打印密码或完整带敏 URI。

## 11. 可观测性

新增指标或状态字段：

- 每源 RTSP 连接状态和重连次数。
- 最后一帧时间。
- 视频/音频 codec。
- 输入队列深度和丢帧数。
- 转码是否运行、观看者数量、转码失败计数。
- Responses 协议名、调用延迟、输入/输出 token、错误分类。

不得记录：

- RTSP 密码。
- 完整带 userinfo URI。
- 模型 API Key。
- 普通日志中的 base64 图片。

## 12. 测试策略

### 12.1 单元测试

- 旧 profile 缺协议字段时的兼容路由。
- 新 profile 必须持久化显式协议。
- RTSP ID 稳定性与 MIoT DID 不变。
- URI scheme、userinfo 和字段校验。
- 密码掩码与留空沿用。
- 原子写入和 owner-only 文件权限。
- 重连错误分类、退避与 60 秒上限。
- 有界队列丢旧保新。
- Responses 请求映射、图片上限、输出与 usage 解析。
- Responses SSE delta、completed、error 和未知事件。
- 空 Key 与 Bearer Key 两种鉴权。

### 12.2 媒体集成测试

使用本地生成的测试媒体和 RTSP fixture 验证：

- H.264 视频感知。
- H.265 视频感知。
- 带音轨与无音轨输入。
- 断流与重连。
- H.264 直播直出或回退。
- H.265 按需软件转码。
- 多观看者共享一个转码实例。
- 最后一名观看者离开后资源释放。
- 单源失败不影响其他源。

### 12.3 Responses 契约测试

严格本地测试服务验证：

- 请求路径必须为 `/responses`。
- 必须发送 `input_image`。
- 非流式与流式返回均可消费。
- 错误状态正确进入熔断分类。
- 无 Key 模式不产生 Authorization 头。
- profile Base URL 改变时不沿用旧 Key。

### 12.4 真实 E2E 边界

用户选择了厂商无关契约，未指定具体本地推理服务。因此：

- 严格 mock/fixture 可证明 Miloco 符合设计契约。
- 它不能证明任意本地 VLM 实现兼容。
- 在至少一个实际本地视觉模型服务通过图片 preflight 与真实感知窗口前，真实本地 VLM E2E 必须报告为 `not_measured`。
- 选择实际服务后，不为该服务加入破坏标准契约的隐式特判；必要差异应成为显式新 adapter。

### 12.5 回归测试

运行仓库现有本地 CI，并重点覆盖：

- MIoT 摄像机发现、作用域、频道和直播。
- 规则与身份识别使用的 DID。
- MiMo、Qwen 和 Gemini 请求/响应。
- 模型档案热更新、preflight、熔断和用量。
- Web 构建、类型检查和相关组件测试。
- 凭据泄漏扫描。

## 13. 验收标准

1. 离线 RTSP 源可保存为 disabled，但不能在未通过探测时启用。
2. H.264 与 H.265 RTSP 均能持续产生现有 `DeviceData`，无需修改下游事件 schema。
3. H.265 在有观看者时可以软件转为 H.264；观看结束后转码实例与队列释放。
4. 临时断网后按退避自动恢复；配置错误不无限重试。
5. 单个摄像机失败不影响其他摄像机、collector 或后端健康。
6. Responses profile 可无 Key 调用，视觉 preflight 能拒绝文本-only 服务。
7. 真实感知窗口能生成标准 Responses 图片请求，输出继续被现有事件解析器消费。
8. usage、延迟和错误进入现有观测链路。
9. 旧配置无需人工迁移即可启动，MIoT DID 和现有模型行为不变。
10. API、日志、trace、诊断包和前端均不泄漏 RTSP 密码或模型 Key。

## 14. 交付顺序

### 批次 1：RTSP 感知基础

- 抽出 `CameraSourceDriver` 与 `MiotCameraSource`。
- 保持 MIoT 回归测试全绿。
- 增加 RTSP 配置、CRUD、探测、共享会话、解码、重连与感知接入。
- 增加通用摄像机聚合模型。

### 批次 2：RTSP 观看闭环

- 通用摄像机直播 WebSocket。
- H.264 直播复用。
- H.265 软件转码。
- 前端 RTSP 管理、状态与直播。
- CLI 与诊断。

### 批次 3：Responses 感知

- 显式协议字段与旧配置兼容解析。
- provider 媒体能力与图片采样。
- Responses 非流式/流式 adapter。
- 视觉 preflight、用量、熔断、trace、UI 与 CLI。

三个批次分别测试和提交，均可独立回滚。批次 3 不依赖批次 1 或 2，可以单独实施。当前 `origin` 是小米上游且没有用户可写 fork；只有在用户提供或批准可写远端后才分别推送，禁止擅自向上游写入。

## 15. 回滚

- RTSP 批次回滚后忽略 `camera.rtsp_sources` 新字段，现有 MIoT 路径继续运行。
- Responses 批次回滚前应先把 active profile 切回 Chat Completions 或 Gemini；代码回滚后旧版本忽略未知协议字段或读取兼容默认。
- 不做历史事件迁移，因此回滚不需要回写数据库。
- 不删除用户 RTSP 配置，重新部署新版本后可以恢复。

## 16. 预计影响范围

主要影响模块：

- `backend/miloco/src/miloco/config/`
- `backend/miloco/src/miloco/perception/collect/`
- `backend/miloco/src/miloco/perception/engine/omni/`
- `backend/miloco/src/miloco/miot/` 中待迁出的 MIoT 摄像机来源逻辑
- 新的通用 camera router/service
- `web/src/components/`、`web/src/api/` 与 i18n
- `cli/src/miloco_cli/commands/`
- 对应 backend、web、CLI 测试与用户文档

不得做无关重构。所有抽象必须直接服务于 RTSP 或 Responses 目标。

## 17. 实施入口门禁（已满足）

以下条件已在 2026-08-28 的用户批准与详细计划自检中满足：

- 用户审阅并批准本设计文档。
- RTSP 与 Responses 继续作为独立批次。
- 接受真实本地 VLM E2E 在未选定实际服务前为 `not_measured`。
- 实施过程中先写失败测试，再改生产代码。
- 不进行生产部署；如未来涉及生产环境，另行走 CO/PAM 与项目部署流程。

## 18. RTSP 感知基础实施结果

截至 2026-08-28，批次 1 的 RTSP 感知基础功能已实施：安全配置与 owner-only 原子写入、MIoT 来源抽取且 DID 保持不变、RTSP 探测、单源单会话解码、有界队列、错误分类与重连、统一 `CameraDeviceAdapter`/`MultimodalCollector` 接入、热更新管理 API 和凭据安全 CLI 均已有自动化契约覆盖。

确定性 H.264（含音频）与 H.265（无音频）PyAV fixture 已通过真实 `RtspSession -> RtspCameraSource -> CameraDeviceAdapter -> MultimodalCollector -> DeviceData` 集成路径；H.264 音频被归一化为 16 kHz、mono、`int16` PCM。相关 RTSP/摄像机聚焦测试最终为 148 passed，CLI 为 629 passed；本地 CI 脚本的 6 项门禁通过，其中 backend 仅按脚本既有规则排除 3 项 macOS `node_monitor`/`smaps` 平台失败。

仓库级既有门禁仍未清零，因此本状态不等于整个仓库质量基线全绿：当前 changed-path `ty` 只剩 `perception/service.py` 两条本计划未改动的既有诊断；一次只读全库 `ty` 检查报告 946 条诊断；只读 `ruff check .` 通过，但最终 `ruff format --check .` 显示 303 个既有文件需要格式化。不得为关闭本批次而扩大范围修复这些存量债务。

未提供 `MILOCO_RTSP_TEST_URL`，真实 RTSP 网络 E2E、真实解码启动时间与断线重连行为均为 `not_measured`；CPU 与可持续 fps 也为 `not_measured`。本批次没有部署到实验室主机，也没有接触生产环境。RTSP 浏览器实时预览/H.265 按观看者转码属于批次 2，当前明确尚未实施；OpenAI Responses Omni 属于批次 3，当前也尚未实施。
