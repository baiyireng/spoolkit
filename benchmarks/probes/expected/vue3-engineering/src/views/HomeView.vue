<script setup lang="ts">
import { computed } from 'vue'

import { useCounter } from '@/composables/useCounter'
import { formatDateTime, formatNumber } from '@/utils/format'

const { count, doubled, label, increment, reset } = useCounter()

const now = computed(() => formatDateTime(new Date('2026-01-01T09:30:00Z')))
const formattedCount = computed(() => formatNumber(count.value))
</script>

<template>
  <section class="home">
    <h2>概览</h2>
    <p>这是一页示例内容，用来把路由、状态与工具函数的接线走通。</p>
    <dl class="home__stats">
      <dt>计数</dt>
      <dd>{{ formattedCount }}</dd>
      <dt>两倍</dt>
      <dd>{{ doubled }}</dd>
      <dt>状态</dt>
      <dd>{{ label }}</dd>
      <dt>示例时间</dt>
      <dd>{{ now }}</dd>
    </dl>
    <div class="home__actions">
      <button type="button" @click="increment()">+1</button>
      <button type="button" @click="increment(5)">+5</button>
      <button type="button" @click="reset()">重置</button>
    </div>
  </section>
</template>
