<script setup lang="ts">
import { get } from '@/api/http'

// 后端还没有，这里只演示请求层怎么被页面调用（按钮点了会失败，属于预期）。
async function probeBackend(): Promise<void> {
  try {
    await get<{ ok: boolean }>('/health')
  } catch {
    // 示例页：失败是正常的，不弹错
  }
}
</script>

<template>
  <section class="about">
    <h2>关于</h2>
    <p>示例工程骨架：路由、状态、请求层、组合式函数、工具函数各有一份最小实现。</p>
    <button type="button" @click="probeBackend()">试一下请求层</button>
  </section>
</template>
