# 会成长的个人助手

单用户网页助手，使用 Python / Flask / 原生前端，数据保存为 JSON 和 Markdown 文件。需求和阶段安排见 [PLAN.md](PLAN.md)。

## 当前范围

- 网页手动输入或导入 JSON 通知，重复消息检查，原文查看。
- 结合画像与规则调用模型，提取事项、行动、截止/活动时间及不确定内容。
- 围绕选择的通知进行对话，也可以让模型撰写邮件**文本草稿**；没有邮件账户接入或发送功能。
- 编辑画像与规则，或在对话中使用 `/记住 信息`、`/规则 纠正`、`/查看画像`、`/查看规则`。
- 文件持久化、记忆历史与中断恢复、单实例写入保护、日 API 尝试次数限制。
- v0.2 开发中：新增“网页任务”入口、受限浏览器工具、多层导航任务记录、进度/取消与出处校验。真实 Kimi 工具调用已实测，但前次模型任务未读到列表/详情，端到端验收尚未通过。

v0.2 新方向：让 AI 根据页面观察自行选择导航、点击、检索和翻页，不为每种推广提示写固定流程。现有实现仍只有链接导航、固定词表检索与结束工具，通用浏览器动作尚未实现；权限、预算和外部写确认继续由程序控制，任意 Shell/Python 不默认开放。

尚未实现：任意网站自主操作、邮箱同步、附件识别、实时/定时主动推送、普通对话自动提取画像、授权邮件发送、远程认证部署。QQ/微信自动接入已取消，网盘与聊天备份暂缓；权限遵循自动读取、手动分析、按需草稿、写操作单独确认。网页仅本机访问，不代表已能在手机上远程使用。

## 本地启动（WSL / Linux，Python 3.10+）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e .
.venv/bin/python -m assistant
```

浏览器打开 `http://127.0.0.1:8000`。端口可通过 `--port 8001` 改变。默认演示模式不访问模型，只验证界面和保存流程；不会真实判断相关性或日期。点击“载入合成样例”可开始试用。

当前使用本地开发服务器，不要把监听地址改成公网地址，不要在多 worker 中运行；没有远程认证。同机其他用户也可能访问 localhost 服务，因此本版不适合直接部署到共享服务器保存私人消息。退出后文件仍保留，下一次启动会读取原有记录。

## 配置真实模型

在启动进程的**同一个终端**设置环境变量，或复制 `.env.example` 为 `.env` 后用编辑器填入配置。`.env` 自动读取，但不会覆盖已经存在的环境变量；修改后需重启程序。

**真实 Key 不能写入 `.env.example`，该文件会进入公开仓库。不要把 Key 发到聊天中。**

```bash
export ASSISTANT_LLM_MODE=api
export ASSISTANT_API_BASE=https://api.moonshot.cn/v1
export ASSISTANT_MODEL=kimi-k2.6
export ASSISTANT_LLM_EXTRA_BODY='{"thinking":{"type":"disabled"}}'
# 通过隐藏输入读取 Key，不将它作为命令写进 shell 历史。
read -r -s -p "API Key: " ASSISTANT_API_KEY
export ASSISTANT_API_KEY
.venv/bin/python -m assistant
```

通知分析与普通对话使用 Chat Completions JSON 输出，不发送 tools。只有明确发起的网页任务启用三个工具：读取已发现链接、公开岗位检索、保存筛选结果；模型不能执行任意代码或授予自己写权限。Kimi 参数及工具调用参考 [API 文档](https://platform.kimi.com/docs/api/chat)，换模型时核对参数要求，不盲目沿用 `thinking` 或采样参数。

默认日尝试上限 30 次、单次输出上限 4096 token、超时 60 秒；可通过 `ASSISTANT_DAILY_CALLS`、`ASSISTANT_MAX_TOKENS`、`ASSISTANT_TIMEOUT` 修改。次数限制不是金额预算。失败尝试也计数，可能已被服务商计费；没有自动重试。额度按 `ASSISTANT_TIMEZONE`（默认 Asia/Shanghai）计算，重启不清零。

配置 Key 后也必须明确选择 `api` 模式。API 请求失败不会回退到演示模式。已有演示分析结果需点击“重新分析”才会使用真实模型。

另一个终端中的 `export` 不会改变已经运行的网页服务或 Agent 的环境。若要让 Agent 联调，推荐在本地被忽略的 `.env` 中配置，而不是在对话里提供凭据。

## v0.2 网页任务（自主动作及官网稳定性待验证）

操作网页使用 Playwright 驱动真实 Chromium，不是让模型生成终端命令。首次安装浏览器后，在启动应用的同一终端设置其路径：

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m playwright install chromium --no-shell
export PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers
.venv/bin/python -m assistant
```

Linux 若缺少浏览器系统库，按 Playwright 提示安装；此操作可能需要管理员权限。浏览器二进制不进入 Git。通知/普通问答仍可在没有 Chromium 的情况下使用，只有浏览任务会提示启动失败。

在“画像与规则”填写适合发送至模型的背景，再在“网页任务”明确查询方向和约束。本版只允许字节社会招聘公开入口、列表及详情，不登录、不投递；校园入口和其他网站未覆盖。默认单任务最多12次模型尝试、300秒、40次导航、30页及20份详情；可用 `ASSISTANT_BROWSER_CALLS`、`ASSISTANT_BROWSER_SECONDS` 调整前两项，共用已有日调用额度。到达预算会保存部分证据，不伪报全站查询完成。

后台一次仅运行一个浏览任务，期间通知分析/普通问答可能返回忙碌提示；取消不会撤回已经发起的网络调用，但其结束后不执行下一动作。进程中断后保留记录，不自动恢复调用；“重新发起”明确创建新任务、使用当前画像并重新计费，暂不提供原地续跑。

专用无登录会话阻断不在白名单内的导航、下载和业务写接口。POST 仅放行已检查结构且查询参数与正文一致的岗位检索，以及仅含公共入口编号的匿名 CSRF 初始化；后者不涉及账号认证或业务数据修改，不授予一般写权限。保留官网自身生成的检索签名，不生成签名或解验证码。搜索词暂限内置职位词表，防止模型把画像片段发送给招聘网站；一般关键词扩展后续验证。固定资源域解析到公网地址后在浏览器内绑定，私网/保留地址会拒绝；这不是任意不可信网站的通用安全沙箱。

当前环境观察到代理 Fake-IP（`198.18.x.x`），系统 DNS 模式会被安全检查拒绝。可由用户调整代理使白名单域返回真实地址，或显式设置 `ASSISTANT_BROWSER_DNS=google` 使用固定的 [Google DNS-over-HTTPS](https://developers.google.com/speed/public-dns/docs/doh/json)。后者只发送公开域名、不发送画像或任务文本；默认仍为 `system`，不静默启用。2026-09-17 可选模式实测能解析并读到招聘入口，但检索探测出现 HTTP 405，正式列表/搜索读取超时；该选项不是网站可达性保证。

## 输入、存储和记忆

网页 JSON 导入支持一个通知对象或最多 20 个对象的数组，文件最多 100KB，格式如下：

```json
{
  "source": "qq",
  "conversation": "合成课程群",
  "sender": "合成助教",
  "message_id": "synthetic-001",
  "sent_at": "2026-09-16T10:00:00+08:00",
  "text": "请于9月20日18:00前报名，活动于9月23日14:00开始。",
  "attachments": []
}
```

`sent_at` 必须带时区，表示**原消息时间**。`message_id` 可省略，没有时用来源、会话、发送者、原时间和正文组成身份。相同正文在不同时间不是重复消息。同一身份出现不同内容会报冲突，不覆盖原文。网页上的来源选择仅是标记，不表示已接入该平台。

`attachments` 只支持文本元信息列表，当前不读取文件，也不访问正文链接。单次分析/问答上下文超过 40000 字会报错，不静默截断；对话最多选择 10 条通知，未选择时使用最新 10 条。完整单会话最多 200 轮，模型只读取最近 6 轮。

私有数据默认在 `data/`：`messages/*.json`、`tasks/*.json`、`memory/profile.md`、`memory/rules.md`、`memory/history/*.json`、`chat.json` 和 `usage.json`。任务记录保留用户要求、画像/规则快照、页面获取时间与受长度限制的正文、失败和筛选结果；可在网页查看保存的证据。可以备份整个目录；当前没有自动备份、删除或保留期限管理，任务最多100条。不要存放真实资料直到你接受该保存方式。

显式记忆命令和编辑立即生效，不自动重跑旧通知。修改错误记忆时可直接编辑网页内容再保存；完整旧/新文本保留在历史快照中。记忆不能改变发送或提交权限。本版尚不从普通对话自动推断画像。

不要把密码或 Key 放入画像、规则、通知和对话；这些内容会随真实分析发送至配置的模型服务。文件仅供当前 Linux 用户访问，但同机管理员仍可能读取，不能视为完全隔离。

## 测试与开发

```bash
.venv/bin/python -m unittest discover -s . -v
.venv/bin/python -m compileall -q assistant tests
git diff --check
```

单元测试使用临时目录和模拟模型，覆盖存储恢复、去重、格式校验、网页 API、记忆传递及权限边界。CI 配置为 Python 3.10 / 3.12；配置存在不代表远程 CI 已运行。

配置真实模型后可显式运行合成数据联调（约 3 次 API 尝试，产生调用费用）：

```bash
.venv/bin/python -m tests.live_check
```

报告及输入输出保存在被忽略的 `data/live-check/`。这是小样本功能检查，不是相关性评测，也不能单凭它断言纠正规则带来了效果提升。

浏览器验收需另行安装 Chromium，全部使用临时合成数据，无真实模型调用：

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m playwright install chromium --no-shell
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m tests.browser_smoke
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m tests.browser_task_smoke
```

浏览器验收用临时合成数据，不读取 `.env` 或调用 API。截图在 `data/browser-check/`，手机检查只是 Chromium 窄屏模拟，不等同于 iPhone Safari 实测。

经确认后可单独运行公开官网探测，不调用模型、登录或提交：

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m tests.site_probe
# 仅在明确选择公开 DNS 模式后追加 --dns google
# 排错可追加 --diagnostics；仅查列表路径可再追加 --listing-only
# --snapshot 将匿名页面截图存入忽略目录；--details 2 检查两份不同详情
```

2026-09-17 经确认，使用合成画像与匿名官网完成6次真实 `kimi-k2.6` 工具调用。模型提出导航/搜索动作，并返回读取失败说明及空岗位列表；任务为 `partial`，仅入口读取成功，没有岗位详情。证明工具协议和失败返回可运行，不证明完整官网流程或匹配质量通过；不能将空结果解释为没有合适岗位。78项单元测试及 Chromium 合成任务回归通过，均不代替真实官网验收。

后续不调用模型的官网探测曾完成入口、列表、算法搜索及两份不同详情，405经官网匿名初始化后恢复；再次新会话又出现脚本网络错误及入口加载失败。用户报告的 AI 岗位推荐推广（“稍后再试 / 立即体验”）未在匿名会话复现，不能作为超时根因。截图在 `data/site-probe/`，网站稳定性及自主动作/真实模型完整筛选仍待验证。

用户确认后，可运行独立联调（最多6次 API 尝试、只用合成画像，产生调用费用）：

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m tests.live_browser_check
# 只有同时确认可选公开 DNS 后才追加 --dns google
```

每轮新建 `data/live-browser-check/<随机标识>/`，不读取现有用户画像，不改动主工作区数据。可用 `--calls 1` 至 `--calls 6` 降低本轮上限；各轮累计尝试数仍必须遵守用户授权，不得重跑清零后超额调用。本次6次授权已用完，追加付费测试需重新确认。独立额度不计入主应用的 usage.json，不是服务商账单预算。

使用短期功能分支和小粒度本地提交；推送另行确认。`data/`、`.env`、`demand_list.md` 必须留在 Git 之外；只提交代码、模板、计划和合成样例。
