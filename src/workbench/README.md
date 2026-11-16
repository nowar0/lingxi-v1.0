# 零汐workbench

使用 React 18.3.1、xterm.js 6.0.0、FitAddon 0.11.0；构建器 esbuild 0.25.12。`package-lock.json` 锁定依赖。

首次修改前执行 `npm ci`，然后运行 `npm run build`。在交付源码中，输出到相邻的 `app/static/ui/assets/workbench`；在本次独立工作区中，输出到 `../project/app/static/ui/assets/workbench`。

`src` 是零汐适配层，`vendor` 保留并适配 GitHub 项目的 React 组件和 CSS。没有装入 DSH Host/profile 运行时，也没有开放手机配对、独立后台、Issue→PR、工坊安装或 PTC。

构建后需同步运行版 `零汐/resources/backend/_internal/app/static/ui/assets/workbench`；如修改 Python，还需使用源码中的packaging重建后端。不要把 node_modules 放进发行目录。

来源与许可见编译输出目录中的 `THIRD_PARTY_NOTICES.md` 及 LICENSE 文件。部分实际端口包括主机密钥核对、Windows 系统凭据、工作会话绑定、明确的模型证据确认；外部操作必须由使用者在界面发起。
