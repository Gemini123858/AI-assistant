# 会成长的个人助手

单用户网页助手，使用 Python / Flask / 原生前端，数据保存为 JSON 和 Markdown 文件。需求和阶段安排见 [PLAN.md](PLAN.md)。

## 当前范围

- 网页手动输入或导入 JSON 通知，重复消息检查，原文查看。
- 结合画像与规则调用模型，提取事项、行动、截止/活动时间及不确定内容。
- 围绕选择的通知进行对话，也可以让模型撰写邮件**文本草稿**；没有邮件账户接入或发送功能。
- 编辑画像与规则，或在对话中使用 `/记住 信息`、`/规则 纠正`、`/查看画像`、`/查看规则`。
- 文件持久化、记忆历史与中断恢复、单实例写入保护、日 API 尝试次数限制。

尚未实现：多步骤网页查询、邮箱同步、附件识别、实时/定时主动推送、普通对话自动提取画像、授权邮件发送、远程认证部署。QQ/微信自动接入已取消，网盘与聊天备份暂缓；后续权限遵循自动读取、手动分析、按需草稿、写操作单独确认。网页仅本机访问，不代表已能在手机上远程使用。

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

客户端使用 Chat Completions JSON 输出，不发送 tools。Kimi K2.6 的非思考设置及参数说明见 [官方指南](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart)，JSON 格式见 [API 文档](https://platform.kimi.com/docs/api/chat)。换模型时核对其参数要求，不盲目沿用 `thinking` 或采样参数。

默认日尝试上限 30 次、单次输出上限 4096 token、超时 60 秒；可通过 `ASSISTANT_DAILY_CALLS`、`ASSISTANT_MAX_TOKENS`、`ASSISTANT_TIMEOUT` 修改。次数限制不是金额预算。失败尝试也计数，可能已被服务商计费；没有自动重试。额度按 `ASSISTANT_TIMEZONE`（默认 Asia/Shanghai）计算，重启不清零。

配置 Key 后也必须明确选择 `api` 模式。API 请求失败不会回退到演示模式。已有演示分析结果需点击“重新分析”才会使用真实模型。

另一个终端中的 `export` 不会改变已经运行的网页服务或 Agent 的环境。若要让 Agent 联调，推荐在本地被忽略的 `.env` 中配置，而不是在对话里提供凭据。

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

私有数据默认在 `data/`：`messages/*.json`、`memory/profile.md`、`memory/rules.md`、`memory/history/*.json`、`chat.json` 和 `usage.json`。可以备份整个目录；当前没有自动备份、删除或保留期限管理。不要存放真实资料直到你接受该保存方式。

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

可选浏览器验收，不属于运行依赖：

```bash
.venv/bin/python -m pip install playwright
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m playwright install chromium --no-shell
PLAYWRIGHT_BROWSERS_PATH=/tmp/personal-assistant-browsers .venv/bin/python -m tests.browser_smoke
```

浏览器验收用临时合成数据，不读取 `.env` 或调用 API。截图在 `data/browser-check/`，手机检查只是 Chromium 窄屏模拟，不等同于 iPhone Safari 实测。

使用短期功能分支和小粒度本地提交；推送另行确认。`data/`、`.env`、`demand_list.md` 必须留在 Git 之外；只提交代码、模板、计划和合成样例。
