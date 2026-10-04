<script setup lang="ts">
import { useClaudeBridgeStore } from '@proj-airi/stage-ui/stores/claude-bridge'
import { storeToRefs } from 'pinia'
import { nextTick, ref, watch } from 'vue'

const bridge = useClaudeBridgeStore()
const { activeTaskId, running, logLines, pendingQuestion } = storeToRefs(bridge)

const collapsed = ref(false)
const logEl = ref<HTMLElement | null>(null)
const panelEl = ref<HTMLElement | null>(null)

// Panel visibility: visible by default (shows task list even when idle);
// the header ✕ hides it, a floating dot reopens it. Task activity overrides
// the hidden state so a running/waiting task always surfaces the panel.
const hidden = ref(localStorage.getItem('settings/claude-bridge/panel-hidden') === '1')

function hide() {
  hidden.value = true
  localStorage.setItem('settings/claude-bridge/panel-hidden', '1')
}

function show() {
  hidden.value = false
  localStorage.setItem('settings/claude-bridge/panel-hidden', '0')
}

// Panel position: defaults to bottom-right; user can drag the header to move
// it (the fixed panel would otherwise cover the chat input at small viewports).
const savedPos = JSON.parse(localStorage.getItem('settings/claude-bridge/panel-pos') || 'null')
const pos = ref<{ left: number, top: number } | null>(
  savedPos && Number.isFinite(savedPos.left) && Number.isFinite(savedPos.top) ? savedPos : null,
)
const posStyle = ref(pos.value
  ? { left: `${pos.value.left}px`, top: `${pos.value.top}px`, right: 'auto', bottom: 'auto' }
  : {})

let dragged = false

function onHeaderPointerDown(e: PointerEvent) {
  if ((e.target as HTMLElement).closest('button'))
    return
  const el = panelEl.value
  if (!el)
    return
  dragged = false
  const rect = el.getBoundingClientRect()
  const offsetX = e.clientX - rect.left
  const offsetY = e.clientY - rect.top
  const move = (ev: PointerEvent) => {
    const left = Math.min(Math.max(0, ev.clientX - offsetX), window.innerWidth - rect.width)
    const top = Math.min(Math.max(0, ev.clientY - offsetY), window.innerHeight - 32)
    if (Math.abs(ev.clientX - e.clientX) > 4 || Math.abs(ev.clientY - e.clientY) > 4)
      dragged = true
    el.style.right = 'auto'
    el.style.bottom = 'auto'
    el.style.left = `${left}px`
    el.style.top = `${top}px`
  }
  const up = () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    const r = el.getBoundingClientRect()
    posStyle.value = { left: `${Math.round(r.left)}px`, top: `${Math.round(r.top)}px`, right: 'auto', bottom: 'auto' }
    localStorage.setItem('settings/claude-bridge/panel-pos', JSON.stringify({ left: Math.round(r.left), top: Math.round(r.top) }))
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
}

function onHeaderClick() {
  if (!dragged)
    collapsed.value = !collapsed.value
}

watch(logLines, async () => {
  await nextTick()
  if (logEl.value)
    logEl.value.scrollTop = logEl.value.scrollHeight
}, { deep: true })

function clear() {
  bridge.logLines.splice(0)
}

const STATUS_TEXT: Record<string, string> = {
  running: '执行中',
  waiting: '等待回答',
  done: '完成',
  error: '出错',
}

function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status
}
</script>

<template>
  <button
    v-if="hidden && !running && !bridge.hasWaitingTask && !logLines.length"
    class="cbp-reopen"
    title="打开 Claude Code 任务面板"
    @click="show"
  >
    CC
  </button>
  <div
    v-else-if="!hidden || running || bridge.hasWaitingTask || logLines.length"
    ref="panelEl"
    class="claude-bridge-panel"
    :class="{ collapsed }"
    :style="posStyle"
  >
    <div class="cbp-header" title="拖拽可移动" @pointerdown="onHeaderPointerDown" @click="onHeaderClick">
      <span class="cbp-dot" :class="{ busy: running || bridge.hasWaitingTask }" />
      <span class="cbp-title">
        Claude Code {{ running ? '执行中' : bridge.hasWaitingTask ? '等待回答' : '空闲' }}<template v-if="activeTaskId"> · {{ activeTaskId }}</template>
      </span>
      <button class="cbp-btn" @click.stop="clear">
        清空
      </button>
      <button class="cbp-btn" @click.stop="hide">
        ✕
      </button>
      <button class="cbp-btn" @click.stop="collapsed = !collapsed">
        {{ collapsed ? '展开' : '收起' }}
      </button>
    </div>
    <div v-if="!collapsed" class="cbp-body">
      <div v-if="pendingQuestion" class="cbp-question">
        <div class="cbp-question-label">
          ⏸ 等待你回答（可点按钮、直接说话或打字）
        </div>
        <div class="cbp-question-text">
          {{ pendingQuestion.preview }}
        </div>
        <div class="cbp-question-actions">
          <button class="cbp-btn cbp-allow" @click.stop="bridge.respond('allow')">
            ✓ 同意
          </button>
          <button class="cbp-btn cbp-deny" @click.stop="bridge.respond('deny')">
            ✗ 拒绝
          </button>
        </div>
      </div>
      <div v-if="bridge.tasksList.length" class="cbp-tasks">
        <div
          v-for="t in bridge.tasksList"
          :key="t.task_id"
          class="cbp-task"
          :class="[t.status, { active: t.task_id === activeTaskId }]"
          :title="t.preview || t.result || ''"
          @click="bridge.reattach(t.task_id)"
        >
          <span class="cbp-task-id">{{ t.task_id }}</span>
          <span class="cbp-task-status" :class="t.status">{{ statusText(t.status) }}</span>
          <span class="cbp-task-time">{{ t.started_at }}</span>
          <span v-if="t.status === 'waiting' && t.preview" class="cbp-task-preview">{{ t.preview }}</span>
        </div>
      </div>
      <div ref="logEl" class="cbp-log">
        <div v-for="(line, i) in logLines" :key="i" class="cbp-line">
          {{ line }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cbp-reopen {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 9000;
  width: 36px;
  height: 36px;
  border: 0;
  border-radius: 50%;
  background: rgb(23 23 23 / 85%);
  color: #a3a3a3;
  font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
  cursor: pointer;
  box-shadow: 0 4px 14px rgb(0 0 0 / 40%);
}

.cbp-reopen:hover {
  color: #e5e5e5;
  background: rgb(40 40 40 / 90%);
}

.claude-bridge-panel {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 9000;
  width: 380px;
  max-width: calc(100vw - 32px);
  border-radius: 10px;
  background: rgb(23 23 23 / 92%);
  color: #e5e5e5;
  font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  box-shadow: 0 6px 24px rgb(0 0 0 / 45%);
  overflow: hidden;
}

.cbp-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  cursor: pointer;
  background: rgb(255 255 255 / 6%);
  user-select: none;
}

.cbp-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #4ade80;
  flex-shrink: 0;
}

.cbp-dot.busy {
  background: #facc15;
  animation: cbp-pulse 1s ease-in-out infinite alternate;
}

@keyframes cbp-pulse {
  from { opacity: 1; }
  to { opacity: 0.35; }
}

.cbp-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cbp-btn {
  border: 0;
  border-radius: 6px;
  padding: 2px 8px;
  background: rgb(255 255 255 / 12%);
  color: inherit;
  font: inherit;
  cursor: pointer;
}

.cbp-btn:hover {
  background: rgb(255 255 255 / 22%);
}

.cbp-body {
  display: flex;
  flex-direction: column;
  max-height: 300px;
}

.cbp-question {
  margin: 8px 8px 0;
  padding: 8px 10px;
  border: 1px solid #facc15;
  border-radius: 8px;
  background: rgb(250 204 21 / 12%);
}

.cbp-question-label {
  color: #fde68a;
  margin-bottom: 4px;
}

.cbp-question-text {
  white-space: pre-wrap;
  word-break: break-all;
}

.cbp-question-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.cbp-allow {
  background: rgb(34 197 94 / 25%);
  color: #86efac;
}

.cbp-allow:hover {
  background: rgb(34 197 94 / 40%);
}

.cbp-deny {
  background: rgb(239 68 68 / 22%);
  color: #fca5a5;
}

.cbp-deny:hover {
  background: rgb(239 68 68 / 38%);
}

.cbp-log {
  overflow-y: auto;
  padding: 8px 10px;
}

.cbp-tasks {
  max-height: 110px;
  overflow-y: auto;
  border-bottom: 1px solid rgb(255 255 255 / 10%);
  padding: 4px 6px;
}

.cbp-task {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 3px 6px;
  border-radius: 6px;
  cursor: pointer;
}

.cbp-task:hover {
  background: rgb(255 255 255 / 8%);
}

.cbp-task.active {
  background: rgb(125 211 252 / 12%);
}

.cbp-task-id {
  color: #a3a3a3;
  font-size: 11px;
}

.cbp-task-status {
  font-size: 11px;
  padding: 0 6px;
  border-radius: 4px;
  background: rgb(255 255 255 / 10%);
}

.cbp-task-status.waiting {
  color: #fde68a;
  background: rgb(250 204 21 / 18%);
}

.cbp-task-status.error {
  color: #fca5a5;
  background: rgb(239 68 68 / 18%);
}

.cbp-task-status.done {
  color: #86efac;
}

.cbp-task-time {
  margin-left: auto;
  color: #737373;
  font-size: 11px;
}

.cbp-task-preview {
  flex-basis: 100%;
  color: #d4d4d4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cbp-line {
  white-space: pre-wrap;
  word-break: break-all;
  padding: 1px 0;
  border-bottom: 1px solid rgb(255 255 255 / 4%);
}
</style>