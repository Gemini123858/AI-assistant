# QQ 文字聊天入口

2026-11-04。首版代码与离线闭环已实现，用户已完成 QQ 连接本地测试并授权合入本地 `main`；已明确确认扫码绑定和真实 QQ 私聊 `/help` 正常收发。仅接入腾讯官方机器人私聊，复用当前主助手、工具、画像及原有完整会话上下文；本轮不实现新的伙伴记忆、压缩、群聊、多模态、定时提醒或邮件发送确认。

## 使用效果

本人私聊机器人，文字直接交给现有 Conversations；助手可继续查询网页、读取本机邮件/资料、管理通知和起草邮件。每个绑定保存一个当前 QQ 会话，网页历史列表显示 `QQ · 首条问题`，可打开查看、继续交流。网页中执行的轮次不自动推送 QQ，QQ 只回复从 QQ 发起的轮次。

普通聊天采用现有 API 配置、调用预算和上下文限制；演示模式明确返回演示说明。重启保留记录与 QQ 会话映射，未完成任务按原规则中断，不自动重新执行或补发。

命令无需模型调用：

| 命令 | 作用 |
| --- | --- |
| `/new` | 开始新对话；保留旧会话，执行中需先停止 |
| `/status` | 查看当前进度、网页会话标题和 ID |
| `/stop` | 请求停止当前任务，保留已有产物 |
| `/result` | 查看当前会话最近一轮结果；执行中返回进度 |
| `/help` | 显示命令 |

同一会话执行时，新请求明确提示忙碌，不排队或合并；可发状态/停止命令。图片、语音和文件会提示仅支持文字，不下载附件。

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

- 只接受绑定本人的 C2C；群消息和其他用户被忽略。近期重复消息去重，避免重复触发模型。QQ 发送是对用户本轮请求的回复，不包含定时或无关会话推送。
- 使用普通文本消息，不依赖 Markdown/按钮权限。长回答按 Unicode 字符分块，最多四条；超过展示上限会明确提示在网页查看完整回答，原回答不会被截断存储。
- 只发送最终结果和命令响应，不逐步刷工具调用。网页可看完整执行记录与引用。模型/网页失败或取消时返回实际状态。
- 回复关联原入站消息；平台回复窗口、额度、内容审核或网络可能导致失败。未自动切换主动消息或重发，结果留在网页；可用 `/result` 明确请求重看。发送成功只表示 SDK 请求成功，不代表用户已阅读。
- QQ 轮次发送结果在会话中记为 `qq_delivery`。发送失败或结果不明时不重做模型任务。服务重启不会自动补发；处理前已记录的近期消息不自动重放。
- 草稿发送仍需网页核对确认；QQ 消息不能代替发送按钮。普通网页入口和既有权限保持不变。
- 全部清理 `--all` 会移除 QQ 凭据和映射，需要重新绑定；仅清理会话后，下次 QQ 输入会建立新会话。

## 验证

```bash
.venv/bin/python -m unittest tests.test_qqbot -v
npm --prefix assistant/qq_bridge test
```

合成身份、模型、QQ 发送器与真实本地子进程管道用于验证权限过滤、消息去重、连续对话、工具复用、忙碌/取消、新会话、重启映射、发送失败及关闭流程。另运行全量离线回归与真实 SDK 包加载检查，不连接真实账号或产生模型费用。

本轮254项Python离线回归、4项Node渠道测试通过；wheel构建并核对桥接脚本/锁文件已包含，node_modules与私人数据未包含。

用户已实测通过扫码绑定和 `/help` 命令双向收发。该命令不调用模型；普通模型聊天、连续追问、工具执行、断网重连及长任务回复窗口仍待实测，不将命令收发成功表述为这些能力均已验收。

参考：[腾讯 dsh-qqbot](https://github.com/tencent-connect/dsh-qqbot)、[QQ Node SDK](https://github.com/tencent-connect/qqbot-nodejs)。本项目复用底层依赖，不安装完整 DSH。
