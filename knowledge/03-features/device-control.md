# 设备控制

## 背景与目标

用户想让 AI 帮自己控制家里的灯、空调、风扇。传统方式需要打开米家 App 找到设备再操作；Miloco 让 Agent 直接理解用户意图并执行。

设备控制模块提供完整的米家设备操作能力：单属性写入、批量属性写入、动作调用、属性查询、场景执行，覆盖用户在 Agent 对话中所有可能的设备操作需求。

---

## 产品面

### 能做什么

- **属性控制**：设置设备的任意可写属性（亮度、色温、温度、开关、模式）；同一设备多属性可合并为一次请求
- **动作调用**：触发设备支持的动作（如音箱播报 TTS、扫地机开始清扫、空气净化器启动自动清洁）
- **属性查询**：读取设备当前状态，用于 Agent 回答"客厅灯现在是多少亮度"
- **场景执行**：一键触发米家配置的智能场景（多设备联动预设），如"回家模式""睡眠模式"
- **Scope 管理**：配置 Miloco 管控哪些家庭和摄像头，是设备接入的前置配置

### 典型场景

**场景 1 — 对话控制**：对 Agent 说"把客厅的灯调到 60% 亮度"。Agent 选择 `miloco-devices` Skill，通过 CLI 调 `/api/miot/devices/{did}/control`，后端执行属性写入，用户约 1 秒内看到灯光变化。

**场景 2 — 规则自动化**：感知流水线检测到"有人进入书房"，STATIC 规则触发，`RuleRunner` 直接调 `MiotProxy.set_device_properties` 打开书房台灯，无需 Agent 介入，无 LLM 额外调用。

**场景 3 — 家庭面板操作**：用户在浏览器打开家庭面板"设备"标签，按房间浏览设备列表，点击开关或滑块直接发起控制请求。

**场景 4 — 音箱 TTS**：Agent 需要向用户播报提醒，通过 `miloco-devices` Skill 找到房间内的音箱设备，调用 `play-text` 动作完成播报。

### 能力边界

- 仅操作已绑定小米账号、且被纳入启用家庭（scope）的设备，其余请求一律拒绝（返回 scope 校验错误）
- 设备联网状态由小米云管理，Server 不感知"控制是否真正送达硬件"——返回成功仅表示指令已发出
- 场景执行由小米云侧完成，Server 只负责转发
- 不支持自定义协议或非米家生态设备
- MiOT OAuth 未绑定时相关端点抛 `MiotOAuthException`

---

## 研发面

### 架构概览（数据流图）

```
CLI / Agent（miloco-devices Skill）
  → POST /api/miot/devices/{did}/control
  → MiotService（scope 校验 + 类型分发）（miot/service.py）
  → MiotProxy（miot/client.py）
  → MIoTClient.set_props_async / get_props_async / action_async
      ├─ 本地可控且未冷却 → 本地中枢网关（mTLS MQTT + MIPS RPC）→ 设备
      └─ 否则 / 本地失败可重发 → MIoTHttpClient（backend/miot/src/miot/cloud.py）
                                  → 小米云 HTTP API → 设备
```

属性查询入口为 `MiotService.get_device_status`，场景触发为 `trigger_scene` → `MiotProxy.execute_miot_scene`，链路结构相同。控制写入 / 查询 / 动作调用优先走局域网本地中枢，云端为兜底（见下「控制优先走本地中枢，云端兜底」）。

规则触发的 STATIC 控制路径较短：`RuleRunner`（`rule/runner.py`）直接调 `MiotProxy`，绕过 `MiotService`（规则在创建时已绑定 scope 内设备，设计上已保证安全）。

### 核心模块

**MiotService**（`miot/service.py`）

业务编排层，主要职责：

- **scope 校验**：检查请求 did 所属家庭是否在启用集内（KV 存储 home 白名单）
- **控制类型分发**：将 `set_property` / `set_properties` / `call_action` 请求转换为对应 MiOT 参数类型
- **LRU 设备目录维护**：记录用户操作过的设备及属性，确保其出现在 Agent 设备目录（catalog）中
- **home / camera scope 管理**：`switch_home` / `toggle_camera` 写 KV 后触发后台刷新并同步感知层 adapter
- **OAuth 校验**：未绑定或 token 过期时抛 `MiotOAuthException`

**MiotProxy**（`miot/client.py`）

Server 代理层，主要职责：

- **token 生命周期**：后台自动刷新，失效时清空 OAuth 缓存；token 通过 `KVRepo` 持久化，重启后自动恢复
- **数据缓存**：设备/摄像头/场景列表内存维护，重启时重新拉取
- **实时事件订阅**：注册 MIPS 云 MQTT 回调（设备改名 / 换房换家、家庭场景变更、摄像头云端上线 / 离线），经 `mips_listeners.py` 的防抖监听器刷新对应缓存，使本地缓存无需轮询即与云端收敛；设备绑定 / 移入受管家庭触发的设备欢迎见 [device-welcome.md](device-welcome.md)
- **device spec 按需缓存**：首次使用时加载并缓存，避免启动时全量拉取拖慢启动
- **摄像头 manager 管理**：维护每个摄像头的 `CameraVisionHandler`（`miot/camera_handler.py`）实例

**MIoTClient**（`backend/miot/src/miot/client.py`）

MiOT SDK 顶层客户端，聚合 Cloud、LAN、mDNS、MQTT、摄像头等子模块，对 MiotProxy 暴露统一异步接口。详见 [sdk-miot.md](../05-external-deps/sdk-miot.md)。

### Scope 机制

Scope 定义了"Miloco 管控哪些设备"的边界，分为两个维度：

**家庭维度（Home Scope）**：用户的小米账号下可能有多个家庭（如"公寓""父母家"），Miloco 同一时刻只管控一个家庭的设备。启用的家庭白名单持久化在 `miloco.db::kv` 表中，由 `filter.py`（`miot/filter.py`）读取后应用于过滤。

**摄像头维度（Camera Scope）**：在启用家庭内，用户可以进一步禁用某些摄像头（如不想让 Miloco 看客厅）。被禁用的摄像头 DID 以黑名单形式存在 KV 表中——新摄像头默认被感知，用户选择性关闭。同时启用的摄像头数量有上限（4 台，`filter.py::MAX_ENABLED_CAMERAS`，经状态接口下发前端作为唯一来源），主动启用超限或启用离线摄像头会被 `toggle_camera` 拒绝。

**Scope 过滤的作用点**：

- 设备列表 / 场景列表接口返回前，`filter.py` 过滤掉不在启用家庭的条目
- 控制设备前，`MiotService` 校验 did 所属家庭是否在启用集内，不在则拒绝
- 摄像头流水线层：摄像头拉流（native PPCS 会话 + 解码）与感知投喂**共用同一选择口径** `select_active_camera_dids`（`miot/filter.py`）——在启用家庭内、未拉黑、在线、且按 did 截断到启用上限，拉流集即投喂集不漂移。scope 变更后 `MiotService` 先 `refresh_cameras` 按新口径建 / 销 camera manager（关闭 / 移出家庭 / 离线 / 超额的摄像头会停掉 native 会话与解码），再同步感知 adapter 的投喂订阅，无需重启服务

**Scope 变更**：切换家庭时按「先加目标家、再移其余家」的顺序写 KV，保证切换过程中启用集不瞬时空掉（空集会触发兜底自动选家、扰动感知）；落库后再通知感知层 adapter 同步，无需重启服务。账号切换时清空所有家庭与摄像头 scope，回到干净状态。若启用集为空或无效，自动回退到首个可见家庭，避免感知全黑。

**切换家庭时重置 Agent 会话**：切换真正改变启用集时，`switch_home` 会后台 best-effort 触发一次 openclaw 侧 miloco agent 会话的重置（`agent_client.reset_agent_sessions` → 插件 `reset_sessions` webhook，见 [Agent 集成](openclaw-integration.md)），清掉旧家庭遗留在会话里的上下文（设备 / 房间 / 习惯），避免旧家庭上下文串入新家庭造成干扰。空切（重复选中当前已是唯一启用的家庭）跳过重置，以免白删仍有效的热上下文；`list_homes` 启用集为空时的兜底自动选家属同一 bug class，同样触发重置。整个重置纯后台 fire-and-forget，openclaw 不可达只 WARN、绝不阻塞或打断切换本身。待重置的会话集以 `MILOCO_SESSION_KEYS`（`dispatch/dispatcher.py`，由 `_ROUTE` 派生的唯一事实源）为准。

**在哪配置**：web 面板"概览"标签（摄像头在用切换）和顶部 TopBar 家庭切换器；也可通过 `miloco-miot-scope` Skill 在 CLI 完成。

### 关键设计决策

#### 控制优先走本地中枢，云端兜底

设备属性写入 / 查询 / 动作调用先尝试局域网**本地中枢网关**（mDNS 发现 + mTLS MQTT + MIPS TLV RPC），失败按「这条指令有没有可能已经在设备上执行了」分流，而不是简单地「本地失败就重发云端」：

| 本地侧发生了什么                                                                  | 是否重发云端 | 为什么                                                                       |
| --------------------------------------------------------------------------------- | ------------ | ---------------------------------------------------------------------------- |
| 设备不在活网关后面 / 离线 / 无 spec 访问                                          | 是           | 本地一个字节都没发出去，重发安全                                             |
| 该设备（或其所属网关）正在冷却窗口内                                              | 是           | 刚超时过，跳过本地省掉又一次 5s 等待                                         |
| 本地调用抛异常，或回包里连 `code` 都没有                                          | 是           | 请求明确没落到设备上                                                         |
| 本地 RPC **超时**（`-10006`）— 写属性 / 调动作                                    | **否**       | 指令可能已执行，云端重发会执行两次                                           |
| 本地 RPC 超时 — 读属性                                                            | 是           | 读是幂等的，重读无害                                                         |
| 网关已回包但不可用（`-10041` 结构不认识 / `-10040` 非合法 JSON）— 写属性 / 调动作 | **否**       | 同样是「结果未知」：能落到这两个码的前提就是请求已到过网关，指令很可能已执行 |
| 网关已回包但不可用（`-10041`/`-10040`）— 读属性                                   | 是           | 读幂等，重读拿确定值更有用                                                   |
| 本地成功                                                                          | 否           | 已拿到确定结果                                                               |
| 设备明确拒绝（带码错误）— 写属性 / 调动作                                         | 否           | 已拿到确定结果，重发只会再被拒一次                                           |
| 设备明确拒绝（带码错误）— 读属性                                                  | 是           | 读幂等，云端兜底可能拿到值（实现是「任何非 OK 码都兜云端」）                 |

**超时**会冷却该设备 30s；当同一网关后有 2 个不同设备在窗口内超时，则判定网关整体卡死并冷却整台网关，避免批量控制逐个各付一次 5s 超时。`-10041`/`-10040` 不冷却——它们是快速返回，没有超时代价可省，冷却只会白白把设备赶去云端。

注意 `-10004` 不属于上表任何一行：它现在只表示「本地与云端都没给出这一条的结果」（云端批次回填不到），与本地路由无关。「结果未知」的本地码是 `-10006`/`-10041`/`-10040`，凡是这三个，台账文案都必须传达「指令可能已生效」，否则 agent 会当成失败去重试，把非幂等指令做两遍。

路由实现在 SDK 内：`MIoTClient.set_props_async` / `get_props_async` / `action_async`（`backend/miot/src/miot/client.py`），业务层 `MiotProxy` 只是一行转调。总开关 `miot.central_hub_enabled`（关掉整条退回纯云端），mDNS 发现不到时可用 `miot.central_hub_gateways` 手工配网关地址。云端 `MIoTHttpClient`（`backend/miot/src/miot/cloud.py`）退居兜底。

**动作（action）的返回体在 SDK 出口统一成一种形状**。同一个 action 走不同通道时，上游给的信封**整体**不同——本地中枢（MIPS `proxy/rpcReq`）是 `{"code": 0, "out": [{"piid": 7, "value": "<json>"}]}`，云端（MiHome `/app/v2/miotspec/action`）是 `{"did": …, "miid": 0, "code": 0, "out": ["<json>"], "exe_time": 19, "net_cost": 213, …}`，且**失败时连 `out` 键都没有**。差异不止 `out` 一个字段。

调用方拿到哪种形状取决于**路由决策**，而路由会因超时冷却、网关掉线在同一台设备上来回翻转——所以这不是"初始化时分支一次"的差异，而是一个会当着调用者面切换的形状。两条通道各自在自己的出口归一到 `backend/miot/src/miot/action_result.py` 定义的正典形状：`did` / `siid` / `aiid` / `code` / `source` / `out` / `exe_time` / `latency` / `net_cost`。三条约定：

- `out` 一律是 `[{"piid", "value"}]`。云端把 piid 拍平了，`MIoTClient` 随后按设备 spec 里该 action 声明的 `out` 列表**按位补回来**——只统一形状而留空 piid，下游照样得按"这条走了哪条通道"写两套解析，那这个兼容就等于没做。补全有两个克制：走 **只读缓存**的 spec 取法（`MIoTSpecParser.get_cached_async`，未命中即放弃），绝不把 `parse_async` 拖进控制热路径（未命中会去 miot-spec.org 拉数百个请求）；且**只在声明个数与返回个数对得上时补**，对不上说明 spec 与上游理解不一致，宁可留 `None` 也不赌顺序。归一按**元素形态**判定，`source` 只用来判断能力差异，**不用来判断形状**。
- `latency` 是 SDK 自己量的端到端往返，两条通道同源才可比；**`net_cost = latency - exe_time` 由 SDK 自算，不取上游的**——上游那个只覆盖"云端→设备"一段，与本地中枢的局域网往返不是同一条路径，直接用会得到假对等。
- `exe_time` 本地也**有**：网关把它放在回包**顶层、`result` 的兄弟位置**，只读 `result_obj.get("result")` 会连同一起丢掉。

`MIoTClient` 内的 LAN（`backend/miot/src/miot/lan.py`）子模块仍只做局域网设备发现与在线状态维护，摄像头实时画面走 PPCS 串流（见 [live-camera-view](live-camera-view.md)），二者都不承载控制写入。SDK 各路径能力见 [sdk-miot.md](../05-external-deps/sdk-miot.md)。

#### 属性推送同样选路，但按「每台设备只有一个源」

控制是**每次调用**选一次路，推送是**每台设备**选一条通道长期挂着。2026-10 之前属性推送只有云端一条（`device/{did}/up/properties_changed/#`）；本地中枢那条接口早就写好但没接线，网关的设备清单里那个 `pushAvailable` 标志也就没人用。

现在按设备选：`can_push(did)` 为真走本地中枢，否则走云端。**注意它和控制用的 `can_control` 不是同一个谓词**——两个只差最后一位，`can_push` 看 `push_available`、`can_control` 看 `specv2_access`，**两套标志相互独立**。所以完全会出现「控制走云端、推送走本地」的同一台设备。这不是笔误，改动这里时别把两个谓词"顺手"合并。

**为什么是单源互斥，而不是两条都订再合并去重**：本地往返比云端短（实测同一台设备 90ms vs 164ms），两条都订时同一条属性若在两次变化间隔小于这个差值，云端那条**旧值**会盖在本地新值之后；而两边都不带设备时间戳，容器无从仲裁。去重只去得掉「同值」，去不掉乱序——乱序的两个值确实不同。用互斥规避重复，比合并去重简单，也少一份解码开销。

**换源次序是「先订新、再退旧」。** 重叠窗口里同一条变更会被投两次，值相同，容器 `state_store.set` 的判等（`_same`）后零事件、零规则重算；反过来先退后订，窗口里落地的变更两条通道都收不到，容器就停在旧值——**静默丢比重复难查得多**。业务层那条对账路径（`miloco/miot/client.py` 的 `_reconcile_subscriptions`）也是这个次序，别改成「退旧订新」。

**搬运有两个触发点，缺一不可。** 一个是中枢**设备表变化**——网关连上 / 断开、设备上下线、`pushAvailable` 翻转，这三件事都会改 `can_push`，但**都不改「订了哪些 did」**，所以业务层那条按 did 的对账会算出空差集直接早退、搬不动通道。搬运因此做在 SDK 里，由 `CentralHubManager` 的设备表回调触发（这个槽位由 SDK 占住，`MiotProxy` 通过 `register_dev_list_changed_callback` 注册成它的下游，只收清单真增删）。

⚠️ 但设备表变化**结构上看不见「本地订阅没了」这件事**，所以还有第二个触发点：**中枢换了一个新的 `MipsLocalClient` 实例**。本地订阅挂在那实例的订阅表（`_broadcasts`）上，实例一换就全没了，而**设备表可能一字未变**——网关 MQTT 抖一下把 `_connected` 置假，20s 重连 sweep 直接把实例 deinit 掉重建，新实例回拉设备清单算不出任何差异（`added`/`removed`/`flags_changed` 全假）。没有这个信号，镜像会一直停在「已订在本地」而实际一条订阅都没有，那些设备的推送**静默停掉**，业务层的按 did 对账也救不了（did 还在镜像里，`sub_device_props_async` 直接早退）。所以协调器另有一个 `on_local_transport_reset` 槽位，在**新实例连上之后**发一次（排在回拉设备表之后，`can_push` 才判得准）；SDK 收到就作废镜像里标本地的条目再跑一次搬运。

这也顺带解释了下面那个 `None` 哨兵：**teardown 与「实例被换」是同一件事的两种形态**，都让本地订阅凭空消失，所以都走「作废本地条目」这一条路。

**能力标志翻转也要触发**：设备还在表里时 upsert 是就地覆盖，`added`/`removed` 都是空——只看这两个列表的话，「设备上线」「`push_available` 翻转」都通知不到谁。所以 `__refresh_dev_list` 单独记一个 `flags_changed`，并在回调里由 SDK 分流：**来源搬运无条件做**（要的就是每一次翻转），**业务层只在清单增删时收到通知**（它的处理是重拉设备清单，一趟云端往返，标志翻转不改成员、不需要重拉）。

**镜像记的是「实际订在哪条通道」，不是「打算订在哪条」。** 本地那批的订阅寿命挂在 client 实例上，所以实例一换、或 hub 被拆掉重建（`teardown_central_hub_async` → `setup_central_hub_async`，走「换号失败但旧账号 token 仍有效」那条恢复路径），镜像里标本地的条目就要**作废**——抹掉后取值为 `None`，含义是「有意图但一条都没建立」，与「确实订在云端」区分开；切源逻辑据此重新注册，并且不会去退一条根本不存在的云端订阅（默认成云端就会）。

⚠️ 反过来，**mips 重建时不能把本地那批一起抹掉**。本地订阅不挂在 mips 上，mips 重建不会让它们消失；整片抹镜像会让重放把它当「从零重订」，此刻若网关不可用（`can_push` 假）就改判到云端，而「它原来在本地」这个事实已随镜像消失——那条活着的本地订阅既没人退也没人记得，网关回来 `_on_connect` 把 `_broadcasts` 原样重放，就是同一台设备两条通道同时投。所以重放只覆盖**非本地**那批（含"没建立"的 `None`），本地那批带着镜像留着。

⚠️ 退订的判据**不能带 `is_connected`**（这与选源/订阅那条相反）。paho 短暂断连（6~60s 退避自动重连）不销毁实例，`_broadcasts` 里的注册还在，重连时 `_on_connect` 会原样重放——按连接状态挡下退订，就变成「镜像已抹掉它、注册却活着」，而且重连后 `is_connected` 转真、重连扫描的早退分支让这个实例活到进程退出，换实例前不自愈。所以退订要「先摘表、再尽力撤订阅」，与云端那条（`mips_cloud._unsubscribe_async`）同口径：撤不掉没关系，表摘掉了就不会重放。

⚠️ **判 move 不能只看通道名。** 多网关家庭里设备的归属网关会变（mesh 换代理、重新配对）：它从 A 的清单消失、出现在 B 的清单里，**通道还是本地**。中枢删表循环本来就带 `group_id` 守卫（只删仍属于本网关的条目），说明代码预期了这种改嫁——但推送这层得跟上：注册挂在**旧实例**的订阅表上，设备表已指向新的，`old == want` 差集为空、这条设备推送静默停掉且没有自愈入口（旧实例一直连着，transport reset 不会发生）。所以搬运要按「注册**实际**落在哪几台」再判一次 move，而这个事实现问实例即可（`props_sub_groups` 扫各实例的注册表，答全集而非第一台命中的——只看一台的话，残留那台恰好排在前面就把「不一致」误判成「一致」），不必再记一份镜像。

⚠️ 退订同理要按**注册所在实例**路由，而不是设备表。设备改嫁后按表找会扑空，旧实例上的注册清不掉、每次重连还被 `_on_connect` 原样重放，设备回到 A 覆盖范围内就是双通道同投。所以退订要逐个实例问、有就摘（`unsub_device_props` 不带 `keep` 时就是全摘）；而「本地→本地」的搬运要**保留刚订上的新实例**（带 `keep=<当前归属>`），摘掉其余全部——旧实例那条、以及同一台设备身上更早的残渣。保留是必须的：不给定就会把新注册一起摘掉，「先订新再退旧」立刻退化成两条都没有；而只摘「旧的那一台」又漏残渣，它们都会在重连时被原样重放。

⚠️ 还有一处**修复入口要补全**：`None` 哨兵（有意图、一条源都没建立）唯一的修复入口是中枢事件，而「换号失败但旧账号 token 仍有效」那条恢复路径只重建 hub、不跑 mips 重放。若此后没有任何网关连上（LNP 未授权 / 网关离线 / 不在网关网段），那批 did 会一直停在一条源都没有——控制照常走云端、日志里什么都没有，只有重启后端能恢复。所以 `_setup_central_hub_async` 末尾要补一次搬运（此刻 `can_push` 为假就订回云端）。

**换源是两阶段、阶段内并发**：一次网关断开会让整批设备同时换源，逐个 `await` 的话 N 次 SUBACK 串起来能把设备表回调阻塞到秒级（那个回调还等着通知业务层）。所以「所有订新」并发跑完，才开「所有退旧」——阶段边界是语义要的，不能合并成一批。在飞订阅数沿用量级同一个上限，压力落在同一条 broker 连接上。

⚠️ **业务侧那把锁只罩镜像、不罩网络；搬运那一侧正相反。** 业务侧的订/退与搬运共用 `_props_source_lock`，为的是让「读意图集 → 改镜像」这一段与搬运互斥（不然在飞订阅返回时那句无条件的镜像写回会把业务刚记下的来源改谎，两条通道同时活着、退订只退得掉一条）。业务侧的**网络阶段必须在锁外**：一次云端 SUBACK 最长等 10s，上层 `_reconcile_subscriptions` 是按 `_RECONCILE_CONCURRENCY`（16）并发对账的，跨 I/O 持锁会把 16 路串成 1 路（首次登录全量订属性从 `ceil(N/16)×RTT` 变成 `N×RTT`）。这条与本仓业务层同款，见 `miloco/miot/client.py::_reconcile_subscriptions` 的 docstring。

⚠️ 而**搬运那一侧整批两阶段连同网络都在锁内**，业务侧的订/退排在它后面 —— 只有这样那句镜像写回才不会与业务提交交错。所以这是「锁不跨网络 I/O」原则在这里的**例外**，代价被限在「一次网关断开才会整批换源、`_REPLAY_CONCURRENCY` 并发」。两侧共付的代价都只是「算来源」与「落地」之间能力可能翻转过，所以提交时按当下判据复查一次，不一致就补跑搬运。别照着业务侧那半句去「修正」搬运：把它的网络段挪到锁外就立刻退回本段开头那个 bug。

⚠️ **本地那条订阅要等网关 SUBACK 才算订上**（云端一直如此，本地原先不是）。向网关发 `master/appMsg/notify/iot/{did}/property/#` 才是「让它开始转发这台设备属性」的动作，而 `mqtt.subscribe()` 只是把请求排出去——连接恰在此刻断掉、或网关在 SUBACK 里拒绝，都只有后续回包才知道。把「发出去了」当成功，上层会记「推送走本地」并**把还在工作的云端订阅退掉**，而这台本地一条订阅都没有：推送静默丢失，镜像还判「已订在本地」，幂等早退让重试也进不来。所以**属性**这批在 `_broadcasts` 里只放**已确认或正在确认**的注册（在飞那条同时挂在 `_props_confirms` 上：确认失败的立刻摘键；并发订阅的后来者不乐观返回成功，而是**共享首订的结局**），失败/超时/拒绝/取消一律回滚并抛。在飞窗口里 `has_props_sub` 会短暂答「在」——这是有意的（免得并发重订），真正的不变量是「**确认失败后不留键**」（`has_props_sub` 据此答「注册落在哪台」，撒谎就会连累搬运判定），失败/超时/拒绝/取消一律回滚并抛；事件推送那条（`sub_event`，未接线）走的仍是「请求已发出」，而连接时的重放是 fire-and-forget —— 它的 SUBACK 被拒时按同一条判据摘键并告警，不保证「只放已确认」。网关**会**回 SUBACK（这也是能这么改的前提：等确认不会把现在能用的路径变成超时失败），所以超时是一道活性闸门而非紧张预算。

**两条通道的推送形状不同，在各自出口归一**：本地 topic 用**点号**（`appMsg/notify/iot/{did}/property/{siid}.{piid}`）、载荷是单条 `{did,siid,piid,value}`；云端用**斜杠**（`.../properties_changed/{siid}/{piid}`）、`params` 可以是数组、一条消息带多条变更。两边都归一到 `MIoTDevicePropsEvent`，下游 `IotPushWriter` 因此不必知道这条推送来自哪条通道。

⚠️ **另一种本地载荷形状暂不支持**：网关还会发 `{"did","type":"property","name":"ota_state","value":"idle"}`——用 `name` 而不是 `siid`/`piid`。2026-10 实测它只承载 OTA 遥测，而 spec 属性（含传感器占位状态）走的仍是 `siid/piid`，所以解码器按白名单丢掉它、打 INFO `mips_local unknown prop msg`（不静默）。支持它要先证明 `name` → iid 的映射稳定（spec 的 `type_name` 用**连字符**、观测到的 `name` 用**下划线**，不是直接查表能对上的），而映射错了会**写错叶子**——比丢掉更糟，它会把一个真实属性的值静默改掉。所以先不支持，留着 INFO 日志观察。

**这条改动能自失效**：`pushAvailable` 是网关自己报的，网关不报或报假就全部留在云端，等于没做这个改动；`miot.central_hub_enabled` 关掉同理。

**为什么 STATIC 规则绕过 MiotService**：规则在创建时已绑定 scope 内设备，MiotService 的 scope 校验是冗余的。STATIC 规则执行路径需要极低延迟（感知到设备响应），省掉 service 层的开销。

**Scope 为什么用 KV 而非配置文件**：Scope 是运行期可变的用户选择，不是静态配置。KV 表提供事务性单行原子写，读路径走内存缓存，变更即生效，与配置文件的"重启才生效"语义不同。

### 如果我要修改设备控制相关功能

| 修改目标              | 去看哪个文件                                                                     |
| --------------------- | -------------------------------------------------------------------------------- |
| 修改 scope 过滤逻辑   | `miot/filter.py`                                                                 |
| 修改 scope CRUD 逻辑  | `miot/service.py`（`switch_home` / `toggle_camera` / `list_cameras_with_state`） |
| 修改设备控制 API 端点 | `miot/router.py`                                                                 |
| 修改 MiOT SDK 封装层  | `miot/client.py`（MiotProxy），更底层看 `backend/miot/src/miot/`                 |
| 修改摄像头管理逻辑    | `miot/camera_handler.py`（`CameraVisionHandler`）                                |

### 设备控制相关 API 路径

主要入口：`POST /api/miot/devices/{did}/control`（控制设备），`GET /api/miot/device_list`（设备列表），完整端点见 `miot/router.py`。

### 与其他模块的关系

**上游**：`miloco-devices` Skill 通过 CLI 调 `/api/miot/devices/{did}/control`，是主要控制入口。`RuleRunner`（`rule/runner.py`）在 STATIC 规则条件满足时直接调用 `MiotProxy`。

**下游**：所有控制 / 查询 / 动作指令经 `MiotProxy` → `MIoTClient.set_props_async` / `get_props_async` / `action_async`，由 SDK 内部决定走本地中枢网关还是云端兜底（见上「控制优先走本地中枢，云端兜底」）。

**互动**：scope 变更（切换家庭 / 启停摄像头）后，`MiotService` 先按 `select_active_camera_dids` 口径重建 / 销毁 camera manager（停用 / 移出家庭的摄像头停掉 native 会话），再同步感知层 adapter 的投喂订阅，无需重启服务。OAuth 完成后，`MiotService` 主动重启感知引擎，让摄像头 adapter 重新注册帧回调。
