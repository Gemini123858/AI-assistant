# 持久 Python 网页子代理：实现与验证

2026-11-03；分支 `feat/browser-runtime`，承接 `fix/browser-interaction`。用户已确认采用原生Playwright代码执行与视觉观察；服务器已能运行rootless Docker。本轮只在本地直接运行worker测试，不运行Docker，不接真实模型API，不合入main。

## 当前实现

主助手仍委派独立网页任务。默认任务执行器改为 `BrowserRuntime`：每任务一个持久Python进程，提供 `browser`、`context`、`page`、`asyncio`，支持顶层await，变量在调用间保留。没有Python语法白名单；运行隔离由Docker承担，不是通过删builtins实现。

模型主要使用 `browser_run(code, timeout=30, observe=true)`；一次代码允许组合操作。默认随后单次观察当前page：1440×1000视口PNG截图、可见正文、标题、URL、标签列表。没有旧候选枚举或全页稳定循环；模型可使用原生定位、坐标鼠标、键盘、页面JS和自定义程序。新标签通过context.pages访问，改变观察目标需赋值page。

执行与观察独立报告：代码异常前的修改仍可能生效，异常不自动重放；正文读取、截图、标题分别获取，某一通道失败不丢弃其他通道。`observe=false`用于纯计算或读取日志，不生成新页面证据。截图通过图片content发送给模型，非仅发送文件路径；不统一遮住匿名页的公开搜索框。

标准输出和错误输出分开捕获，包括普通子进程输出。工具先返回各8000字符，完整已保存部分可通过 `read_browser_output(step, stream, start, characters)` 分页补读。每个流最多保存100万字符，明确标记截断并提供worker内日志路径；任务仍运行时可用Python按需读取文件后续片段。容器/本地会话销毁后不保留超出保存上限的尾部日志。截图、错误和代码输出都是不可信资料。

代码默认30秒、模型可申请1～120秒；异步超时尝试取消本段并返回异常及观察。不响应异步取消的CPU循环或卡住的观察，由父进程在本段上限加25秒后销毁会话，明确部分操作可能已经执行。任务取消每约200ms由父进程检查并清理，不依赖生成代码配合。显式restart_browser才创建新会话，旧变量不会恢复。任务结束清理进程/容器，不在应用重启后恢复交互进程。

截图与页面文本绑定当次观察，动态配置变化不会因正文相同而合并为旧观察。保留原有正文编号行引用；findings支持 `evidence_type=screenshot`、page_id和说明，必须确有已发送截图，不伪造行号。UI展示代码、stdout/stderr、执行异常、观察问题和当步截图；截图通过原有登录保护下的同源PNG接口提供。旧任务JSON、资料引用和清理脚本继续可用，未批量迁移数据。

系统提示词仅说明持久变量、异步语法、观察渠道、错误恢复、证据与授权，不规定网站流程。模型与主助手复用当前API配置；需支持图片和工具调用，多模态错误不静默退回纯文本。最近两张截图保留在模型上下文，其余仅在本地任务记录中保留。

## 两种后端

### Docker：默认部署后端

```dotenv
ASSISTANT_BROWSER_BACKEND=docker
ASSISTANT_BROWSER_IMAGE=personal-assistant-browser:1.63.0
ASSISTANT_BROWSER_SECCOMP=deploy/browser/seccomp_profile.json
```

请在项目根目录启动应用；seccomp配置可使用绝对路径。镜像构建命令见 `deploy/browser/README.md`。Docker CLI沿用当前用户的rootless context/DOCKER_HOST，不使用sudo。不在任务执行中自动安装或拉取镜像。

每任务 `docker run --rm --init`，非root UID/GID，根文件系统只读、drop全部capabilities、no-new-privileges，使用Playwright官方seccomp并开启Chromium sandbox。不挂载源码、data、.env、宿主机HOME或Docker socket；模型/邮箱凭据不进入容器。临时文件写/tmp。当前资源初值为2CPU、2GB内存、512进程、512MB共享内存和512MB临时目录；这些是待容器实测调整的初值，不是性能结论。

网络使用 `--network=none`，仅有容器内部loopback。唯一宿主机挂载是本任务的Unix套接字目录（只读挂载），其中不包含应用文件。容器内loopback HTTP代理将连接转发到该套接字；套接字在容器外连接公网HTTPS网关。代理环境变量不是隔离边界，移除代理后仍没有普通出网路径。

网关只接受CONNECT 443，宿主机解析并校验公网IP，再连接已校验的字面IP；拒绝内网、loopback、链路本地和元数据地址，不将TLS解密交给网关。限定域任务检查授权域及资源域的并集；公开任务不增加站点白名单。`ASSISTANT_BROWSER_PROXY`仅在宿主机网关侧用作可选HTTP上游，TLS仍在浏览器与网站之间。受限域模式不区分“文档导航域”和“资源域”，资源域也是可连的主机，不能声称更细的网络隔离。

为兼容rootless UID映射，套接字目录可遍历、套接字可连接；它只提供相同的公网HTTPS出口，不提供应用RPC或数据读取。容器不能在只读挂载中替换该套接字。生成代码能使用这个出口访问公网，但不能通过它指定宿主机地址。

容器限制宿主机访问，不保证公网业务只读：匿名GET也可能有业务副作用，代码能控制网页。仅公开查询、禁止登录/下单/发送等继续作为明确任务授权。未声称网络层能识别全部业务写入。旧版路径级blocked_urls/read_exceptions不能由TLS隧道执行，新代码任务收到此类限制会明确拒绝，不静默忽略。

### Local：开发后端，本轮验证使用

```dotenv
ASSISTANT_BROWSER_BACKEND=local
```

本地安装Playwright/Chromium后使用与容器相同的worker和消息协议，不经过旧JobBrowser。每任务独立进程及临时工作目录，不传递API/邮箱环境变量，结束后清理临时目录。

**local不是沙箱**：代码仍有运行用户的文件与网络权限，不提供容器网络隔离，`ASSISTANT_BROWSER_PROXY`也不自动代理local worker。仅用于可信开发验证，不能据此声称阻止了访问宿主机。默认docker失败时不会回退local。开发代码使用当前系统权限，所以不会对真实Kimi调用自动启用local；需用户显式设置。

`ASSISTANT_BROWSER_VISION`仅保留旧DOM执行器兼容；新代码任务始终自动发送截图。旧HTTP追问兼容入口目前仍使用原DOM流程，统一聊天发起/补查的网页任务使用新代码流程；本轮没有把隐藏兼容入口重写为第二套代码代理。

## 验证与剩余验收

本轮实际结果：245项离线单测通过，`tests.browser_python_smoke`本地Chromium完整流程通过，既有 `tests.browser_conversation_smoke` 统一入口回归通过。测试未访问真实账号、网页或模型API，未启动Docker。

- 离线单测覆盖模型工具契约、图片传输、执行失败仍返回截图、输出分页、视觉证据校验、截图鉴权，以及网关拒绝内部地址/超域目标。
- `tests.browser_python_smoke` 使用真实本地Chromium与合成网页，验证持久变量、配置选择与延迟价格、截图、异常后继续、OS/子进程输出、异步超时、完整任务循环和CPU循环取消。没有真实账号、外部网页、模型API或Docker。
- 用户随后验证实际模型能否理解截图、生成原生Playwright并稳定恢复；不能把脚本模型测试称为真实Kimi效果验证。
- Docker镜像构建、rootless下Unix套接字、Chromium sandbox、资源限制、DNS/HTTPS连通、直接访问宿主机/内网失败及任务取消后容器销毁，均留待下一轮实际验证。当前只有启动参数与网关逻辑的离线检查，尚不能宣称容器隔离验收通过。

运行本轮流程：

```sh
.venv/bin/python -m unittest discover -q
PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-cache" .venv/bin/python -m tests.browser_python_smoke
```
