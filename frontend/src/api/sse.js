// SSE 流式客户端（架构 §7.3）：EventSource 不支持 POST body，故用 fetch + ReadableStream 手写解析。
// 事件：tool（工具状态）/ token（答案增量）/ source（引用来源）/ done（结束）/ error（异常）
// 中断：AbortController.abort()；捕获 AbortError 不弹错误，由 UI 复位输入框（§3.5 / §P-02）。

/**
 * 发起流式问答请求
 * @param {{session_id:number, query:string}} payload
 * @param {Object} handlers 回调：onTool/onToken/onSource/onDone/onError/onAbort
 * @returns {{abort: Function}} 中断控制器
 */
export function streamChat(payload, handlers = {}) {
  const controller = new AbortController()

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
      if (e.name === 'AbortError') handlers.onAbort?.()
      else handlers.onError?.({ code: 'NETWORK', msg: '连接中断，请重试' })
      return
    }

    if (!resp.ok || !resp.body) {
      // 建立流之前的 4xx/5xx 为 JSON 错误包络
      let msg = `请求失败（${resp.status}）`
      try {
        const body = await resp.json()
        msg = body.msg || msg
      } catch (_) { /* 忽略解析失败 */ }
      handlers.onError?.({ code: 'HTTP_' + resp.status, msg })
      return
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // SSE 以空行（\n\n）分隔事件帧；切出完整帧，残余片段留到下次拼接
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''
        for (const frame of frames) dispatch(frame, handlers)
      }
      handlers.onDone?.()
    } catch (e) {
      if (e.name === 'AbortError') handlers.onAbort?.()
      else handlers.onError?.({ code: 'STREAM', msg: '流式读取异常' })
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
