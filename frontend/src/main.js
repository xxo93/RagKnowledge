// 前端入口：挂载 Vue 并注册 Element Plus
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/theme.css'
import './styles/markdown.css'
import App from './App.vue'

createApp(App).use(ElementPlus).mount('#app')
