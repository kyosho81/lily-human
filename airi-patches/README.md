# AIRI 增量补丁（airi-patches）

本项目的数字人前端基于第三方开源项目 [AIRI](https://github.com/moeru-ai/airi)（MIT License，
版权归属 Neko Ayaka / moeru-ai）。`airi/` 目录不随本仓库发布，我们的全部改动以补丁形式放在这里。

## 基线版本

补丁基于 airi 提交 `2f59a4c`（`test: include pipelines audio in root vitest projects (#2419)`）。

```bash
git clone https://github.com/moeru-ai/airi.git
cd airi
git checkout 2f59a4c
```

## 应用补丁

```bash
# 在 airi 仓库根目录执行
git apply /path/to/digital-human/airi-patches/airi-patch.diff

# 复制新增文件（保持目录结构）
cp -r /path/to/digital-human/airi-patches/new-files/apps/stage-web/src/components/ClaudeBridgePanel.vue \
      apps/stage-web/src/components/
cp -r /path/to/digital-human/airi-patches/new-files/packages/stage-ui/src/. \
      packages/stage-ui/src/
```

## 补丁内容

**修改（airi-patch.diff，14 个文件）：**
- `apps/stage-web`：App.vue / main.ts / vite.config.ts（注册 claude_task 工具、
  挂载 ClaudeBridgePanel、PWA selfDestroying 防旧缓存、Promise.withResolvers polyfill）
- `packages/stage-ui`：chat.ts ingest 包装（挂起提问期间消息路由到 /answer）、
  speech / hearing / default 模块（接本地 TTS/ASR）、providers/official、
  package.json、vad.ts
- `packages/core-agent`：chat-completions 请求清洗（剔除空 assistant 消息，兼容 Moonshot）、
  chat-orchestrator-runtime、llm-service
- `packages/provider-inference`：openai-audio 小改

**新增（new-files/）：**
- `apps/stage-web/src/components/ClaudeBridgePanel.vue` — 右下角 Claude 执行实时日志面板
- `packages/stage-ui/src/stores/claude-bridge.ts` — pinia store：SSE 事件流 / 提问状态 / TTS 播报
- `packages/stage-ui/src/tools/claude-code.ts` — Kimi 工具 `claude_task` 定义
- `packages/stage-ui/src/libs/providers/local-ws-transcription/` — 本地 WebSocket ASR 提供方

## 注意

- `apps/stage-web/public/models/silero-vad` 为 VAD 模型二进制（约 2.2MB），
  未包含在补丁中，运行时按 AIRI 正常流程下载或自行放置。
- 前端 localStorage 的 provider 配置（LLM key 等）属于本机私有数据，
  不包含在仓库内，请在浏览器中自行配置。