# 会成长的个人助手

面向单用户的本地网页助手。它使用 Kimi 分析通知和邮件，并通过 Playwright/Chromium 执行公开网页查询；数据以 JSON 和 Markdown 保存在本机，不依赖数据库。

当前 v0.1～v0.3 的主要闭环已完成，v0.4 已实现可编辑邮件草稿与显式 SMTP 发送的本地闭环。项目初始规划、当前功能和后续安排见 [PLAN.md](PLAN.md)。

`feat/unified-assistant` 分支已实现统一聊天首版，等待真实 API 和用户验收，尚未合入 `main`。设计与实现细节见[统一入口方案](docs/unified-assistant-design.md)。本分支首页提供多会话、资料选择、跨类型工具执行与会话内草稿编辑／发送。

## 当前能力

- 通知卡片：左侧“通知”查看、筛选、新建、编辑、删除和恢复，支持日期、精确时间或每周循环，并可加入当前对话；助手也可以通过工具操作同一张卡片。
- 名称与称呼：“设置与画像”中可修改助手名称和用户昵称，跨会话生效；昵称不自动作为真实姓名或邮件署名，配置保存在本机私有数据目录。
- 导入文本：聊天旁“导入文本”可粘贴消息或读取UTF-8的txt/md文件，资料管理可批量导入JSON。导入只保存，不自动分析；分析后可“保存为通知”。
- 网页任务：Kimi 自主导航、搜索、点击、筛选、翻页、管理标签页和读取详情，保留来源、动作、失败诊断与最终结果。
- QQ 邮箱：只读同步新邮件标题，按需读取正文，明确要求后进行主题初筛和细读分析，展示摘要、事项、时间、行动和原文证据。
- 邮件草稿：通知、邮件分析或网页任务均可作为上下文；Kimi 填写收件人、抄送、密送、主题和纯文本正文，用户可继续对话或直接编辑。内容变化才产生版本，可从历史版本恢复。
- 邮件发送：只发送用户当前看到并点击确认的草稿版本；发送后移入独立只读记录，阻止过期版本、重复发送和结果不明时的盲目重试。
- 画像：区分可用于公开检索的画像与只在私人分析阶段使用的画像；更新有来源记录，用户可查看和纠正。
- 多会话：历史会话可切换、重命名和删除，完整保存工具执行、资料快照与手工操作；不同会话可引用并修改同一份草稿。
- 统一入口：输入框旁添加邮件、通知、网页结果或草稿，也可让助手搜索已保存资料；同轮可完成邮件分析、网页查询和起草。
- 本地持久化：任务、邮件、会话、用量及记忆在重启后保留；未完成轮次标记中断，不自动继续调用模型。

## 权限边界

- 网页能力面向匿名公开信息查询，不自动登录、绕过验证码或读取本机/内网地址。
- 邮件方面，Kimi 只能创建或修改本地草稿，不能调用发送操作。SMTP 发送只由草稿详情面板的“发送当前版本”按钮触发。
- 删除邮件、投递、提交表单、上传、购买及其他外部写操作尚未开放。
- 邮箱后台只同步和保存，不自动调用模型；初筛和细读由用户手动触发。
- 网页浏览阶段不接收私人画像或完整邮件正文。邮件追问需要补查公开链接时，会切换到隔离的公开浏览阶段。
- 网页执行器用独立浏览上下文运行并保存证据，完成后交回主会话。补查产生关联的新任务；关闭页面或切换会话不丢失已保存记录。
- 所有真实模型尝试，包括失败和追问中的多轮工具调用，都会计入日额度，也可能产生费用。

这些边界是当前原型的防护，不代表任意网站操作都已被证明无副作用。模型结论和引用仍需人工复核。

## 快速启动

要求 WSL/Linux、Python 3.10+。

部署和日常更新统一使用 `main`：首次克隆用 `git clone -b main <仓库地址>`，进入项目根目录后执行下面的安装步骤。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e .
cp .env.example .env
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-cache"
.venv/bin/python -m playwright install chromium --no-shell
.venv/bin/python -m assistant
```

浏览器访问 `http://127.0.0.1:8000`。可用 `--port 8001` 修改端口。

默认是 `demo` 模式，只验证界面和存储，不进行真实语义分析或网页任务。启动使用单进程、8线程的 Waitress；当前不支持多 worker 或多个实例共用一个数据目录。

## 校内服务器 IP 测试部署

在目标机器 clone 后进入项目目录，按“快速启动”安装 Python 依赖和 Chromium。无需安装 Node.js、数据库或 Docker。如果 Chromium 提示缺少系统库，运行 `.venv/bin/python -m playwright install-deps chromium`；此步骤通常需要管理员权限。

编辑服务器上的私有 `.env`，保留 Kimi、邮箱等配置，再设置：

```dotenv
ASSISTANT_HOST=0.0.0.0
ASSISTANT_PORT=11209
# 替换成浏览器实际访问的网关 IP；只填 IP，不加 http:// 或 :11209
ASSISTANT_TRUSTED_HOSTS=localhost,127.0.0.1,你的网关IP
ASSISTANT_ACCESS_TOKEN=替换为自己生成的至少24字符随机口令
```

`11209` 是 Mario 的示例映射端口，须确认空闲。浏览器访问 `http://网关IP:11209/`，使用 `ASSISTANT_ACCESS_TOKEN` 登录。`0.0.0.0` 是监听地址，不是浏览器访问地址。非回环监听必须设置访问口令；HTTP 校内测试流量没有加密。

从项目根目录启动（先停止占用相同端口的网络测试程序）：

```bash
chmod 600 .env
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-cache"
.venv/bin/python -m assistant
```

终端中的 `.env` 配置会自动加载，无需 `source .env`；已经导出的同名环境变量优先。可以用 `--host` 和 `--port` 临时覆盖监听配置。服务器重启应用后需要重新登录；会话与业务资料保留，未完成轮次与网页任务标记中断。

测试期间可用 tmux 保持 SSH 断开后继续运行：

```bash
tmux new -s personal-assistant
# 在 tmux 中进入项目根目录，再执行上面的 export 与启动命令
```

按 `Ctrl+B` 然后按 `D` 脱离会话；用 `tmux attach -t personal-assistant` 返回，`Ctrl+C` 停止服务。tmux 不提供崩溃或机器重启后的自动启动。

更新时先停止服务并备份 `data/`，再 `git pull --ff-only`、重新安装依赖并启动；Playwright 版本变化后重新安装 Chromium。新 clone 不含 `.env`、邮箱记录、草稿和画像；如需迁移数据，应在源实例停止后私下复制 `.env` 与 `data/`，不要通过 Git 上传。

## Git 分支与版本

- `main`：已完成相关检查、供单用户测试部署的主线；Mario 从这里克隆和更新。当前测试版标签为 `v0.4.0-beta.1`，不表示所有功能都已完成真实环境验收。
- `feat/<简短功能名>`、`fix/<简短问题名>`：从最新 `main` 创建，一次只处理一个功能或问题，检查完成后合并回 `main` 并删除分支。
- 标签：保留固定代码节点，不随后续提交移动。`archive/v0.1-web`、`archive/v0.2-browser` 和 `archive/browser-namespaced-reads` 保存本次清理前的旧分支末端，只是历史节点，不代表对应版本的正式发布。

日常流程示例（先保证工作树干净；不要把私人文件顺手提交）：

```bash
git switch main
git pull --ff-only
git switch -c fix/example-issue
# 修改、验证，并明确选择文件提交
git add <本次修改的文件>
git commit -m "fix: describe the issue"
git switch main
git merge --ff-only fix/example-issue
git branch -d fix/example-issue
git push origin main
```

如果 `--ff-only` 提示分叉，先检查双方提交再处理，不强制覆盖。当前仍为单人开发流程，不额外维护长期 `develop` 分支；版本发布时在 `main` 上创建带说明的标签。

本次本地分支整理后，首次同步由维护者执行：

```bash
git push -u origin main --follow-tags
```

随后在仓库网页设置默认分支为 `main`。确认远端主线和标签已存在后，可删除已归档的远端旧分支：

```bash
git push origin --delete fix/browser-namespaced-reads
git fetch --prune
```

已有服务器检出旧分支时，停止应用、保留并备份本机数据，在干净工作树中执行 `git fetch origin`、`git switch main` 后按部署说明更新依赖并启动。Git 标签只保存代码，不包含 `.env`、画像或邮箱数据备份；切换旧版本前也需备份数据并核对格式兼容性。

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

更新代码或系统提示词后同样需要停止并重新启动应用，当前服务不自动重载代码。新主会话模型事件中的 `system_template_sha256` 可用于追溯使用的主助手提示词模板版本；该字段不保存账号背景或完整请求内容。

网页任务默认每次最多 40 轮模型调用、120 个动作和 900 秒，可通过以下变量调整：

```dotenv
ASSISTANT_BROWSER_CALLS=40
ASSISTANT_BROWSER_STEPS=120
ASSISTANT_BROWSER_SECONDS=900
ASSISTANT_BROWSER_VISION=false
```

浏览器默认通过系统 DNS 直连。需要经过 FlClash 等 HTTP 代理时，在 `.env` 设置 `ASSISTANT_BROWSER_PROXY=http://代理主机:端口`，例如 `http://127.0.0.1:7890`，然后重启应用。WSL 中代理主机应使用 WSL 可访问的地址。该配置用于当前浏览器的全部连接；留空恢复直连。

网页任务和追问共用 `ASSISTANT_DAILY_CALLS`。视觉截图默认关闭；开启前需确认模型支持图片与工具调用。

统一会话新增以下可选配置（不填写则使用默认值）：

```dotenv
ASSISTANT_TURN_CALLS=50
ASSISTANT_TURN_SECONDS=1200
ASSISTANT_CONTEXT_CHARS=300000
```

本轮调用预算包含主助手和浏览执行器的调用，并受日剩余额度限制；默认日额度30次仍可能先耗尽，可按真实使用情况调整。上下文按主会话完整历史组织，字符上限是程序侧限制，不等于模型精确 token 容量；超出时停止，不自动裁剪。修改配置后重启。

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

1. 登录网页，在“设置与画像”中启用同步。首次连接只建立新邮件基线，不读取历史。
2. 发送一封新的文本测试邮件，或明确确认后只导入最新一封已有邮件。
3. 在聊天中要求邮件头初筛，或从“添加资料”选择邮件后要求分析。后台轮询不会自动调用 Kimi。
4. HTML 会转成纯文本且不加载远程图片；附件目前只显示名称，不解析内容。

邮件连接使用只读 IMAP、`BODY.PEEK` 和 TLS，不标已读、不移动或删除远端邮件。不要使用 QQ 登录密码代替授权码。
同一 QQ 授权码也用于 SMTP TLS 登录；凭据只由服务端读取，不会发送给 Kimi 或浏览器。

## 使用流程

### 网页任务

直接在聊天中输入公开查询目标和起始 HTTPS URL。主助手调用现有独立浏览执行器，运行中显示状态，可停止；完成后在本轮结果中打开详情、出处、保存正文和诊断。

任务完成后直接继续聊天，也可以在其他会话添加该任务结果。补查沿用相关任务范围，新记录保留原结果。主会话的私人内容不整体传入浏览执行器。

### 邮件分析

同步只保存新邮件。在输入框旁点“添加资料” → 选择“邮件” → 搜索、勾选 → 添加到消息，再输入要求。也可直接让助手搜索本机已保存邮件。模型可提出有证据的画像补充；归属不明的建议在邮件详情中等待用户确认。

同一轮可继续查询公开网页并起草邮件，无需切换功能页面。资料管理只负责浏览与维护，没有独立聊天窗口。

### 邮件草稿与发送

可在资料选择器新建空白草稿，也可在聊天中要求基于多份通知、邮件和网页结果起草。点击本轮结果中的草稿，在旁边的详情面板编辑或继续聊天修改。跨会话引用默认共享同一对象；需要独立方案时点击“复制为新草稿”。

手工输入约800毫秒后自动保存，切换对象／会话和发送前等待保存；失败时保留浏览器本地输入。仅内容变化产生版本，历史消息保留当时内容，编辑器打开当前版本。恢复历史会创建新版本。删除会话不删除独立草稿；单独删除草稿保留会话里已经形成的历史快照。

点击“发送当前版本”先保存页面修改，再通过 QQ SMTP 发送；该点击是对当前收件人、主题和正文的单次授权。发送状态在详情面板及会话记录中展示，已发送对象只读。SMTP 接受不代表已阅读；结果不明时先核查 QQ 已发送邮件，不自动重发。

## 数据与隐私

私有数据默认保存在被 Git 忽略的 `data/`：

- `messages/`：手动导入的通知与分析结果；
- `tasks/`：网页任务、动作、来源和结果；
- `mail/`：QQ 邮件与同步状态；
- `drafts/`：可编辑邮件草稿、版本、编写对话和发送结果；
- `memory/public_profile.md`：可发送给模型用于公开检索的画像；
- `memory/profile.md`：只用于私人分析的画像；
- `memory/rules.md` 和 `memory/history/`：规则与修改历史；
- `conversations/`：多会话、轮次状态、工具执行、资料快照与用户操作；
- `chat.json`：旧普通对话，首次启动时兼容导入为“历史对话”，原文件保留；
- `conversation-migration.json`：旧聊天导入标记，防止重复导入；
- `usage.json`：模型调用用量。

浏览器中另保存当前会话标识，以及本标签页未发送的输入／尚未保存的草稿内容，便于刷新恢复；不将这些输入上传到公开仓库。会话历史、业务记录和浏览器本地输入具有不同生命周期，删除会话不会清除所有关联资料。

“公开检索画像”仍是本机私有文件，“公开”只表示允许其内容进入公开网页任务的模型上下文。当前没有自动备份、清理或保留期限；请自行备份 `data/`，不要将其提交到 Git。

## 测试

```bash
.venv/bin/python -m unittest discover
.venv/bin/python -m compileall -q assistant tests
git diff --check
```

离线测试使用临时目录、合成邮箱、SMTP 和模拟模型，不访问真实账号或产生 API 费用。

可选的 Chromium 合成界面回归：

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-cache"
.venv/bin/python -m tests.browser_smoke
.venv/bin/python -m tests.browser_conversation_smoke
```

真实 Kimi、真实邮箱和公开网站联调脚本位于 `tests/`，运行前需要单独确认费用和数据范围。

## 已知限制

- 邮件发送第一版仅支持 QQ SMTP 纯文本邮件；尚无附件、HTML、联系人目录、HTTPS 公网部署、主动推送、Bark 或网盘备份。
- 合成 SMTP 已验证，真实 QQ 发送仍需由用户指定测试收件人并明确触发；SMTP 接受不等于最终投递或阅读。
- QQ/微信客户端自动接入已取消；聊天内容需通过邮件、复制或导入提供。
- 浏览器代理由 `ASSISTANT_BROWSER_PROXY` 显式配置，域名使用系统 DNS，连接层选择解析结果中的公网地址。
- Linux 无中文字体时，Chromium 截图可能显示方框，但 DOM 文字读取不受影响。
- 页面可读、HTTP 成功或测试通过都不等于模型结论可靠，也不代表覆盖了整个网站。
- 统一入口首版使用纯文本回答、关键词资料搜索和轮询进度；同一会话一次执行一轮，同一时间仅一个浏览任务。长历史达到字符／模型上下文限制后需新建会话并添加所需资料。

通知卡片的数据关系、删除范围与实现说明见[通知卡片与导入资料](docs/notification-cards.md)。旧 notification 指导入文本；新卡片为 notice。旧文件不批量迁移。

循环事项可直接说“新建一条通知，每周二18:30开组会”，或在“通知 → 新建通知 → 重复：每周”中选择星期、当地时刻和时区。支持每周多天，可选生效／最后日期；卡片显示下次发生时间。修改和删除作用于整个系列，可改回“不重复”；当前不支持单次例外、主动推送或定时执行。
