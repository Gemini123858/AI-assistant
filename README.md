# 会成长的个人助手

面向单用户的本地网页助手。它使用 Kimi 分析通知和邮件，并通过 Playwright/Chromium 执行公开网页查询；数据以 JSON 和 Markdown 保存在本机，不依赖数据库。

当前 v0.1～v0.3 的主要闭环已完成，v0.4 已实现可编辑邮件草稿与显式 SMTP 发送的本地闭环。详细设计、历史测试和后续阶段见 [PLAN.md](PLAN.md)。

## 当前能力

- 通知：手动输入或导入 JSON，提取摘要、行动、时间和不确定项，支持基于通知的问答与邮件文本草稿。
- 网页任务：Kimi 自主导航、搜索、点击、筛选、翻页、管理标签页和读取详情，保留来源、动作、失败诊断与最终结果。
- QQ 邮箱：只读同步新邮件标题，按需读取正文，手动进行主题初筛和细读分析，展示摘要、事件表、时间、行动和原文证据。
- 邮件草稿：通知、邮件分析或网页任务均可作为上下文；Kimi 填写收件人、抄送、密送、主题和纯文本正文，用户可继续对话或直接编辑。内容变化才产生版本，可从历史版本恢复。
- 邮件发送：只发送用户当前看到并点击确认的草稿版本；发送后移入独立只读记录，阻止过期版本、重复发送和结果不明时的盲目重试。
- 画像：区分可用于公开检索的画像与只在私人分析阶段使用的画像；更新有来源记录，用户可查看和纠正。
- 连续追问：最新一次网页任务和最新一次邮件分析可继续对话，并按需再次调用公开网页工具。
- 本地持久化：任务、邮件、聊天、模型用量及记忆在重启后保留；临时追问会话除外。

## 权限边界

- 网页能力面向匿名公开信息查询，不自动登录、绕过验证码或读取本机/内网地址。
- Kimi 只能创建或修改本地草稿，不能调用发送操作。SMTP 发送只由邮箱工作台的“发送当前版本”按钮触发。
- 删除邮件、投递、提交表单、上传、购买及其他外部写操作尚未开放。
- 邮箱后台只同步和保存，不自动调用模型；初筛和细读由用户手动触发。
- 网页浏览阶段不接收私人画像或完整邮件正文。邮件追问需要补查公开链接时，会切换到隔离的公开浏览阶段。
- 追问每次需要浏览时重新启动匿名 Chromium；新证据只保存在当前内存会话。关闭、新建同类任务、退出登录或重启服务后不可恢复。
- 所有真实模型尝试，包括失败和追问中的多轮工具调用，都会计入日额度，也可能产生费用。

这些边界是当前原型的防护，不代表任意网站操作都已被证明无副作用。模型结论和引用仍需人工复核。

## 快速启动

要求 WSL/Linux、Python 3.10+。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e .
cp .env.example .env
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-cache"
.venv/bin/python -m playwright install chromium --no-shell
.venv/bin/python -m assistant
```

浏览器访问 `http://127.0.0.1:8000`。可用 `--port 8001` 修改端口。

默认是 `demo` 模式，只验证界面和存储，不进行真实语义分析或网页任务。当前使用 Flask 本地开发服务器，不要直接暴露到公网或使用多 worker 启动。

## 配置 Kimi

编辑被 Git 忽略的 `.env`：

```dotenv
ASSISTANT_LLM_MODE=api
ASSISTANT_API_KEY=你的Kimi_API_Key
ASSISTANT_API_BASE=https://api.moonshot.cn/v1
ASSISTANT_MODEL=kimi-k2.6
ASSISTANT_LLM_EXTRA_BODY='{"thinking":{"type":"disabled"}}'
ASSISTANT_DAILY_CALLS=30
ASSISTANT_TIMEZONE=Asia/Shanghai
```

修改配置后重启应用。不要把真实 Key 写入 `.env.example`、通知、画像、规则或聊天。API 失败不会自动回退到演示模式。

网页任务默认每次最多 40 轮模型调用、120 个动作和 900 秒，可通过以下变量调整：

```dotenv
ASSISTANT_BROWSER_CALLS=40
ASSISTANT_BROWSER_STEPS=120
ASSISTANT_BROWSER_SECONDS=900
ASSISTANT_BROWSER_VISION=false
```

网页任务和追问共用 `ASSISTANT_DAILY_CALLS`。视觉截图默认关闭；开启前需确认模型支持图片与工具调用。

## 配置 QQ 邮箱

在 `.env` 中添加：

```dotenv
QQ_IMAP_USER=你的QQ邮箱地址
QQ_IMAP_AUTH_CODE=QQ邮箱生成的IMAP授权码
ASSISTANT_ACCESS_TOKEN=至少24字符的本地网页访问口令
ASSISTANT_MAIL_POLL_SECONDS=60
ASSISTANT_CHAT_SELF_NAMES=
```

可在本地生成访问口令：

```bash
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

`ASSISTANT_ACCESS_TOKEN` 只保护本地网页，不是邮箱授权码或模型 Key。启用邮箱后：

1. 登录网页，在“邮箱”中启用同步。首次连接只建立新邮件基线，不读取历史。
2. 发送一封新的文本测试邮件，或明确确认后只导入最新一封已有邮件。
3. 手动点击主题初筛或细读分析。后台轮询不会自动调用 Kimi。
4. HTML 会转成纯文本且不加载远程图片；附件目前只显示名称，不解析内容。

邮件连接使用只读 IMAP、`BODY.PEEK` 和 TLS，不标已读、不移动或删除远端邮件。不要使用 QQ 登录密码代替授权码。
同一 QQ 授权码也用于 SMTP TLS 登录；凭据只由服务端读取，不会发送给 Kimi 或浏览器。

## 使用流程

### 网页任务

填写起始 HTTPS URL、公开任务目标，以及可选的本次公开画像补充和私人筛选条件。Kimi 在公开阶段操作网页；需要结合私人条件时，会切换到没有网页动作权限的分析阶段。

任务完成后，可在最新结果下继续追问。网页追问继承原任务范围；限定域任务不能通过追问扩域。

### 邮件分析

同步只保存新邮件。用户手动选择初筛、读取正文或细读分析。模型可以基于当前邮件证据提出画像补充；无法确认说话人属于用户时，会等待人工确认。

最新邮件分析同样支持追问。用户明确要求查看公开链接时，Kimi 可进入隔离浏览阶段，取得网页证据后返回邮件上下文回答。

### 邮件草稿与发送

可新建空白草稿，也可从通知、邮件或已完成的网页任务创建草稿。输入修改要求后，Kimi 会直接更新结构化字段；程序只检查数据结构、长度、邮件地址和版本，不按关键词审查或重写邮件内容。

用户可随时手动编辑并保存；没有内容变化时不会增加版本。恢复历史版本会创建一个新的当前版本，已有历史不会被覆盖。可编辑草稿也可在二次确认后直接删除；删除会永久移除该草稿的全部版本和编写对话，但不能删除独立的发送记录。点击“发送当前版本”会先保存页面上的最新内容，再通过 QQ SMTP 发送；发送结果在独立区域只读展示。该点击就是对当前收件人、主题和正文的单次授权。SMTP 接受不代表收件人已阅读；提交阶段断线会显示“结果不明”，此时应先核查 QQ 已发送邮件。

## 数据与隐私

私有数据默认保存在被 Git 忽略的 `data/`：

- `messages/`：手动导入的通知与分析结果；
- `tasks/`：网页任务、动作、来源和结果；
- `mail/`：QQ 邮件与同步状态；
- `drafts/`：可编辑邮件草稿、版本、编写对话和发送结果；
- `memory/public_profile.md`：可发送给模型用于公开检索的画像；
- `memory/profile.md`：只用于私人分析的画像；
- `memory/rules.md` 和 `memory/history/`：规则与修改历史；
- `chat.json`、`usage.json`：普通对话与模型用量。

“公开检索画像”仍是本机私有文件，“公开”只表示允许其内容进入公开网页任务的模型上下文。当前没有自动备份、清理或保留期限；请自行备份 `data/`，不要将其提交到 Git。

## 测试

```bash
.venv/bin/python -m unittest discover
.venv/bin/python -m compileall -q assistant tests
git diff --check
```

离线测试使用临时目录、合成邮箱、SMTP 和模拟模型，不访问真实账号或产生 API 费用。当前共有 181 项单元测试。

可选的 Chromium 合成界面回归：

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-cache"
.venv/bin/python -m tests.browser_smoke
.venv/bin/python -m tests.browser_task_smoke
.venv/bin/python -m tests.browser_generic_smoke
.venv/bin/python -m tests.browser_mail_smoke
```

真实 Kimi、真实邮箱和公开网站联调脚本及历史结果记录在 [PLAN.md](PLAN.md)，运行前需要单独确认费用和数据范围。

## 已知限制

- 邮件发送第一版仅支持 QQ SMTP 纯文本邮件；尚无附件、HTML、联系人目录、远程安全部署、主动推送、Bark 或网盘备份。
- 合成 SMTP 已验证，真实 QQ 发送仍需由用户指定测试收件人并明确触发；SMTP 接受不等于最终投递或阅读。
- QQ/微信客户端自动接入已取消；聊天内容需通过邮件、复制或导入提供。
- Chromium 不继承 WSL 的 `HTTP_PROXY`；当前使用系统 DNS，并拒绝解析到私网或 Fake-IP 的公网目标。
- Linux 无中文字体时，Chromium 截图可能显示方框，但 DOM 文字读取不受影响。
- 页面可读、HTTP 成功或测试通过都不等于模型结论可靠，也不代表覆盖了整个网站。
