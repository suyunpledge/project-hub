# Project Hub — 给 Agent 的操作说明

Project Hub 是本机项目的登记处。每个项目仍留在它原来的目录、用它自己的 git 仓库；
Hub 负责登记、分组、按分支提交、运行和检查。所有命令都在本目录下执行：

```
cd <project-hub 目录>
python hub.py <命令> [--json]
```

`<项目>` 可以写 id、显示名或目录路径。加 `--json` 得到机器可读输出。
退出码：`0` 成功 / 无错误，`1` 检查或运行发现错误，`2` 用法或 git 错误。

## 1. 找到项目

```
python hub.py list                    # [分组] id  路径
python hub.py list --ungrouped        # 还没分组的项目
python hub.py list --group "分组名"
python hub.py find 接口 --group "后台服务" --kind python --json
python hub.py find --tag 发布 --tag 客户端 --json
python hub.py find --missing --json    # 找到目录已失效的登记
python hub.py status <项目>            # 当前分支 + 未提交改动
python hub.py add <目录> --group "分组名"   # 登记新项目（分组不存在会自动创建）
python hub.py scan <用户主目录> --add    # 扫描目录（向下 2 层），把找到的项目都登记为未分组
```

查找会匹配项目 ID、显示名、路径、备注、分组和标签，多个关键词须全部命中。
JSON 数组按匹配结果返回，空数组表示没有匹配。按分组和路径选好项目后，把结果中的
`ref`（例如 `id:订单服务-2`）原样传给 `tag`、`commit`、`push`。普通名称或 ID
匹配到多个项目时，退出码为 2，JSON 中的 `candidates` 列出每个项目的引用、分组和路径。
标签可跨分组使用，支持中文，英文字母忽略大小写。

```
python hub.py tag <项目> --add 发布 --add 客户端 --json
python hub.py tag <项目> --remove 发布
python hub.py tag <项目> --json          # 查看当前标签
```

## 1.5 给项目分类（分组）

相关的项目放进同一个分组；桌面应用每 5 秒刷新，分好的组会直接出现在侧栏。

```
python hub.py groups                          # 现有分组和项目数
python hub.py info <项目>                      # 类型、README 开头、最近 5 次提交 —— 判断归属用
python hub.py group "分组名" <项目> <项目> ... --desc "这一组是什么" [--color "#10b981"]
python hub.py group-edit "分组名" --rename "新名" | --desc "说明" | --color "#hex" | --delete
python hub.py ungroup <项目> ...               # 移回「未分组」
```

- `group` 分组不存在就创建，已存在就把项目移进去；一个项目只属于一个分组。
- 删除分组不会删除项目，只把它们移回「未分组」。
- 多个 Agent 可以同时执行这些命令：登记文件有跨进程锁，不会互相覆盖。
- 分类只改登记信息（`data/hub.json`），不会移动或修改任何项目文件。

## 2. 提交到对应分支

```
python hub.py commit <项目> -b <分支> -m "说明" [文件 ...]
```

- `-b` 省略 → 提交到当前检出的分支（普通 `git add` + `git commit`）。
- `-b` 是**其他分支** → 把工作区里这些文件的当前内容叠加到该分支最新提交上，
  **不切换工作区、不影响用户正在看的分支**。分支不存在时从 `HEAD`（或 `--base <ref>`）新建。
- 不列文件 = 整个工作区快照（遵守 .gitignore）。建议 Agent 总是列出自己改过的文件。
- 和目标分支相比没有变化时会拒绝（退出码 2）。
- `--author "Name <email>"` 可标注作者。
- 目标分支若在另一个 git worktree 中检出，会被拒绝——请到那个 worktree 里提交。

推荐分支命名：`agent/<任务>`，用户在 Hub 网页的「历史」页按分支查看、合并。

## 2.1 推送已提交的分支

```
python hub.py info <项目> --json
python hub.py push <项目> -b agent/feature-x --remote origin --dry-run --json
python hub.py push <项目> -b agent/feature-x --remote origin --preview-id <上一步的previewId> --json
```

`info` 的 `remoteTargets` 列出每个远端的推送地址。即使两个远端名称都有效，也可据此区分测试库和发布库。
`--dry-run` 连接远端进行 Git 预演，返回的 `pushUrls`、`target` 和 `sha` 可用于核对。
`remoteTips` 按推送地址的顺序列出目标分支的提交，`null` 表示分支尚不存在。
正式推送时传回 `previewId`，可以确保多个 Agent 使用各自核对过的结果。每个项目保留最近一次成功预演，完成推送后消费该编号。
同一项目的预演和正式推送按顺序执行。项目有待推送的预演时，必须传入自己收到的编号。
省略编号会得到 `preview_id_required`。项目空闲且没有待执行预演时，仍可直接推送。
遇到 `push_busy`，请等当前操作结束后用原参数重试。
断线或 Agent 退出后，请保留原命令及 `--preview-id` 重试。正式推送开始前，Hub 先在本机登记本次尝试。
Agent 退出后，仍在执行写入的本地 Git 进程继续持有项目锁，Git 退出时系统自动释放锁。
上次结果待确认时，重试会先只读查询每个实际推送地址的目标分支：

- 所有目标都指向预演 SHA 时，Hub 补记成功回执，返回退出码 0 和 `recovered=true`。本次不发送推送请求，再用同一编号可得到 `preview_already_pushed`。
- 所有目标仍处于预演时的位置时，Hub 重新核对本地分支和远端状态，再尝试原推送。
- 无法读取目标时返回 `push_outcome_unknown`，恢复读取权限或连接后可用原编号重试。
- 仅部分地址达到目标时返回 `push_partially_applied`。逐个查看 `destinations` 中的 `url`、`status` 和失败原因 `error`。用原编号核对时，`remoteTips` 与 `pushUrls` 一一对应。出现其他提交时返回 `preview_stale`。

如果另一个 Agent 已经完成新预演，旧编号返回 `preview_stale` 和 `reason=superseded`，需要重新预演。
如果这个编号已经成功推送，重复调用返回退出码 2 和 `preview_already_pushed`，附上原推送的 SHA、远端及分支。Agent 可据此结束本次任务。
每个项目保存最近 64 个已替代或已成功推送的编号。查不到的编号返回 `preview_stale` 和 `reason=unknown`，需要重新预演。
本地提交、推送地址或远端目标分支在预演后变化时，CLI 返回退出码 2 和 `code=preview_stale`。
`changes` 用 `local_sha`、`push_urls`、`remote_tip` 标记以上变化，用 `preview_id` 标记编号失效。核对改动后重新运行 `--dry-run`，再用新编号推送。
一个远端配置多个推送地址时，预演会先读取各地址的目标分支。`at_target` 表示该地址已是目标 SHA，`pending` 表示还需推送。
正式推送成功的地址标为 `pushed`，Git 明确拒绝的标为 `rejected`。无法确认结果时标为 `unknown`，其余可读取地址仍分别列出。遇到竞争时，`changed` 标出发生变化的地址，`not_attempted` 标出中止后尚未推送的地址。
修复拒收原因后，用相同的项目、分支和远端重新运行 `--dry-run`，再使用新 `previewId`。已是目标 SHA 的地址只接受只读核对，预演和正式补推都跳过其推送连接。
补推前，Hub 会把所有地址（包括准备跳过的地址）与这次预演逐一比对。已成功的地址被回退，或待补推地址被别人先推，即使对方推的是同一提交，也会返回退出码 2 和 `preview_stale`，提示重新预演。查看 `destinations` 中标为 `changed` 的行，`url` 是变化的地址，`previewTip` 是预演时的 SHA，`remoteTip` 是最近读到的 SHA。值为 `null` 表示当时分支不存在。
如果变化发生在逐个写入期间，已写入的地址保留 `pushed` 状态，剩余未执行的地址标为 `not_attempted`。跨地址的推送按顺序执行。
多地址正式推送遇到服务端拒绝时，Hub 会只读复查该地址。即使 Git 只给出通用拒绝原因，只要目标分支已偏离预演，Hub 就停止后续写入并返回 `preview_stale`。连接中断且无法确认写入结果时，也会暂停后续地址，保留 `unknown` 状态供核对。重新预演时，先前已写入且仍指向目标 SHA 的地址会显示为 `at_target`。
如果 A 推送成功、B 的推送回执中断，Hub 会暂停 C 并只读检查 B。查到目标 SHA 时，B 显示 `at_target` 和 `verifiedAfterDisconnect=true`。若 B 仍离线，恢复连接后用原编号重试。在 A、B 都已到位而 C 仍待推的情况下，原编号返回 `push_partially_applied`（退出码 2），本次只做核对。重新预演并传回新编号后，只向 C 发送推送。
省略 `-b` 时使用当前分支，目标默认为同名分支，可用 `--to <远端分支>` 指定。
省略 `--remote` 时依次采用分支的 `pushRemote`、`remote.pushDefault`、分支的 `remote`，
再选 `origin` 或唯一远端。结果包含本地分支、远端、目标分支和完整提交 SHA。
推送采用普通 Git 快进规则；远端拒绝时命令返回退出码 2。
远端名拼错时，读取 `code=unknown_remote` 和 `availableRemotes`。
遇到 `non_fast_forward` 时，先获取目标分支、比较并整合历史，再重新预演。
其他拒绝或连接失败的说明在 `error` 和 `hint` 中。服务端钩子也可能在正式推送时拒绝更新。

## 3. 检查是否报错

```
python hub.py check <项目>            # 内置语法检查 + 项目配置的检查命令
python hub.py check <项目> --no-command   # 只做快速内置检查
```

内置检查：Python（装了 ruff 用 ruff，否则 `compile`）、JS（`node --check`）、JSON。
项目的「检查命令」（如 `npx tsc --noEmit`、`pytest -x -q`）输出中的 `文件:行` 会被解析成问题。

## 4. 运行并捕获报错

```
python hub.py run <项目> --timeout 20
python hub.py run <项目> --cmd "python -m pytest -x -q" --timeout 120
```

运行配置的命令（或 `--cmd`），最多 `--timeout` 秒。服务类程序到时仍在运行且没报错算成功
（到时会被停止）。会解析 Python Traceback、`file:line:col`、`file(line,col)` 等格式。

## 5. 修改项目配置

```
python hub.py set <项目> run "npm run dev"
python hub.py set <项目> check "npx tsc --noEmit"
python hub.py group "分组名" <项目> <项目> ...
```

## 规则

- 只提交你自己改过的文件；不要用 Hub 去改别的项目。
- 用户要求实际推送时使用 `push`，核对目标时使用 `push --dry-run`。
- 网页界面：`python hub.py serve --open`（http://127.0.0.1:8790）。
