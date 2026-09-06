# 有界跨窗口并发运行时交付报告

更新时间：2026-09-06 15:10 +0800。范围：本地 `feature/model-concurrency` 工作树；未访问生产、读取凭据、暂存、提交或推送。起始 HEAD：`5847873d1ee90978759b92e4c532555d734d29e7`。

## 结果

同一摄像头的多个窗口现在可以同时进入 Omni HTTP 阶段。实际入口 `runner → processor → proxy → InferenceWorker → PerceptionEngine → pipeline → HTTP transport` 在延迟假端点上证明 C=4、C=8 的同相机重叠；Gate 准备峰值为 1；故意反序的响应按窗口顺序提交。没有复制 Identity/ONNX 引擎。

全局 HTTP 限制读取 `get_settings().model.omni.concurrency`，范围 1–8，配置默认值由 settings 子任务负责。HTTP permit 在所有 Omni 传输入口共享，包含非流式、流式及 fused；C=1 保留串行跨窗口调度，C>1 使用 FIFO 覆盖消费。1→8 不要求缓冲变空才切换；8→4 先排空高于新上限的在途请求，再按新上限准入。

源码快照（下文 15 个 runtime 文件的相对路径与内容依次哈希）：`2117b540197313028b5149bf8896e9b200e2a83be9857de34a9763d593f4ab72`。

## 调度、顺序与生命周期

- 等待 HTTP 的队列最多全局 8 个、每摄像头 4 个；runner 的整个窗口生命周期最多 C+8 个，按摄像头轮转取窗。准备信号发出后才取下一份原始窗口，避免在队列中堆积多份原尺寸 BGR。
- 每相机串行准备音频尾段、上下文、Gate、tracker、identity 候选及编码请求；编码完成后释放窗口原始媒体及准备锁，随后等待全局 HTTP 槽位。
- 128 MiB 为编码 payload 预算，按请求 JSON UTF-8 字节的两倍保守计量。响应已经完成但仍在重排/媒体持久化的窗口继续占用预算，完成整个窗口生命周期才释放。
- FusedDispatcher 的 pending 按 job ID 隔离，候选、gallery、回调不会被另一窗口覆盖；HTTP 异常、解析异常、取消及关闭清理各自 pending。
- 响应回写身份、合并 caption/speech/suggestion 状态之前等待同相机前序窗口；前序到截止时间写明确缺口并推进。并发窗口不执行流式早送回调，最终结果在顺序边界只处理一次。
- 每相机帧序在普通、并发及主动查询路径统一预留；1→8→1 不倒退，不因另一相机增加而加快本机 recheck/GC。
- 源状态有独立代际，新状态实例及重连清缓冲均能区分旧任务。模型、协议、端点身份通过无凭据摘要识别；身份变化使旧结果失去实时写回资格。只改变并发档不会使任务失效。
- 身份回调同时绑定原 track state 对象，重连/reset 后重用 track ID 不继承旧结果。模型失败/未回答不当作无人证据。旧帧响应不会拿更新帧的 crop 累积 Tier-C 样本。
- 主动查询和 FPS 热更新先暂停新准入，等待已注册的有界实时任务结束，避免同时操作同一 tracker/identity 实例。停止先撤销 job、取消窗口，再在持久 worker 上关闭引擎，最后结束该 worker；同实例新 loop 重启有回归覆盖。

## 年龄与动作

按主代理本次裁定，使用窗口结束起 **120 秒** 的年龄上限；40 秒级的正常模型结果仍可参与实时动作。没有使用会使现有 Grok 结果几乎全部失效的 4 秒上限。

已实际收到、但已过龄或模型/源身份已失效的结果走历史描述分支，不更新当前人物、caption、语音或建议链状态。规则更新前、持久化 await 后、建议派发与语音派发之间重新确认动作资格；模型在等待中切换后不会继续派发旧动作。已经发生的动作不声称可以撤销。

runner 在 120 秒截止取消的请求记录 `deadline_gap`，**不声称收到了或归档了供应方的迟到响应**。HTTP 返回之后在重排或媒体落盘阶段被取消，只记窗口缺口，保留已成功 HTTP 的结果口径，不伪报为供应方失败。

## 输入与统计修复

- `clear` 现在按 `_windows` 中真正丢失的不同窗口计数，并扣除随后重建保留的当前窗。现场形状从错误的 9 修正为 4。
- 当前窗口已有部分数据被截断时另计 `partial_windows_count`；保留原 `consume_drop_stats()` 四元组，新增独立 consume_partial_stats。
- `drain_ready(mode="fifo")` 提供覆盖消费；默认 latest 和非消费 peek 语义保留。
- 在真实 RTSP 入队函数中构造“一帧视频后跟音频突发”的夹具，复现三槽队列中的视频全部被淘汰。最小修复为满队列时优先淘汰同模态的最旧项，总槽位数不变。未降低分辨率、帧率，也未放大缓冲。
- 截止取消、停止取消以及准备前过龄均记录一次相机窗口缺口 trace，携带已消费的 dropped/partial 增量。准备前拒绝的 HTTP 请求数及错误数为 0。
- 并发路径分别记录 `http_<did>_ms`、`slot_wait_<did>_ms`、`reorder_wait_<did>_ms`；`omni_<did>_ms` 使用实际 HTTP 阶段耗时，不混入前序提交等待。取消 trace 使用原始入队延迟，避免再次加上已经计入 cycle 的处理时间。
- 容量拒绝发生在 HTTP 开始之前时不生成 OmniTrace，不触发供应方熔断。HTTP transport 完成与窗口完成是不同计数；语义正确率不由这两个计数推导。

## 诊断接口

`PerceptionRunner.concurrency_status()` 返回全局 request limiter 快照及 runner 窗口计数；schema 已增加 `RuntimeEngineSummary.concurrency`，service 接线由主代理负责。

快照包含 `limit / active / peak / peak_at_limit / waiting / payload_bytes / submitted / http_started / completed / rejected / jobs / generation / windows`。`submitted` 是获得槽位数，`http_started` 是进入 HTTP 传输段数，`completed` 是释放槽位的生命周期数（包括错误/取消），不是语义成功数。`windows` 区分 admitted、completed、deadline_gap、cancelled_gap、failed_gap、expired_before_prepare。

INFO 日志只在并发上限变化及该上限的新峰值时输出 `[omni-concurrency]` 摘要，包含请求数、等待数和字节数，不记录图片、提示词、响应内容、端点或凭据。降低 C 时 `active` 可短暂高于新值，表示此前已经开始的请求正在排空。

## 改动文件

以下路径均相对 `backend/miloco/src/miloco/perception/`：

- `window_runtime.py`：全局公平 HTTP 限制、窗口生命周期、跨 loop 完成标记、时钟/预算/摘要。
- `runner.py`：按相机 FIFO 调度、切档、截止/停止处理及计数。
- `processor.py`：传入已收集窗口、准确请求阶段统计及所有消费缺口 trace。
- `client.py`：并发准入和生命周期互斥、媒体释放、按序最终动作、动作资格再检查及有界持久化。
- `schema.py`：partial、源代际、释放后帧数及 runtime concurrency 摘要字段。
- `collect/stream_buffer.py`：clear 去重计数、partial 计数、FIFO 及代际。
- `collect/collector.py`、`collect/camera_adapter.py`：FIFO 透传、partial/源代际打包。
- `collect/rtsp_session.py`：同模态优先淘汰，保留混合流中的视频。
- `engine/api.py`：串行准备/有序提交边界、相机帧序、源 reset、历史结果分支。
- `engine/pipeline.py`：原始媒体释放、相机帧序透传、请求版本、顺序边界。
- `engine/identity/dispatcher.py`、`engine/identity/engine.py`：pending 隔离、旧状态和失败证据保护。
- `engine/omni/omni.py`、`engine/omni/omni_client.py`：真正 HTTP 槽位、fused 顺序回写、取消清理、局部拒绝不影响供应方熔断。

测试改动：

- `backend/miloco/tests/perception/test_window_concurrency.py`：新增运行时集成与失败/顺序/资源测试。
- `backend/miloco/tests/perception/test_stream_buffer_overflow.py`：精确 4 窗、FIFO、部分截断。
- `backend/miloco/tests/perception/collect/test_rtsp_session.py`：音频突发保留视频。
- `backend/miloco/tests/perception/test_apply_config_restart.py`：旧 `__new__` fixture 初始化新增 runner 队列字段。

`service.py`、observability、settings、admin、web、deploy 为其他代理改动，不属于本子任务的写入。

## 验证

已观察到的 RED 包括：clear 实际返回 9；FIFO 参数不存在；同相机 Gate 同时进入 4/8 个；runner 未创建窗口 job；音频突发后无视频；重排永不推进；相机帧序受其他相机累计；1→8→1 帧序回退；持久化后模型切换仍发送建议；取消窗口无 trace；HTTP 完成后的取消被误记 HTTP 错误；持续 ready 时 1→8 不退出旧循环。相关修复后均已 GREEN。

执行目录为 canonical 工作树的 `backend`。Python：`/Users/nicholasliao/clawd/xiaomi-miloco/backend/.venv/bin/python`。环境 `PYTHONPATH=miloco/src:miot/src`；`MILOCO_SERVER__TOKEN=miloco-local-test` 仅为本地测试的合成值，不是生产凭据。

1. `python -m pytest miloco/tests/perception miloco/tests/test_perception_client.py -q --tb=short`：**1695 passed**，24.15 秒，4 条已有依赖/废弃 API 警告。此组在最后三处父审查补丁之前执行。
2. 最后补丁后：`python -m pytest miloco/tests/perception/test_window_concurrency.py miloco/tests/perception/test_apply_config_restart.py miloco/tests/perception/test_latency_rtf.py miloco/tests/perception/test_inference_worker.py miloco/tests/perception/test_stream_buffer_overflow.py miloco/tests/perception/collect/test_stream_buffer_retention.py -q --tb=short`：**84 passed**，1.35 秒。
3. 新增准备前过龄会计断言后：`python -m pytest miloco/tests/perception/test_window_concurrency.py -q --tb=short`：**31 passed**，1.30 秒。
4. 运行时 15 个文件及修改测试的 scoped `ruff check`：通过；`git diff --check`：通过。

主代理继续执行完整后端、前端构建及发布验收；本子任务没有宣称本地 fixture 已验证生产模型吞吐、真实事件召回或实际供应方账单。

## 限制

- 真实模型端点是否能在 C=8 提高语义有效吞吐，仍须按主代理冻结的生产验收条件测量。
- RTSP 混合队列缺陷有源码和夹具证明；尚未证明它独自解释生产所有零/单帧窗口。原有视频保留字节上限仍生效，完整 decoded→HTTP 帧链需现场对齐。
- 编码预算针对等待、在途、重排及窗口持久化持有的请求；单个窗口的准备阶段有编码/序列化瞬时开销，未声称主机 RSS 等于该预算。
- 并发前序未完成时，窗口使用准备时已经提交的身份/语音上下文；不会预测未来响应。旧帧不复用较新帧的 Tier-C crop，因此库样本积累频率需生产观察。
- 已开始的供应方请求在本地取消后，不能证明供应方已经停止推理或计费。
