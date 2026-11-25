import { defineStore } from 'pinia'

export interface CounterState {
  count: number
}

/**
 * 示例 store：只演示「状态 + 派生值 + 动作」三件事怎么写。
 */
export const useCounterStore = defineStore('counter', {
  state: (): CounterState => ({
    count: 0,
  }),
  getters: {
    doubled: (state): number => state.count * 2,
    label: (state): string => (state.count > 0 ? `已点 ${state.count} 次` : '还没点过'),
  },
  actions: {
    increment(step = 1): void {
      this.count += step
    },
    reset(): void {
      this.count = 0
    },
  },
})
