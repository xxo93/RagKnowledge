// SSE 流式问答客户端；EventSource 不支持 POST body，因此使用 fetch + ReadableStream 解析
// 事件类型：tool / token / source / done / error
// AbortController 用于中断请求；Cookie 由浏览器同源自动携带
import { notifyError } from './http'

const FIRST_EVENT_TIMEOUT_MS = 30000

/**
 * 发起流式问答请求
 * @param {{session_id:number, query:string}} payload
 * @param {Object} handlers 回调：onTool/onToken/onSource/onDone/onError/onAbort
 * @returns {{abort: Function}} 中断控制器
 */
export function streamChat(payload, handlers = {}) {
  const controller = new AbortController()
  let firstEventTimer = null
  let timedOut = false

  function clearFirstEventTimer() {
    if (firstEventTimer !== null) {
      clearTimeout(firstEventTimer)
      firstEventTimer = null
    }
  }

  function armFirstEventTimer() {
    clearFirstEventTimer()
    firstEventTimer = setTimeout(() => {
      clearFirstEventTimer()
      timedOut = true
      controller.abort()
      handlers.onError?.({ code: 'TIMEOUT', msg: '等待模型响应超时，请重试' })
    }, FIRST_EVENT_TIMEOUT_MS)
  }

  async function run() {
    let resp
    try {
      resp = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal
      })
    } catch (e) {
      if (e.name === 'AbortError' && !timedOut) handlers.onAbort?.()
      else if (e.name !== 'AbortError') handlers.onError?.({ code: 'NETWORK', msg: '连接中断，请重试' })
      return
    }

    if (!resp.ok || !resp.body) {
      // 建立流之前的 4xx/5xx 响应为 JSON 错误包络
      let msg = `请求失败（${resp.status}）`
      let bizCode = null
      try {
        const body = await resp.json()
        msg = body.msg || msg
        bizCode = body.code ?? null
      } catch (_) { /* 忽略解析失败 */ }
      const err = new Error(msg)
      err.httpStatus = resp.status
      err.bizCode = bizCode
      // 401 由全局处理回到登录态；403 统一提示无权限
      notifyError(err)
      handlers.onError?.({ code: 'HTTP_' + resp.status, msg })
      return
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    armFirstEventTimer()
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // SSE 以空行（\n\n）分隔事件帧；切出完整帧，残余片段留到下次拼接
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''
        for (const frame of frames) {
          // 注意：首帧超时的计时器重置必须留在 run() 内。dispatch 是模块级函数，
          // 看不到 streamChat 的闭包变量；曾经在这里调用 clearFirstEventTimer，
          // 导致每个 event 帧抛 ReferenceError 并被误报为“流式读取异常”。
          // 任意完整帧（含 ": ping" 心跳注释帧）都说明连接与后端仍在工作，重置计时。
          clearFirstEventTimer()
          try {
            dispatch(frame, handlers)
          } catch (e) {
            // 单个事件处理异常不应中断整条流，单独隔离上报便于定位渲染层问题
            console.error('[sse] 事件处理失败', e, frame)
          }
        }
      }
      handlers.onDone?.()
    } catch (e) {
      if (e.name === 'AbortError' && !timedOut) handlers.onAbort?.()
      else if (e.name !== 'AbortError') {
        console.error('[sse] 流式读取异常', e)
        handlers.onError?.({ code: 'STREAM', msg: '流式读取异常' })
      }
    } finally {
      clearFirstEventTimer()
    }
  }

  run()
  return { abort: () => controller.abort() }
}

function dispatch(frame, handlers) {
  let eventName = 'message'
  const dataLines = []
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue // 心跳注释帧 ": ping"
    if (line.startsWith('event:')) eventName = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return
  let data = {}
  try {
    data = JSON.parse(dataLines.join('\n'))
  } catch (_) {
    data = { raw: dataLines.join('\n') }
  }
  switch (eventName) {
    case 'tool': handlers.onTool?.(data); break
    case 'token': handlers.onToken?.(data); break
    case 'source': handlers.onSource?.(data); break
    case 'done': handlers.onDone?.(data); break
    case 'error': handlers.onError?.(data); break
    default: break
  }
}
