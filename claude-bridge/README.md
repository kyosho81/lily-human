# Claude Code Bridge（数字人 ↔ Claude Code 桥接）

让数字人（AIRI + Kimi）通过语音创建并驱动本机已登录的 Claude Code 会话：
全自动执行（acceptEdits）、过程实时可见、需要交互的地方（权限确认 / AskUserQuestion）
通过数字人的语音问答完成、结束后语音播报摘要。

## 架构

```
语音/文字 → Kimi(LLM) --调用工具 claude_task--> 浏览器 execute
                                              | POST localhost:8932/run
                                              v
                     claude_bridge_server.py (端口 8932，本目录)
                                              | claude-agent-sdk query()
                                              v
                                    claude.exe（登录态继承当前用户）
                                              |
        进度/提问 <-- SSE /tasks/{id}/events <--+-- can_use_tool 挂起
                                              |
        用户答复 --POST /tasks/{id}/answer -----+
```

AIRI 侧三个新文件（都在 airi 仓库内）：
- `packages/stage-ui/src/stores/claude-bridge.ts` — pinia store：SSE 事件流、日志、提问状态、消息注入 + TTS 播报
- `packages/stage-ui/src/tools/claude-code.ts` — Kimi 工具 `claude_task`
- `apps/stage-web/src/components/ClaudeBridgePanel.vue` — 右下角实时日志面板

`airi/packages/stage-ui/src/stores/chat.ts` 的 ingest 包装器在挂起提问期间把用户消息
路由到 `/answer` 而不是发给 Kimi。

## 配置

### 功能开关（前端 localStorage）

- `settings/claude-bridge/enabled`：`'true'` 开启（默认，config-restore.js 自动初始化）；
  置为其他值即关闭（工具不注册、消息不拦截、面板不出现）。
- 关闭后如想彻底不加载桥接代码，刷新页面即可。

### 桥接服务 config.json

与 `claude_bridge_server.py` 同目录，首次启动自动创建：

```json
{
  "port": 8932,
  "model": "",
  "max_turns": 50,
  "permission_mode": "acceptEdits",
  "claude_bin_dir": "C:\\Users\\tgv1\\.local\\bin",
  "cwd_whitelist": {
    "digital-human": "F:\\digital-human",
    "kps": "D:\\MyRobot\\N_KPS_Arm"
  }
}
```

- `model` 为空 = Claude Code 默认模型
- `permission_mode`：`acceptEdits`（自动接受文件编辑，Bash 等仍走 can_use_tool 语音确认）
- `cwd_whitelist`：工作目录白名单，key 即 `claude_task` 的 `cwd_key` 参数

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + 白名单 key 列表 |
| POST | `/run` | `{prompt, cwd_key}` → `{task_id}`，立即返回，后台执行 |
| GET | `/tasks/{id}/events` | SSE 事件流：`hello / tool_start / text / log / waiting_input / done / error` |
| POST | `/tasks/{id}/answer` | 答复挂起提问 `{request_id, behavior: "allow"\|"deny", answers?, message?}` |
| GET | `/tasks` | 任务列表（最近 20 个：状态 / 挂起数 / 预览 / 结果摘要） |
| GET | `/tasks/{id}` | 状态快照（status / pending 列表含 preview / result / error / elapsed_s） |

## 运维

- 启动：`F:\digital-human\services\启动语音服务.bat` 会一并拉起（第 4 个服务，端口 8932）
- 手动启动：`F:\digital-human\envs\voxcpm\Scripts\python.exe F:\digital-human\claude-bridge\claude_bridge_server.py`
- 依赖：claude-agent-sdk 装在 voxcpm 环境（安装需先 `pip install hatchling` 再
  `--no-build-isolation`，见服务文件头注释）
- 登录态：继承当前 Windows 用户的 Claude Code 登录，无需额外配置
- 日志：服务窗口 stdout（task 启动 / 提问挂起 / 答复 / 完成均有留痕）

## 安全说明

- 工作目录严格白名单，非白名单 `cwd_key` 返回 400
- `acceptEdits` 下文件编辑自动通过；Bash 等工具及 AskUserQuestion 均经
  `can_use_tool` 挂起，等用户在网页/语音确认后才执行（deny 可拒绝）
- 服务只监听 127.0.0.1