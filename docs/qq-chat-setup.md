# QQ 文字聊天入口

2026-11-04更新。原QQ基础连接已由用户实测；本轮扩展持续记忆、独立性格、按需工具及中途发言，代码和离线测试已完成，新行为尚未连接真实账号或付费模型验证。

## 使用效果

QQ是一段持续的本人文字交流，话题自然切换，不提供/new等斜杠程序命令。自己的回忆围绕经历形成片段，未完便签接续尚未结束的事情；模型自主决定何时读写。网页暂不显示、继续或修改QQ对话与回忆，业务资料、通知和草稿仍可管理。

默认性格活泼、调皮、有主见，适度接梗或顶嘴，提示词采用正面表达。名称和称呼沿用已有identity设置。参考社区角色的表达取向，采用本项目原创提示词：[性格参考](https://github.com/yunyunyunyunsuan/dsh-plugin-moments#-人设蓝色大肥鱼社区共创--官方收编)。没有照搬角色身份或社区对其能力的宣称。

每次主助手响应可通过content或工具参数say说一句话。程序在响应落盘后、执行工具前直接提交QQ发送；最终finish_turn.reply在收尾成功后发送，相同content与reply只发送一次。转发的是公开交流文字，内部推理字段保持在协议之外。这里是响应级即时投递，不是逐token流式输出；内部历史摘要调用及网页子任务的执行日志不直接转发。

新一轮先提供简短能力概览和list_tools、finish_turn。模型按需发现工具，完整schema加入下一次请求，同一轮继续可用，下轮重置。发现步骤消耗主模型调用额度，普通闲聊可直接收尾。

普通聊天沿用API配置和总预算；演示模式返回演示说明。执行中再次输入暂时仍提示忙碌，没有新增排队或自然语言实时打断。重启保留记录和QQ映射，已中断任务不自动执行或补发。附件仍提示当前文字范围。

可选配置：

```dotenv
ASSISTANT_QQ_MEMORY_HALF_LIFE=30
ASSISTANT_QQ_CONTEXT_CHARS=48000
ASSISTANT_QQ_RECENT_TURNS=8
```

半衰期影响普通回忆活跃度，不改写正文；便签与明确固定的回忆不普通衰减。整理软目标以字符计，不冒充精确token。需要时将较早完整轮次整理为摘要，当前窗口带最近3段摘要，更多细节可通过read_chat查原话；摘要调用同样计入预算。正在执行的工具链保留，特别大的单轮资料仍可能需要分段处理。

私人数据新增companion/<QQ记录ID>/：Markdown保存回忆、便签和摘要，state.json原子选择有效版本；原始聊天继续在conversations保存。停机清理可单选companion，或使用原有--all。它们不是用户需要选择的聊天会话。

## 安装依赖

Linux / WSL 项目目录下，确保 Node.js >=20.3 和 npm 在 PATH 中（推荐 Node 24）。使用 nvm 时先在当前终端激活版本；也可通过 `ASSISTANT_QQ_NODE` 指定 Node 可执行文件绝对路径。

```bash
npm --prefix assistant/qq_bridge ci --omit=optional --ignore-scripts
node assistant/qq_bridge/bridge.mjs check
```

`check` 仅加载真实 SDK 包，不连接账号。Node 依赖单独安装，不包含在 Python wheel 内；wheel 包含桥接脚本和锁文件，使用安装包时在其 `assistant/qq_bridge` 目录运行 npm ci。Python 应用默认不开 QQ，不安装 Node 也能照常使用网页。

## 扫码绑定

先停止应用，然后执行：

```bash
.venv/bin/python -m assistant.qqbot bind
```

终端显示二维码和可在浏览器打开的绑定链接。用本人 QQ 扫码授权机器人；凭据返回后写入私有数据目录 `qqbot-credentials.json`，权限为 0600，不打印 AppSecret。采用 dsh-qqbot 使用的腾讯 `qqbot-connector`，要求返回扫码本人的 userOpenid；缺失时绑定失败，不放开给任意用户。

该命令复用应用单写入锁，运行中的应用会阻止绑定。只进行机器人绑定，不调用模型或读取远端邮件。取消可按 Ctrl+C。具体绑定页和机器人添加/打开聊天操作以 QQ 实际页面为准；用户已完成一次真实扫码绑定。

已有机器人凭据时，可在被忽略的 `.env` 中配置以下三项，优先于扫码文件：

```dotenv
QQBOT_APPID=你的机器人AppID
QQBOT_SECRET=你的机器人AppSecret
QQBOT_OWNER_OPENID=本人在该机器人下的userOpenid
```

userOpenid 不是 QQ 号或昵称；三项必须同时配置，不自动信任第一个发消息的人。手动配置时无需扫码。

## 开启和使用

在 `.env` 中设置，然后启动原应用：

```dotenv
ASSISTANT_QQ_ENABLED=true
ASSISTANT_QQ_NODE=node
```

```bash
.venv/bin/python -m assistant
```

Python 启动 Node WebSocket 连接进程，通过标准输入输出交换消息，共用原应用 Store 与业务执行层；不新增入站端口、HTTP 鉴权豁免或第二个业务写进程。Node 不接收模型 Key、邮箱授权码和网页口令，只接收 QQ 凭据。停机先关闭连接进程，再停止业务执行与存储。

QQ 入口状态通过登录保护下的 `/api/status` 的 `qq_bot` 返回：connecting、connected、reconnecting、disconnected 等。故障信息不带原始凭据或消息正文。SDK 处理心跳和重连，Node 进程退出后需要排查并重启应用。

## 迁移到服务器

无需创建新机器人或重复扫码。先停止本地应用，私下迁移 `.env` 和 `data/` 到服务器，再安装 Python、Node 及桥接依赖，启用 QQ 后按原方式启动。确保服务器使用迁移后的数据目录，并执行 `chmod 600 .env data/qqbot-credentials.json`（自定义数据目录时修改路径）。

只迁移绑定文件也可沿用机器人，但不会带上原会话和业务资料；完整迁移 `data/` 才能保留这些记录。同一机器人只运行一个连接实例；不要将凭据提交 Git。按 Ctrl+C 退出应用会同时关闭 QQ 子进程，绑定文件保留。

## 发送边界与排错

- 只接受绑定本人的 C2C；群消息和其他用户被忽略。近期重复消息去重，避免重复触发模型。每次发送使用独立标识，同一入站可对应多次公开响应。QQ 发送是对用户本轮请求的回复，不包含定时或无关会话推送。
- 使用普通文本消息，不依赖 Markdown/按钮权限。每条响应按Unicode字符分块，最多四块；超长时提示可分段继续，完整回答仍保存在本地。
- 主助手每次响应的公开文字即时交付，工具细节仍保存在本地。模型失败或取消时回复实际状态。
- 回复关联原入站消息；平台回复窗口、额度、内容审核或网络可能导致失败。未自动切换主动消息或重发，结果保存在本地；可以在后续自然交流中请求回查。发送成功只表示 SDK 请求成功，不代表用户已阅读。
- QQ 轮次发送结果在会话中记为 `qq_delivery`。发送失败或结果不明时不重做模型任务。服务重启不会自动补发；处理前已记录的近期消息不自动重放。
- 草稿发送仍需网页核对确认；QQ 消息不能代替发送按钮。普通网页入口和既有权限保持不变。
- 全部清理 `--all` 会移除 QQ 凭据和映射，需要重新绑定；仅清理会话后，下次 QQ 输入会建立新会话。

## 验证

```bash
.venv/bin/python -m unittest tests.test_qqbot -v
npm --prefix assistant/qq_bridge test
```

合成身份、模型、发送器与真实本地子进程管道验证权限过滤、连续交流、动态工具schema、记忆与便签、衰减/深查、原话回查、压缩及失败保留、网页接口隔离、工具执行前发言、最终回复去重与进程关闭。Node通道测试验证同一入站可发送多次独立响应，重放同一发送标识不会重复投递。

273项Python与5项Node离线测试通过，补充了连续重复发言、格式碎片、超长中途发言、回忆引用与下一轮刷新回归。中途发言say必填，最多300字符，发送前完成长度与已观察到的格式碎片校验；超长或含碎片时由模型重新表达。最终回答保留原有长度额度。去重只作用于同一轮连续完全相同的文字。

经用户授权，使用现有模型配置、独立临时目录、合成回忆和模拟QQ发送器进行了真实模型测试：累计19次请求尝试，18次收到响应、1次网络失败；完成留便签、从预置回忆找答案、关闭指定便签、闲聊、修订原回忆及重新检索核对。新建便签用3次调用，纠正用了5次（模型自主再次修改了杂话），回查确认“假设未验证、另一张便签仍未完成”。真实模型成功使用了回忆/便签引用，并在下一轮刷新。

测试也暴露了无关回忆、零星杂词，以及一轮中途发言夹带大段自言自语的问题；据此增加了发送前长度拦截并做离线回归，最后这项修改未追加真实API测试。上述结果证明基础流程可运行，不代表模型表达质量或长期记忆效果已达标。本次没有接入真实QQ平台发送，平台额度、回复窗口和内容审核仍以实际投递为准。

可手动运行同一路径的集成测试（会使用.env中的真实模型配置并产生费用；不要在单元测试/CI中运行）：

```bash
.venv/bin/python -m tests.manual_qq_companion_live --root /tmp/qq-memory-live-test --scenario core
.venv/bin/python -m tests.manual_qq_companion_live --root /tmp/qq-memory-live-test --scenario correction
.venv/bin/python -m tests.manual_qq_companion_live --root /tmp/qq-memory-live-test --scenario verify
```

三次命令使用同一专用目录，持久计数文件将该目录累计生成尝试限制为20次，失败也计入；未使用的额度不会自动补跑。测试走QQChat.receive和真实会话执行层，发送器只收集文字，不连接QQ、不使用原有私人记忆，也不加载外部操作工具。

参考：[腾讯 dsh-qqbot](https://github.com/tencent-connect/dsh-qqbot)、[QQ Node SDK](https://github.com/tencent-connect/qqbot-nodejs)。本项目复用底层依赖，不安装完整 DSH。
