<script setup lang="ts">
import { RouterLink } from 'vue-router'

withDefaults(defineProps<{ title?: string }>(), { title: '后台管理' })
</script>

<template>
  <header class="app-header">
    <h1 class="app-header__title">{{ title }}</h1>
    <nav class="app-header__nav">
      <RouterLink to="/">概览</RouterLink>
      <RouterLink to="/about">关于</RouterLink>
    </nav>
  </header>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
}

.app-header__title {
  margin: 0;
  font-size: 18px;
}

.app-header__nav {
  display: flex;
  gap: 16px;
}
</style>
