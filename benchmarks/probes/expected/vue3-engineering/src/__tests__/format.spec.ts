import { describe, expect, it } from 'vitest'

import { formatDateTime, formatNumber, truncate } from '@/utils/format'

describe('formatNumber', () => {
  it('按位数补零', () => {
    expect(formatNumber(3.14159, 2)).toBe('3.14')
    expect(formatNumber(7)).toBe('7')
  })

  it('非有限数给占位符', () => {
    expect(formatNumber(Number.NaN)).toBe('—')
    expect(formatNumber(Number.POSITIVE_INFINITY)).toBe('—')
  })
})

describe('formatDateTime', () => {
  it('格式化有效时间', () => {
    expect(formatDateTime(new Date('2026-01-01T09:30:00Z'))).toContain('2026')
  })

  it('无效输入给占位符', () => {
    expect(formatDateTime('不是时间')).toBe('—')
  })
})

describe('truncate', () => {
  it('超长才截断', () => {
    expect(truncate('abcdef', 4)).toBe('abcd…')
    expect(truncate('abc', 4)).toBe('abc')
  })
})
