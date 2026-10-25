<script setup lang="ts">
import AppHeader from '@/components/AppHeader.vue'

const title = import.meta.env.VITE_APP_TITLE || '后台管理'
</script>

<template>
  <div class="app">
    <AppHeader :title="title" />
    <main class="app__main">
      <RouterView />
    </main>
  </div>
</template>
