// 主题切换：浅色/暗色变量通过 <html data-theme> 控制，并记忆到 localStorage。
import { ref } from 'vue'

const STORAGE_KEY = 'rag-theme'
// <html data-theme="..."> 的属性名；写入必须用 setAttribute。
// 不可写成 dataset['data-theme']：DOMStringMap 只接受去掉 data- 前缀的驼峰键，
// 带前缀的键名会抛 TypeError，导致 onMounted 中断、登录校验一直停留在遮罩。
const THEME_ATTR = 'data-theme'

function readStoredTheme() {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return value === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

const theme = ref(readStoredTheme())

function applyTheme(value) {
  theme.value = value === 'dark' ? 'dark' : 'light'
  const root = document.documentElement
  root.setAttribute(THEME_ATTR, theme.value)
  root.classList.toggle('dark', theme.value === 'dark')
  try {
    window.localStorage.setItem(STORAGE_KEY, theme.value)
  } catch {
    // 隐私模式或存储被禁用时不阻断界面使用
  }
}

export function useTheme() {
  function toggleTheme() {
    applyTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  function initTheme() {
    applyTheme(theme.value)
  }

  return { theme, toggleTheme, initTheme }
}
