import type { ExecutableTool } from '../stores/ai/chat-llm/tools'

import { rawTool } from '@xsai/tool'

import { useClaudeBridgeStore } from '../stores/claude-bridge'

/**
 * Lets the assistant (Kimi) create Claude Code sessions on the user's machine.
 *
 * The tool returns immediately after the task is accepted; the bridge streams
 * progress to the ClaudeBridgePanel and announces the result by itself. The
 * description is written to stop Kimi from blocking on or inventing results.
 *
 * Note: @xsai/tool's `tool()` is async (it awaits schema conversion), which
 * silently breaks the spread into addTools. `rawTool` is synchronous and takes
 * a plain JSON schema, so the tool shape stays intact.
 */
export function createClaudeCodeTools(): ExecutableTool[] {
  const claudeTask = rawTool({
    name: 'claude_task',
    description: [
      '让 Claude Code（本机已登录的 claude）在预设工作目录里执行一个编程/查问题任务。',
      '什么时候用：用户说"让小K/Claude 去…"、"帮我改一下代码"、"查一下这个问题"等希望调用 Claude Code 干活的请求。',
      'cwd_key 怎么选：用户提到数字人/airi/本项目时用 "digital-human"（F:\\digital-human）；提到机械臂/KPS/N_KPS_Arm 时用 "kps"（D:\\MyRobot\\N_KPS_Arm）；没提及时用 "digital-human"。',
      '重要：调用本工具后任务在后台执行，可能需要几分钟。你必须立即用一两句话告诉用户"任务已开始执行"，',
      '绝对不要等待结果、不要编造任务结果、不要追问任务进度；任务完成后结果会自动显示在聊天里并语音播报给用户。',
      '如果任务过程中 Claude 需要向用户提问，用户会直接在对话里回答，那些回答不是发给你的，你不需要解释或转述。',
    ].join('\n'),
    parameters: {
      type: 'object',
      properties: {
        prompt: {
          type: 'string',
          description: '交给 Claude Code 的完整任务描述（中文即可，说清要做什么、在哪个目录、有什么要求）',
        },
        cwd_key: {
          type: 'string',
          enum: ['digital-human', 'kps'],
          description: '预设工作目录键：digital-human=F:\\digital-human，kps=D:\\MyRobot\\N_KPS_Arm',
        },
      },
      required: ['prompt', 'cwd_key'],
      additionalProperties: false,
    },
    execute: async ({ prompt, cwd_key }) => {
      try {
        const bridge = useClaudeBridgeStore()
        const { taskId } = await bridge.startTask(prompt, cwd_key)
        return `任务已启动（id=${taskId}）。现在立刻告诉用户任务已开始执行，不要等待结果、不要编造结果；完成后结果会自动展示并语音播报。`
      }
      catch (error) {
        return `启动 Claude Code 任务失败：${String(error).slice(0, 200)}。桥接服务可能暂时没启动。请告诉用户"桥接服务暂时连不上，请稍等几秒后让我再试一次"；下次用户让你调用时务必直接重试，不要因为这次失败就拒绝调用。`
      }
    },
  })

  return [{
    id: 'claude-code-bridge',
    defaultActive: true,
    ...claudeTask,
  } as ExecutableTool]
}