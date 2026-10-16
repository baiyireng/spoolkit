import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AppHeader from '@/components/AppHeader.vue'

describe('AppHeader', () => {
  it('渲染标题', () => {
    const wrapper = mount(AppHeader, { props: { title: '控制台' } })
    expect(wrapper.text()).toContain('控制台')
  })

  it('没给标题时用默认值', () => {
    const wrapper = mount(AppHeader, { global: { stubs: { RouterLink: true } } })
    expect(wrapper.text()).toContain('后台管理')
  })
})
