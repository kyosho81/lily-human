import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { useCharacterStore } from './character'
import { useChatSessionStore } from './chat/session-store'

const BRIDGE_URL = 'http://localhost:8932'
const MAX_LOG_LINES = 500

export interface BridgePendingQuestion {
  requestId: string
  toolName: string
  preview: string
}

/** Feature switch: config-restore.js initializes this key (default "true"). */
export function isClaudeBridgeEnabled(): boolean {
  return localStorage.getItem('settings/claude-bridge/enabled') === 'true'
}

/**
 * Client for the local Claude Code bridge (claude-bridge/claude_bridge_server.py).
 *
 * The Kimi tool `claude_task` fires a task here and returns immediately; this
 * store follows the SSE event stream, shows a live log, announces pending
 * questions (spoken via TTS so the user can answer by voice), and injects the
 * final result as an assistant chat message.
 */
export const useClaudeBridgeStore = defineStore('claude-bridge', () => {
  const activeTaskId = ref('')
  const running = ref(false)
  const logLines = ref<string[]>([])
  const pendingQuestion = ref<BridgePendingQuestion | null>(null)
  /** Recent bridge tasks (from GET /tasks), refreshed periodically. */
  const tasksList = ref<Array<Record<string, any>>>([])

  let sseAbort: AbortController | null = null

  function pushLog(line: string) {
    logLines.value.push(line)
    if (logLines.value.length > MAX_LOG_LINES)
      logLines.value.splice(0, logLines.value.length - MAX_LOG_LINES)
  }

  async function refreshTasks() {
    try {
      const resp = await fetch(`${BRIDGE_URL}/tasks`)
      if (resp.ok)
        tasksList.value = ((await resp.json()) as { tasks: any[] }).tasks ?? []
    }
    catch {
      // bridge offline; keep stale list
    }
  }

  refreshTasks()
  setInterval(refreshTasks, 8000)

  /** True when some task in the list is suspended waiting for an answer. */
  const hasWaitingTask = computed(() => tasksList.value.some(t => t.status === 'waiting'))

  async function startTask(prompt: string, cwdKey: string): Promise<{ taskId: string }> {
    const resp = await fetch(`${BRIDGE_URL}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, cwd_key: cwdKey }),
    })
    if (!resp.ok)
      throw new Error(`bridge /run failed: HTTP ${resp.status}`)
    const { task_id } = await resp.json() as { task_id: string }
    activeTaskId.value = task_id
    running.value = true
    pendingQuestion.value = null
    void attachSSE(task_id)
    return { taskId: task_id }
  }

  /**
   * Take over a task started earlier (e.g. stuck waiting after a page refresh):
   * restore its pending question and re-attach the SSE stream.
   */
  async function reattach(taskId: string) {
    sseAbort?.abort()
    activeTaskId.value = taskId
    pendingQuestion.value = null
    try {
      const resp = await fetch(`${BRIDGE_URL}/tasks/${taskId}`)
      if (resp.ok) {
        const d = await resp.json() as { status: string, pending?: any[] }
        running.value = d.status === 'running' || d.status === 'waiting'
        if (d.pending && d.pending.length > 0) {
          pendingQuestion.value = {
            requestId: d.pending[0].request_id,
            toolName: d.pending[0].tool_name,
            preview: String(d.pending[0].preview ?? ''),
          }
        }
      }
    }
    catch {
      // snapshot failed; still try SSE
    }
    void attachSSE(taskId)
  }

  async function attachSSE(taskId: string) {
    sseAbort?.abort()
    const ctl = new AbortController()
    sseAbort = ctl
    pushLog(`# task ${taskId} 已连接`)
    try {
      const resp = await fetch(`${BRIDGE_URL}/tasks/${taskId}/events`, { signal: ctl.signal })
      if (!resp.body)
        throw new Error('no response body')
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done)
          break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data:'))
            continue
          try {
            handleEvent(taskId, JSON.parse(line.slice(5).trim()))
          }
          catch {
            // malformed line; skip
          }
        }
      }
    }
    catch (error) {
      if (!ctl.signal.aborted)
        pushLog(`# 事件流断开: ${String(error).slice(0, 120)}`)
    }
  }

  function handleEvent(taskId: string, ev: Record<string, any>) {
    switch (ev.type) {
      case 'tool_start':
        pushLog(`> ${ev.tool} ${ev.input ?? ''}`)
        break
      case 'text':
        pushLog(String(ev.text ?? ''))
        break
      case 'waiting_input':
        pendingQuestion.value = {
          requestId: ev.request_id,
          toolName: ev.tool_name,
          preview: String(ev.preview ?? ''),
        }
        pushLog(`? ${ev.tool_name}: ${ev.preview}`)
        // Speak the question so the user can answer by voice. Permission-style
        // prompts (non-AskUserQuestion) explicitly invite an allow/deny reply.
        if (ev.tool_name === 'AskUserQuestion')
          void useCharacterStore().emitTextOutput(`Claude 提问：${ev.preview}，请回答。`).catch(() => {})
        else
          void useCharacterStore().emitTextOutput(`Claude 请求：${ev.preview}。请回答同意或拒绝。`).catch(() => {})
        break
      case 'done':
        finishTask(taskId, String(ev.result ?? ''), false)
        break
      case 'error':
        finishTask(taskId, `任务出错：${ev.message}`, true)
        break
    }
  }

  /**
   * Split the task result into a speakable short summary and the full text.
   * The bridge prompt asks Claude for "语音摘要\n---\n详细内容"; when the
   * delimiter is missing, speak nothing here and let the caller fall back.
   */
  function splitVoiceSummary(text: string): { speak: string, show: string } {
    const m = text.match(/([\s\S]*?)\n\s*-{3,}\s*\n([\s\S]*)/)
    if (m) {
      // Strip the leading marker Claude adds (「语音摘要」 etc.) so TTS reads
      // only the actual summary sentence.
      const summary = m[1].trim().replace(/^[「【]?语音摘要[」】]?\s*[:：]?\s*/, '').trim()
      return { speak: summary, show: `${summary}\n\n${m[2].trim()}` }
    }
    return { speak: '', show: text }
  }

  function finishTask(taskId: string, result: string, isError: boolean) {
    running.value = false
    pendingQuestion.value = null
    pushLog(`# task ${taskId} 结束${isError ? '（出错）' : ''}`)
    const text = result.trim() || '（任务结束，没有文字结果）'
    const { speak, show } = splitVoiceSummary(text)
    try {
      const session = useChatSessionStore()
      session.appendSessionMessage(session.activeSessionId, {
        id: `claude-bridge-${taskId}`,
        role: 'assistant',
        content: show,
        slices: [{ type: 'text', text: show }],
        tool_results: [],
        createdAt: Date.now(),
      } as any)
    }
    catch (error) {
      pushLog(`# 注入消息失败: ${String(error).slice(0, 120)}`)
    }
    // Speak only the short summary. Fallback (no delimiter): strip code blocks
    // and trim, so long results never get read aloud wholesale.
    const speakable = (speak || text)
      .replace(/```[\s\S]*?```/g, '（代码块）')
      .replace(/`([^`]*)`/g, '$1')
      .slice(0, 300)
    void useCharacterStore().emitTextOutput(speakable).catch(() => {})
  }

  /** Shared POST /answer routine; speaks a confirmation so voice users get feedback. */
  async function postAnswer(q: BridgePendingQuestion, taskId: string, behavior: 'allow' | 'deny', text: string): Promise<boolean> {
    pendingQuestion.value = null
    pushLog(`# 我: ${text}`)
    const resp = await fetch(`${BRIDGE_URL}/tasks/${taskId}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: q.requestId,
        behavior,
        answers: { '*': text },
        message: text,
      }),
    })
    if (!resp.ok) {
      pushLog(`# 答复失败: HTTP ${resp.status}`)
      return false
    }
    void useCharacterStore()
      .emitTextOutput(behavior === 'deny' ? '好的，已拒绝该操作。' : '好的，已同意，任务继续。')
      .catch(() => {})
    return true
  }

  /** Route the user's reply to a pending bridge question. Returns false when nothing is pending. */
  async function answer(text: string): Promise<boolean> {
    const q = pendingQuestion.value
    const taskId = activeTaskId.value
    if (!q || !taskId)
      return false
    // Voice-friendly allow/deny: spoken answers like "同意/确认/继续" allow,
    // "不要/拒绝/算了" deny. Anything else is passed through as allow.
    const isDeny = /^(不要|不用|别|否|拒绝|反对|算了|取消|不行|不可以|不同意|stop|no|deny)/i.test(text.trim())
    return postAnswer(q, taskId, isDeny ? 'deny' : 'allow', text)
  }

  /** Mouse path: explicit allow/deny buttons in the panel question box. */
  async function respond(behavior: 'allow' | 'deny'): Promise<boolean> {
    const q = pendingQuestion.value
    const taskId = activeTaskId.value
    if (!q || !taskId)
      return false
    const text = behavior === 'deny' ? '拒绝' : '同意'
    // Mirror the answer into the chat history so the conversation stays coherent.
    try {
      const session = useChatSessionStore()
      session.appendSessionMessage(session.activeSessionId, {
        id: `claude-bridge-answer-${Date.now()}`,
        role: 'user',
        content: text,
        createdAt: Date.now(),
      } as any)
    }
    catch {
      // chat history append is best-effort
    }
    return postAnswer(q, taskId, behavior, text)
  }

  /** True while a bridge question is waiting for the user's reply. */
  function hasPendingQuestion(): boolean {
    return isClaudeBridgeEnabled() && pendingQuestion.value !== null
  }

  return {
    activeTaskId,
    running,
    logLines,
    pendingQuestion,
    tasksList,
    hasWaitingTask,
    startTask,
    reattach,
    refreshTasks,
    answer,
    respond,
    hasPendingQuestion,
  }
})