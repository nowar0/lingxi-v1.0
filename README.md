# 零汐 V1.0

Windows x64 · Electron 桌面应用。此仓库保存 2026-11-16 当前维护源码；可运行程序和原始压缩包放在 Releases。

## 下载

- [最新完整安装版](https://github.com/nowar0/lingxi-v1.0/releases/tag/v1.0.0-updated-20261116)：与本次指定解压安装目录逐文件一致，解压后运行 `Lingxi.exe`。
- [原目录五个 ZIP 归档](https://github.com/nowar0/lingxi-v1.0/releases/tag/archive-20261116)：按原文件原样保存。它们早于后续界面、上下文和手动工作流更新，不能当作最新源码的对应构建。

程序使用 `%APPDATA%\LingxiV1` 保存用户数据，不自动读取旧版 `%APPDATA%\Lingxi`。完整包包含既有模型和运行组件；不需要 WebView2。

## 源码结构

| 路径 | 内容 |
| --- | --- |
| `src/app` | Python 后端、任务和智能体、记忆、模型服务、工具与权限等 |
| `src/frontend` | React / TypeScript 主界面 |
| `src/workbench` | 工作区内嵌工具及保留许可证的第三方适配源码 |
| `src/packaging/shell` | Electron 桌面启动、托盘及窗口代码 |
| `src/packaging` | 冻结后端、桌面与运行组件的构建工具 |
| `docs` | 能力手册与本轮更新说明 |

仓库不保存模型权重、安装 EXE、构建依赖、用户数据库、Key、日志或测试运行产生的资料。必要的内置图片属于项目资源，保留在前端源码中。

## 源码开发

需要 Windows、Python 3.14、uv，以及 Node.js 22 或以上版本。

先构建主界面和工作台，再启动后端：

```powershell
cd src
uv sync --frozen
cd frontend
npm ci
npm run build
New-Item -ItemType Directory -Path ..\app\static\ui -Force
Copy-Item -Path dist\* -Destination ..\app\static\ui -Recurse -Force
cd ..\workbench
npm ci
npm run build
cd ..
uv run python launch.py
```

这组命令用于源码开发。完整 Electron 桌面程序、媒体工具和随包模型请使用最新 Release；各本地功能仍受相应运行组件与模型是否齐全的限制。桌面打包工具需要开发包的完整运行资源，只有源码仓库不等于已经具备全部离线打包输入。

前端回归：在 `src/frontend` 执行 `npm test`。源码维护版本此前完成 348 项前端、62 项后端与智能体、47 项实际 EXE 验收；这些是具体构建与隔离环境的结果，不表示客户真实账号、额度和外部服务效果已验收。

## 使用说明与本轮变化

- [能力和使用说明](docs/零汐V1.0能力和使用说明.md)
- [上下文设置、手动工作流与重点问题说明](docs/本轮优化与重点问题说明.md)

本轮包含上下文设置集中、模型容量预算、记忆常用操作、设置搜索与默认值预览、智能体手动接线，以及保持原有黑白风格的界面修正。任务节点生成结束后，整体仍进入待验收，不能只凭模型声称完成而直接接收。

## 第三方组件

`src/workbench/vendor` 等目录保留原有许可证。Release 内的 Electron、Chromium、FFmpeg 和其他依赖保留对应许可证文件。此处不将第三方许可证自动解释为整个零汐项目的授权条款。
