// 工作流 API（v3 / WP4）：计划、执行、任务详情与 SSE 事件
import http, { notifyError } from './http'

export function planTask(payload) {
  return http.post('/workflow/plan', payload)
}

export function runTask(taskId) {
  return http.post('/workflow/run', { task_id: taskId })
}

export function fetchWorkflowTask(taskId) {
  return http.get(`/workflow/${taskId}`)
}

export function listWorkflowTasks(params = {}) {
  return http.get('/workflow/list', { params })
}

export function confirmStep(taskId, stepId, action) {
  return http.post(`/workflow/${taskId}/confirm`, { step_id: stepId, action })
}

export function cancelTask(taskId) {
  return http.post(`/workflow/${taskId}/cancel`)
}

export function pauseTask(taskId) {
  return http.post(`/workflow/${taskId}/pause`)
}

export function resumeTask(taskId) {
  return http.post(`/workflow/${taskId}/resume`)
}

/**
 * 订阅任务事件；事件包括 snapshot / plan / step_start / step_done / step_error /
 * confirm_required / task_status / task_done。返回 { abort }。
 */
export function streamWorkflowEvents(taskId, handlers = {}) {
  const controller = new AbortController()
  let closed = false

  async function run() {
    let resp
    try {
      resp = await fetch(`/api/workflow/${taskId}/events`, {
        method: 'GET',
        headers: { Accept: 'text/event-stream' },
        signal: controller.signal
      })
    } catch (e) {
      if (e.name !== 'AbortError') handlers.onError?.(e)
      return
    }
    if (!resp.ok || !resp.body) {
      const err = new Error(`任务事件连接失败（${resp.status}）`)
      err.httpStatus = resp.status
      notifyError(err)
      handlers.onError?.(err)
      return
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    try {
      while (!closed) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''
        for (const frame of frames) {
          const parsed = parseFrame(frame)
          if (!parsed) continue
          if (parsed.event === 'snapshot') handlers.onSnapshot?.(parsed.data)
          else handlers.onEvent?.(parsed.event, parsed.data)
          if (parsed.event === 'task_done') {
            handlers.onDone?.(parsed.data)
            closed = true
          }
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') handlers.onError?.(e)
    } finally {
      try { reader.releaseLock() } catch { /* 已释放 */ }
    }
  }

  run()
  return {
    abort() {
      closed = true
      controller.abort()
    }
  }
}

function parseFrame(frame) {
  let event = 'message'
  const dataLines = []
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}
