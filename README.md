# Claude Code 控制台 · panel

一个本地运行的 Claude Code 图形面板。单文件 Python 标准库实现，前端 HTML/CSS/JS 直接嵌在 `console.py` 里，只监听 `127.0.0.1`。

用来管理：模型、思考档位、权限、magpie 网关、API key、本地历史会话，以及按工作目录打开 Claude Code 终端。

> 仓库是通用版本：默认读取 `~/.claude`，默认工作区 `~/claude-workspace`。所有路径都能用环境变量改，见下面的配置表。

## 功能

| 区块 | 内容 |
|---|---|
| 网关 magpie | 启动 / 停止 / 重启，状态灯，`127.0.0.1:3425` |
| 模型 | 网关里全部模型一键切换；恢复官方默认 |
| 历史对话 | 读取 `CLAUDE_CONFIG_DIR/projects/**/*.jsonl`，列表 + 预览；`claude --resume <id>` 进入 |
| 思考档位 | default / low / medium / high / xhigh / max；子代理独立档位 |
| 权限 · 工作区 | defaultMode + allow / deny / ask / additionalDirectories，一键预设 |
| 思考开关节流 | 思考开/关、自适应开/关、`MAX_THINKING_TOKENS` 预算 |
| 进入 Claude Code | 按工作目录开终端（普通 / `--continue` / 选模型进）、Finder 打开 |
| 余额额度 / 提供商 | `magpie quota`、`magpie providers` |
| API key | 新增 provider、替换已有 key、删除已有 provider；可屏蔽指定预设分组 |
| 发一句话跑一轮 | `claude -p`，可选 `--verbose` 显示细节 |
| 环境 | 配置目录、settings 路径、版本号 |

## 依赖

- macOS（打开 Terminal 用 `osascript`，打开浏览器用 `open`）
- Python 3.9+，只使用标准库
- 可选：`magpie` 网关 CLI、`claude` Claude Code CLI。没装也能启动，对应卡片会显示未安装或离线。

## 快速开始

```bash
cd claude-panel

# 直接运行
python3 console.py

# 或双击启动器
./Claude控制台.command
```

浏览器打开 <http://127.0.0.1:7788/>。服务只绑定本机回环地址。

### 构建 Dock app

```bash
./make_app.sh
open dist/Claude控制台.app
```

`dist/Claude控制台.app` 是自包含的：把 `console.py` 打进 app 里。点击时如果 7788 没在跑，会在后台启动服务（日志 `/tmp/ccp.log`），然后打开浏览器。改完源码后重新执行 `./make_app.sh`。

图标 PNG 以 `.b64` 纯文本形式保存在 `assets/`，`make_app.sh` 会在首次构建时自动还原；还原出的 PNG 属于生成物，不会进入版本控制。

把 app 拖到 Dock、或者拖进「应用程序」文件夹即可。不想留了：右键 Dock 图标 → 选项 → 从 Dock 中移除。

## 配置

全部通过环境变量覆盖：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CCP_PORT` | `7788` | 面板监听端口 |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code 配置目录，读写 `settings.json`，扫描 `projects/` |
| `CLAUDE_BIN_DIR` | `~/.local/bin` | 查找 `magpie` / `claude` 的目录 |
| `MAGPIE_BIN` | `$CLAUDE_BIN_DIR/magpie` | magpie 可执行文件路径 |
| `CLAUDE_BIN` | `$CLAUDE_BIN_DIR/claude` | claude 可执行文件路径 |
| `CLAUDE_PANEL_WORKSPACE` | `~/claude-workspace` | 默认工作区 |
| `CLAUDE_PANEL_ROOT` | 同工作区 | 权限预设里的上级目录；需要放行父目录时再设 |
| `CCP_BLOCKED_GROUPS` | `relays` | 屏蔽的 magpie 预设分组，逗号分隔；留空则不屏蔽 |

例子（换成你自己的路径）：

```bash
export CLAUDE_CONFIG_DIR="$HOME/my-claude-config"
export CLAUDE_PANEL_WORKSPACE="$HOME/my-claude-workspace"
export CLAUDE_PANEL_ROOT="$HOME/my-claude-workspace"
python3 console.py
```

## HTTP 接口

面板自身用，也可以 curl。所有接口只监听本机。

```text
GET  /api/state            全量状态
GET  /api/presets          magpie 预设列表（缓存 10 分钟，按 CCP_BLOCKED_GROUPS 过滤）
GET  /api/sessions         历史会话列表（最近 40 个）
GET  /api/session?id=…     单个会话的对话预览
GET  /api/ping             网关存活
POST /api/gateway          {"action":"start|stop|restart"}
POST /api/model            {"model":"provider/model"}
POST /api/claude_default   {}
POST /api/effort           {"level":"high"} 或 {"level":"medium","field":"subagent"}
POST /api/thinking         {"disabled":true} / {"adaptive_off":true} / {"budget":"32000"}
POST /api/provider_key     {"target":"add:<preset>","key":"..."} 或 {"target":"replace:<id>","key":"..."}
POST /api/provider_rm      {"id":"<provider>"}   删除 provider（不可撤销）
POST /api/perms            {"preset":"workspace"} 或 {"mode":"plan"} 或 {allow_text,deny_text,ask_text,dirs_text}
POST /api/terminal         {"kind":"plain|resume|picker|session","cwd":"...","session":"..."}
POST /api/folder           {"cwd":"..."}
POST /api/ask              {"prompt":"...","cwd":"...","verbose":false}
```

## 权限模型

面板把权限写进 `CLAUDE_CONFIG_DIR/settings.json` 的 `permissions`。优先级 **deny > ask > allow**。

```json
{
  "permissions": {
    "defaultMode": "acceptEdits",
    "additionalDirectories": ["/path/to/workspace"],
    "allow": ["..."],
    "deny":  ["..."],
    "ask":   ["..."]
  }
}
```

内置预设会写入一组 allow / deny / ask 规则：放行工作区读写和常见的只读检索、git 操作；拦截 `~/.ssh`、`~/.aws`、`.env`、`*.pem`、`sudo`、`rm -rf /` 等；`git push`、`rm`、安装依赖等先问。

六种 `defaultMode`：

| 模式 | 行为 |
|---|---|
| `default` | 每个工具调用都问 |
| `acceptEdits` | 改工作区文件不问，Bash 按规则 |
| `auto` | 自动判断，越界才问 |
| `plan` | 只读，只能看和出方案 |
| `dontAsk` | 不问，该拒的直接拒 |
| `bypassPermissions` | 全放开，无任何检查 |

权限、effort、思考的改动都要**新开会话**才生效。

## 历史对话

- 列表读取 `CLAUDE_CONFIG_DIR/projects/*/*.jsonl`，按修改时间倒序。
- 预览只读文件尾部，超大文件最多读最后 4MB、显示最近 200 条消息，避免卡顿。
- 「进入」会使用记录里的工作目录执行 `claude --resume <session-id>`，并在终端里带上当前的 `CLAUDE_CONFIG_DIR`。

## 隐私与安全

- **只监听 `127.0.0.1`**，没有登录和远程访问。不要用端口转发、反向代理把它暴露到公网。
- **API key 不会被面板保存**：key 只在 POST 请求里传给本机 magpie CLI，由 magpie 写入它自己的配置。面板不回显、不写日志；命令输出里的 key 会替换成 `***`。
- **历史对话只在本机读取**：面板扫描本地 JSONL 并在浏览器里展示，不会上传。预览内容只通过 `127.0.0.1` 返回。
- 仓库和压缩包**不包含** `config/`、`projects/`、`sessions/`、日志或任何真实 key；`.gitignore` 也挡住了这些路径。
- `magpie` / `claude` 是可选依赖，它们自己发起的网络请求不受本面板控制。

## 目录

```text
claude-code-console/
├── console.py              单文件主程序
├── Claude控制台.command     双击启动
├── make_app.sh             构建 dist/Claude控制台.app
├── assets/
│   ├── icon.svg            中性占位图标（可自己换）
│   ├── icon-1024.png.b64   1024 图标的纯文本形式
│   └── icon-64.png.b64     64 图标的纯文本形式
├── dist/                   构建产物，默认不提交
├── README.md
├── LICENSE
└── .gitignore
```

## 发布到 GitHub

仓库根目录就是本目录（`claude-code-console/`）。

`dist/`、`*.app`、`*.icns` 以及还原出的 `assets/*.png` 默认被 `.gitignore` 忽略，源码仓库保持干净。图标以 `.b64` 纯文本提交，首次执行 `./make_app.sh` 时自动还原，无需额外操作。

`LICENSE` 默认是 MIT，可以按需要替换。`assets/` 里的图标是本项目自己画的占位图；如果你要使用 Anthropic / Claude 的官方标志，请先确认对方的商标和品牌使用条款。

## 已知噪音

`[claude-code:unrecognized_model]` 是 Claude Code 客户端不认识模型字面 id 的自检提示，功能正常；`/api/ask` 已在输出里过滤。
