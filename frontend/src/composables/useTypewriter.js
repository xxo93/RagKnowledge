// 打字机观感：把流式增量按节奏吐给渲染层，避免整段瞬间蹦出。
//
// 规则：
// - 只在 `streaming` 期间播放；历史消息 / 非流式回答直接整段显示（刷新后不会重播）；
// - 每 TICK_MS 吐 BASE_CHARS 个字符，积压越多单次吐得越多（保证 done 后 ~0.3s 内追平）；
// - streaming 结束即立刻补齐剩余字符，避免"回答完了但正文还在慢慢爬"；
// - 组件卸载清定时器；消息被重置（长度回退）时清空已显示内容。
import { onBeforeUnmount, ref, watch } from 'vue'

const BASE_CHARS = 2
const TICK_MS = 22

export function useTypewriter(getText, getStreaming) {
  const shown = ref('')
  let timer = null
  let latest = ''

  function stop() {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  function tick() {
    timer = null
    const remain = latest.length - shown.value.length
    if (remain <= 0) return
    // 积压越大步长越大：短回答保持"逐字"节奏，长回答不会严重滞后
    const step = BASE_CHARS + Math.floor(remain / 60)
    shown.value = latest.slice(0, shown.value.length + Math.min(step, remain))
    if (shown.value.length < latest.length) schedule()
  }

  function schedule() {
    stop()
    timer = setTimeout(tick, TICK_MS)
  }

  watch(
    [() => getText() || '', () => Boolean(getStreaming())],
    ([full, streaming]) => {
      latest = full
      if (!streaming) {
        // 流式结束 / 历史记录：直接补齐，不播动画
        shown.value = latest
        stop()
        return
      }
      if (shown.value.length > latest.length) shown.value = ''
      if (shown.value.length < latest.length && timer === null) schedule()
    },
    { immediate: true }
  )

  onBeforeUnmount(stop)

  return shown
}
