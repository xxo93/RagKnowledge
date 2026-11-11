// 流式正文渲染调度：流式期间 60ms 内合并刷新，再批量重绘；非流式立即刷新。
// 注：页面在后台/被遮挡时浏览器会暂停 requestAnimationFrame，若只依赖 rAF，
// 正文会一直空白到页面重新可见（例如后台标签页、截图取证时）。因此在
// document.hidden 时退化为 setTimeout，保证内容仍能及时渲染。
import { onBeforeUnmount } from 'vue'

export function useStreamRender(render, streamingRef) {
  let rafId = null
  let timerId = null

  function flush() {
    rafId = null
    render()
  }

  function scheduleFlush() {
    if (typeof document !== 'undefined' && document.hidden) {
      timerId = setTimeout(() => {
        timerId = null
        flush()
      }, 0)
      return
    }
    rafId = requestAnimationFrame(flush)
  }

  function schedule() {
    if (rafId !== null) cancelAnimationFrame(rafId)
    if (timerId !== null) clearTimeout(timerId)

    if (streamingRef?.value) {
      timerId = setTimeout(() => {
        timerId = null
        scheduleFlush()
      }, 60)
    } else {
      scheduleFlush()
    }
  }

  onBeforeUnmount(() => {
    if (rafId !== null) cancelAnimationFrame(rafId)
    if (timerId !== null) clearTimeout(timerId)
    rafId = null
    timerId = null
  })

  return { schedule }
}
