# 会成长的个人助手

单用户网页助手，使用 Python / Flask / 原生前端，数据保存为 JSON 和 Markdown 文件。需求和阶段安排见 [PLAN.md](PLAN.md)。

## 当前范围

- 网页手动输入或导入 JSON 通知，重复消息检查，原文查看。
- 结合画像与规则调用模型，提取事项、行动、截止/活动时间及不确定内容。
- 围绕选择的通知进行对话，也可以让模型撰写邮件**文本草稿**；没有邮件账户接入或发送功能。
- 编辑画像与规则，或在对话中使用 `/记住 信息`、`/规则 纠正`、`/查看画像`、`/查看规则`。
- 文件持久化、记忆历史与中断恢复、单实例写入保护、日 API 尝试次数限制。
- v0.2：按任务授权域操作真实 Chromium，自由文本检索、通用控件、失败重试/恢复、进度/取消、通用来源引用和私人分析；真实 Kimi 联调暴露引用与结果质量问题，尚未通过端到端验收。

Kimi 自行选择导航、点击、检索、筛选、翻页、恢复或报告停止，不再使用固定关键词、招聘路径、必经首页及失败 URL 永久禁令。权限与总预算仍由程序执行；不开放任意 Shell/Python/页面 JS。

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

通知分析与普通对话使用 Chat Completions JSON 输出，不发送 tools。只有明确发起的网页任务启用浏览器/证据工具；模型不能执行任意代码或授予自己写权限。Kimi 参数及工具调用参考 [API 文档](https://platform.kimi.com/docs/api/chat)，换模型时核对参数要求，不盲目沿用 `thinking` 或采样参数。

默认日尝试上限 30 次、单次输出上限 4096 token、超时 60 秒；可通过 `ASSISTANT_DAILY_CALLS`、`ASSISTANT_MAX_TOKENS`、`ASSISTANT_TIMEOUT` 修改。次数限制不是金额预算。失败尝试也计数，可能已被服务商计费；模型 API 失败没有自动重试，浏览动作失败可在任务预算内由模型选择恢复。额度按 `ASSISTANT_TIMEZONE`（默认 Asia/Shanghai）计算，重启不清零。

配置 Key 后也必须明确选择 `api` 模式。API 请求失败不会回退到演示模式。已有演示分析结果需点击“重新分析”才会使用真实模型。

另一个终端中的 `export` 不会改变已经运行的网页服务或 Agent 的环境。若要让 Agent 联调，推荐在本地被忽略的 `.env` 中配置，而不是在对话里提供凭据。

## v0.2 网页任务（通用核心已实现，真实结果质量待修正）

操作网页使用 Playwright 驱动真实 Chromium，不是让模型生成终端命令。首次安装浏览器后，在启动应用的同一终端设置其路径：

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m playwright install chromium --no-shell
export PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers
.venv/bin/python -m assistant
```

Linux 若缺少浏览器系统库，按 Playwright 提示安装；此操作可能需要管理员权限。浏览器二进制不进入 Git。通知/普通问答仍可在没有 Chromium 的情况下使用，只有浏览任务会提示启动失败。

Linux 的 Chromium 截图需要中文字体；本次检查环境没有中文字体，截图会显示方框，但 DOM 中文读取正常。开启视觉前先安装中文字体。通过 Windows 浏览器访问本地网页时使用客户端字体，不依赖 WSL 的截图字体。

在“网页任务”填写起始 HTTPS URL、额外访问域/资源域（可选）、可发送网站的公开查询说明，以及仅用于私人分析的查询要求。确认公开说明后模型可自由组织相关检索词，不逐词审批；不要将敏感信息放入公开说明。起始域自动授权，其他域不会因网页引用而自动加入。字节社招仍为首个场景，另一个真实站点已用 Python 官方文档验证只读导航；这不是任意网站都能安全操作的承诺。

默认每任务40次模型调用、120次动作、900秒，可用 `ASSISTANT_BROWSER_CALLS`、`ASSISTANT_BROWSER_STEPS`、`ASSISTANT_BROWSER_SECONDS` 调整。共用日额度，开始前显示有效模型预算（本任务上限与日剩余的较小值），不会自动增加日额度。原 `.env` 中的12/300等值仍会覆盖新默认，需自行更新并重启。移除前20页/20份详情限制；观察数量随总动作预算，正文保留受长度保护并支持分段，不宣称覆盖全站。

浏览工具包括观察、链接/URL导航、点击、hover、页面 Escape、自由搜索、滚动、返回、刷新、有限等待、按键、下拉选择、筛选勾选和匿名浏览器重启。普通界面按钮无需命中“关闭/稍后”白名单，仍不强制穿透遮挡；元素装饰性标签变化不会使整个引用失效。`observe_page` 的 `text_start`/`text_length` 支持正文窗口，`read_source` 返回指定位置的编号行。`finish_task` 的 findings/jobs 均可用行号由程序取原文，岗位标题自动取，画像原文引用可省略；旧格式仍兼容。

浏览阶段只接收公开说明和网页观察，不接收原始私人画像/规则或私人查询要求；`analyze_results` 切到私人分析，此时只有本地证据、结束和清除上下文后返回浏览的工具。所有阶段共用总预算；返回浏览不会带入私人分析对话。根据任务预算预留最多3次用于来源读取/收尾/纠正，不突破总额度，纠正也消耗调用；校验一次返回全部问题，编号引用要求对应行已提供给模型。`search_source` 可定位保存的关键词片段，避免猜行号。原文存在仍不保证支持结论，结果需人工复核。

失败返回交互/页面加载/网络/权限/参数等分类以及允许请求的网络错误和主动阻断记录。首入口失败不直接结束，失败 URL 可再次导航，由 Kimi 决定等待、刷新、换入口或停止；连续无进展会提示循环而非增加小的逐 URL 上限。必要资源持续不可达时可报告停止，不能把空白页当零结果。模型 API 本身失败不自动重试，由应用保存故障；取消/总预算仍由程序强制执行。

按需视觉截图需显式设置 `ASSISTANT_BROWSER_VISION=true`，仅提供匿名当前页且输入框遮罩，保存在忽略的 `data/screenshots/`。默认关闭；真实 Kimi 已接受一次图片+工具输入，但 DOM 同时提供了相关信息，不能据此认为视觉布局/遮挡判断已验收。

后台一次仅运行一个浏览任务，但浏览不再持有 Service 全局操作锁，期间可对话、导入、编辑记忆或分析通知；普通通知/对话之间原有串行约束仍保留，额度共用且原子预取，其他操作可能消耗任务开始时显示的日余量。取消不撤回已发起调用。进程中断后保留记录、不自动恢复；“重新发起”创建新任务、使用当前画像并重新计费，暂不原地续跑。

专用匿名会话校验导航、HTTP重定向、授权域和公网 DNS，禁止下载、私有表单填写、明显账号/业务写操作及未经授权的 POST。原型默认能力已放宽：访问域/资源域内的公开 GET/HEAD 数据读取可用，资源域仍不授权主页面导航或 POST；静态资源不再要求固定文件扩展名。字节公共查询端点保留大小/分页预算和明显写迹象检查，不要求固定门户参数、完整字段集合或 query/body 镜像；站点自己产生的签名原样传递，不生成签名或绕过验证。

其他只读 POST 可在任务表单“高级”中填写精确 HTTPS 端点，一次授权后接受正常查询字段，不由模型自行添加；只填写已确认用于读取的接口，不授权同时支持写入的混合接口。Cookie 提示由模型优先拒绝可选/仅必要，业务同意不一律放行。`ready` 表示正文快照可取得，`body_stable` 单独报告变化，不要求动态页面完全静止。域约束和写迹象检查不是完整的副作用证明；这版只适合匿名可信公开查询，不能当成任意网站或已登录账号的安全沙箱。

Chromium 明确直连，不继承 WSL 的 HTTP_PROXY；不修改系统代理、路由或自动切换 DNS。系统 DNS 默认，遇到私网/Fake-IP会拒绝；可显式选择 `ASSISTANT_BROWSER_DNS=google` 使用固定的 [Google DNS-over-HTTPS](https://developers.google.com/speed/public-dns/docs/doh/json)，只发送授权公开域名。最新系统 DNS 曾返回公网且读取成功，也复现过必要 JS 分块网络不可达；不能将之前 Fake-IP 或 ERR_NETWORK_CHANGED 自动归因于代理切换，换 DNS 不是稳定性保证。

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

私有数据默认在 `data/`：`messages/*.json`、`tasks/*.json`、`memory/profile.md`、`memory/rules.md`、`memory/history/*.json`、`chat.json` 和 `usage.json`。任务记录保留用户要求、画像/规则快照、动作、页面获取时间与受长度限制的正文、验证状态、失败和筛选结果；可在网页查看保存的证据。可以备份整个目录；当前没有自动备份、删除或保留期限管理，任务最多100条。不要存放真实资料直到你接受该保存方式。

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
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m tests.browser_generic_smoke
```

浏览器验收用临时合成数据，不读取 `.env` 或调用 API。截图在 `data/browser-check/`，手机检查只是 Chromium 窄屏模拟，不等同于 iPhone Safari 实测。

经确认后可单独运行公开官网探测，不调用模型、登录或提交：

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m tests.site_probe
# 仅在明确选择公开 DNS 模式后追加 --dns google
# 排错可追加 --diagnostics；仅查列表路径可再追加 --listing-only
# --snapshot 将匿名页面截图存入忽略目录；--details 2 检查两份不同详情
# --actions 用真实搜索框/Enter及第二页控件检查动作，而不是仅拼接链接
```

本轮仅补6项关键单元回归，现有127项通过；真实 Chromium 通用控件/公开 SPA/动态正文及任务网页回归通过，网页能显示合成结果、原文与动作记录。浏览器缓存已重新安装到上述路径。合成模型不证明真实 Kimi 已完成任务。实际 Python 官方文档读取通过；字节官网此前观察/详情失败，最终一次搜索框输入→第二页→一份详情完整只读检查通过，不代表稳定性。验收优先检查完整真实任务及引用/结论质量，不以测试数量替代实际效果。

历史6次 Kimi 联调仅入口成功；随后另轮8次验证合成推广自主关闭、官网检索/两份详情及独立证据复核，原浏览任务未完整通过。此前授权20次 `kimi-k2.6` 调用已全部使用：

- 字节任务12次：自由组合检索、第二页及两份详情读取成功；三次收尾引用依次失败，任务 partial、无发布结果。
- Python 官方文档3次：机械 completed，但 urljoin 结论引用了 bytes 解析结果类型段落，人工复核不通过。
- 恢复场景3次：重启导致测试脚本重新注入首次故障，样本无效；脚本已修正，未付费复跑。最后一轮只有结束工具，不证明自主提前停止；模型也过度推断 DNS 根因。
- 匿名视觉2次：真实图片+工具接口单样本接受，不代表视觉理解已验收。

本轮完整合成画像仅出现在无网站动作的私人分析输入，公开浏览输入不含该画像。主应用数据/额度未改变，无真实账号或提交。原始输入输出与独立人工复核 `audit-report.json` 留在私有联调目录；不修改原任务状态来掩盖质量问题。后续采纳项及修正顺序见 `PLAN.md` 的 v0.2 H 节。

2026-09-18 用户委托助手决定新测试量，实际85次后停止。真实招聘任务自主恢复、组合搜索/第二页/三份详情/分析收尾完成；定向两份详情验证 `title_line` 实际岗位名称，文档6次样本中文解释且引用相关，持续故障5次样本提前停止并明确根因未知。修复来源工具重复附整页 DOM 导致上下文裁剪循环，来源读取现在只返回证据片段，裁剪保留最近5个片段；补免费 Chromium 启动预检，最后一轮仅收尾。仍存在“会Python”被夸大成满足“熟练Python”、替代条件解释及部分引用范围不足，不作为可靠匹配或稳定性验收；详细失败/调用记录见 `PLAN.md` J 节。

获得付费测试授权后可运行独立联调（必须显式传技术保护预算 `--calls`，只用合成画像，产生调用费用）：

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m tests.live_browser_check --calls 12
# 只有同时确认可选公开 DNS 后才追加 --dns google
# --popup-check 将其中1次用于合成推广/真实 Kimi/真实 Chromium，不额外增加额度
# --review-id <已有合成测试标识> 仅独立复核保存证据，--calls 表示该轮累计总额度
# --scenario docs/recover/network-stop/vision 选择一个场景，默认 jobs
# --run-id <已有标识> 跨场景复用本轮目录和累计额度；不能自行提高 --calls
# --task-calls <次数> 仅限制该场景，仍受本轮累计总额度约束
# --job-urls <详情URL1> <详情URL2> 定向验证两份已观察公开详情，不重复搜索/翻页
```

每轮新建 `data/live-browser-check/<随机标识>/`，不读取现有用户画像，不改动主工作区数据；复用/复核累计额度，不原地恢复浏览任务。私有 `.paid-budget.json` 在 API 请求前累计尝试，跨日期仍保留，复用目录不允许提高上限；CLI 额度是保护措施，不是新的用户授权，不得重跑清零后超出用户授权。历史20次账本已耗尽且不变；本轮用户授权自行决定测试量，生产任务/日预算不因此取消。独立额度不计入主应用的 usage.json，不是服务商账单预算。故障/视觉场景使用真实 Chromium 与本地模拟网页，只访问真实模型 API；机械 completed 仍需人工核查，预算强制结束不算自主提前停止。

使用短期功能分支和小粒度本地提交；推送另行确认。`data/`、`.env`、`demand_list.md` 必须留在 Git 之外；只提交代码、模板、计划和合成样例。
