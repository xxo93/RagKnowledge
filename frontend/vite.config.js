// Vite 开发配置（架构 §2.3 / §6.4）
// 开发期把 /api 代理到本地后端 127.0.0.1:8000，避免跨域分散配置；CORS 在后端作为兜底。
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'vue-vendor': ['vue'],
          'ui-vendor': ['element-plus', 'axios'],
          'markdown-vendor': ['marked', 'dompurify', 'highlight.js']
        }
      }
    }
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      }
    }
  }
})
