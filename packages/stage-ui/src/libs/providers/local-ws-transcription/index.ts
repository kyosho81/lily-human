import type { AIRIStreamTranscriptionDelta, AIRIStreamTranscriptionResult } from '../stream-transcription'

/**
 * 本地 faster-whisper 流式识别（WebSocket 版）。
 *
 * 为什么不用通用 streamTranscription（SSE + fetch 流式上传）：
 * uvicorn 不支持 HTTP/1.1 全双工——响应一旦开始流式发送，请求体读取就会停摆，
 * 浏览器上传的音频永远到不了服务端。WebSocket 天然全双工，没有这个问题，
 * 也不依赖浏览器支持 fetch duplex（旧内核浏览器同样可用）。
 *
 * 协议（与 services/whisper_server.py 的 /ws 端点对应）：
 *   客户端 → 服务端：二进制帧 = PCM16 小端 16kHz 单声道音频块；
 *                    文本帧 {"type":"end"} = 一句话说完，请收尾
 *   服务端 → 客户端：文本帧 JSON：
 *     {"type":"transcript.text.snapshot","text":"...","isFinal":false}  中间结果（边说边出）
 *     {"type":"transcript.text.snapshot","text":"...","isFinal":true }  最终结果（触发提交）
 */
export interface LocalWsTranscriptionOptions {
  baseURL?: string | URL
  inputAudioStream?: ReadableStream<ArrayBuffer | ArrayBufferView>
  file?: Blob
  abortSignal?: AbortSignal
}

function resolveWsUrl(baseURL: string | URL): string {
  const raw = typeof baseURL === 'string' ? baseURL : baseURL.href
  // provider 配的是 http://localhost:8931/v1/ → ws://localhost:8931/ws
  return raw
    .replace(/^http/, 'ws')
    .replace(/\/v1\/?$/, '/ws')
}

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

export function localWsTranscription(options: LocalWsTranscriptionOptions): AIRIStreamTranscriptionResult {
  const wsUrl = resolveWsUrl(options.baseURL ?? '')

  let fullText = ''
  const deferredText = createDeferred<string>()

  let fullStreamCtrl!: ReadableStreamDefaultController<AIRIStreamTranscriptionDelta>
  let textStreamCtrl!: ReadableStreamDefaultController<string>
  const fullStream = new ReadableStream<AIRIStreamTranscriptionDelta>({
    start(controller) {
      fullStreamCtrl = controller
    },
  })
  const textStream = new ReadableStream<string>({
    start(controller) {
      textStreamCtrl = controller
    },
  })

  const finish = () => {
    try { fullStreamCtrl.close() } catch {}
    try { textStreamCtrl.close() } catch {}
    deferredText.resolve(fullText)
  }
  const fail = (err: unknown) => {
    try { fullStreamCtrl.error(err) } catch {}
    try { textStreamCtrl.error(err) } catch {}
    deferredText.reject(err)
  }

  void (async () => {
    let ws: WebSocket | undefined
    let asrT0 = -1  // 流水线阶段指示器：识别计时起点（说完话发送 end 时）
    try {
      if (!wsUrl)
        throw new Error('Missing baseURL for local streaming transcription')

      const input = options.inputAudioStream ?? options.file?.stream()
      if (!input)
        throw new Error('Audio stream or file is required for streaming transcription.')

      console.info('[local-ws-transcription] connecting →', wsUrl)
      ws = new WebSocket(wsUrl)
      ws.binaryType = 'arraybuffer'

      const openPromise = new Promise<void>((resolve, reject) => {
        ws!.onopen = () => {
          console.info('[local-ws-transcription] WebSocket connected')
          resolve()
        }
        ws!.onerror = () => reject(new Error(`Failed to connect to streaming ASR WebSocket: ${wsUrl}`))
      })

      ws.onmessage = (event) => {
        try {
          const delta = JSON.parse(String(event.data)) as AIRIStreamTranscriptionDelta & { isFinal?: boolean }
          console.info('[local-ws-transcription] event:', delta)
          fullStreamCtrl.enqueue(delta)
          if (delta.type === 'transcript.text.delta') {
            fullText += delta.delta
            textStreamCtrl.enqueue(delta.delta)
          }
          else if (delta.type === 'transcript.text.snapshot') {
            fullText = delta.text
            // 流水线阶段指示器：最终结果到达，识别阶段结束
            if (delta.isFinal && asrT0 >= 0) {
              window.dispatchEvent(new CustomEvent('airi:stage', { detail: { stage: 'asr', phase: 'done', ms: Math.round(performance.now() - asrT0) } }))
            }
          }
        }
        catch (err) {
          console.error('[local-ws-transcription] Failed to parse server event:', err)
        }
      }
      ws.onclose = () => finish()
      ws.onerror = () => {}

      options.abortSignal?.addEventListener('abort', () => {
        try { ws?.close() } catch {}
      })

      await openPromise
      console.info('[local-ws-transcription] start forwarding audio chunks')

      // 持续把音频块转发给服务端；输入流结束时通知服务端收尾
      const reader = input.getReader()
      let sent = 0
      while (true) {
        const { done, value } = await reader.read()
        if (done)
          break
        if (ws.readyState !== WebSocket.OPEN)
          return
        const bytes = value instanceof ArrayBuffer
          ? value
          : value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength)
        ws.send(bytes)
        sent += bytes.byteLength
        if (sent < 1000000 && sent % 32000 < bytes.byteLength)
          console.info(`[local-ws-transcription] forwarded ${sent} bytes`)
      }
      console.info('[local-ws-transcription] audio stream ended, sending end')
      if (ws.readyState === WebSocket.OPEN) {
        // 流水线阶段指示器：用户说完，识别阶段开始计时
        asrT0 = performance.now()
        window.dispatchEvent(new CustomEvent('airi:stage', { detail: { stage: 'asr', phase: 'start' } }))
        ws.send(JSON.stringify({ type: 'end' }))
      }
    }
    catch (err) {
      console.error('[local-ws-transcription] error:', err)
      try { ws?.close() } catch {}
      fail(err)
    }
  })()

  return {
    fullStream,
    textStream,
    text: deferredText.promise,
  }
}
