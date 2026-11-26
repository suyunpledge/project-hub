# Project Hub · 本机项目仓库

把本机散落的项目登记到一个地方：按分组管理相关项目、浏览源码和分支、提交到指定分支、
一键用 VS Code / 资源管理器 / 终端打开、运行并实时检查报错；授权后一键发布到 GitHub，
Agent 也能通过 MCP 直接建仓、提交和推送。只用 Python 标准库，无需安装依赖。

## 安装

1. 打开 [Releases](https://github.com/suyunpledge/project-hub/releases/latest)，下载 **ProjectHub-Setup-x.y.z.exe**。
2. 双击运行，点「安装」。安装到当前用户，**不需要管理员权限**，自带 Python 运行时。
3. 从桌面或开始菜单打开 **Project Hub**。

- 需要已安装 [Git](https://git-scm.com/download/win)（分支、提交、推送都依赖它）。
- 已登记的项目保存在 `%LOCALAPPDATA%\ProjectHub`，重装或升级不会丢失；在「应用和功能」里可卸载。
- 安装时勾选「添加 hub 命令」后，新开的终端里可直接运行 `hub find …`、`hub commit …`。
- Windows SmartScreen 可能提示「未知发布者」（安装包没有商业代码签名）：点「更多信息 → 仍要运行」。
  每个 Release 附带 `.sha256` 文件，可用 `certutil -hashfile ProjectHub-Setup-x.y.z.exe SHA256` 核对。

### 从源码运行

```
python hub.py desktop          # 桌面窗口（Edge 应用模式；装了 pywebview 则用原生窗口）
python hub.py serve --open     # 只用浏览器：http://127.0.0.1:8790
python hub.py install-shortcuts
```

自己打包安装程序：`python installer/build.py --download`，产物在 `dist/`。
推送 `v*` 标签时，GitHub Actions 会运行测试、打包并发布到 Releases（见 `.github/workflows/release.yml`）。

## 功能

| 位置 | 能做什么 |
|---|---|
| 总览 | 所有项目的分支、未提交改动、待推送/落后、最近提交、远端一览；按状态筛选 |
| 搜索（Ctrl K） | 按名称/标签/备注相关度排序，支持模糊匹配；Tab 切到**跨项目代码搜索**，回车直接打开到那一行 |
| 侧栏 | 分组、置顶、拖动换组；Ctrl/Shift 多选后批量移组、加标签、置顶 |
| 源码 | 文件树 + 语法高亮；查看工作区或任一分支；直接编辑保存 |
| 变更 | 逐文件 diff，勾选文件提交到当前或**任意其他分支**（不切换工作区） |
| 历史 | 按分支看提交记录与 diff |
| 运行 / 问题 | 运行命令、实时输出、解析报错位置；保存即检查 Python/JS/JSON |
| GitHub | 授权后一键「发布到 GitHub」（新建仓库 + 推送）；设置 Agent 可做的事 |

目录被移动时，项目页会提示「重新定位」；`hub prune` 清理已失效的登记。
登记文件损坏时自动从备份恢复（坏文件另存为 `hub.corrupt-*.json`）。

## GitHub 与 MCP

在侧栏点 **GitHub** 授权，二选一：

- **复用 Git 已登录的账号**：直接使用 Git Credential Manager 里的 GitHub 登录，Hub 不保存令牌；
- **个人访问令牌**：需要 `repo` 权限（或细粒度令牌的 Administration + Contents 读写），
  用 Windows DPAPI 加密保存，只有当前 Windows 用户能解密。

授权时可分别允许 Agent「新建仓库」和「推送」，新仓库默认私有。

在 Agent 中注册 MCP 服务器（GitHub 对话框里可一键复制）：

```
claude mcp add project-hub -- python C:\path\to\project-hub\hub.py mcp     # Claude Code
hub mcp-config                                                              # 打印 Codex / 通用 JSON 配置
```

MCP 工具：`hub_search` `hub_grep` `hub_overview` `hub_info` `hub_status` `hub_commit` `hub_check`
`hub_run` `hub_push` `github_status` `github_create_repo` `github_publish_project`。
命令行用法见 [AGENTS.md](AGENTS.md)。

## 安全

- 服务只监听 127.0.0.1，并校验 Host 头和 `X-Hub` 请求头，其他网站无法调用；项目预览在单独端口，与管理接口隔离。
- 推送时令牌通过环境变量交给 Git，不出现在命令行或进程列表中；错误信息里的地址会去掉凭据。
- Agent 只能做用户在 GitHub 设置中授权的操作；断开授权即刻生效。
