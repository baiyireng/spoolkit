import { storeToRefs } from 'pinia'

import { useCounterStore } from '@/stores/counter'

/**
 * 组合式函数：把 store 的用法包一层，页面里只关心「拿到什么、能做什么」。
 */
export function useCounter() {
  const store = useCounterStore()
  const { count, doubled, label } = storeToRefs(store)

  function increment(step = 1): void {
    store.increment(step)
  }

  function reset(): void {
    store.reset()
  }

  return { count, doubled, label, increment, reset }
}
