# 浏览器任务镜像

从项目根目录构建：

```sh
docker build -f deploy/browser/Dockerfile -t personal-assistant-browser:1.63.0 .
```

应用只运行已有镜像（`--pull=never`），不自动拉取、构建或切换到local后端。配置见 `docs/browser-runtime.md`。本轮按用户要求没有执行Docker构建或容器测试。

`seccomp_profile.json` 原样取自 Playwright v1.63.0：
https://github.com/microsoft/playwright/blob/v1.63.0/utils/docker/seccomp_profile.json

SHA256：`cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`。

同目录 `Playwright-LICENSE` 保留官方Apache-2.0许可证。镜像与Python包版本匹配项目当前Playwright 1.63.0。升级时同时更新镜像、依赖、seccomp配置及测试；不使用seccomp=unconfined或关闭Chromium沙箱作为自动回退。
