/** 纯工具函数：不依赖 Vue，也不需要浏览器 API，方便直接测。 */

export function formatNumber(value: number, digits = 0): string {
  if (!Number.isFinite(value)) {
    return '—'
  }
  return value.toFixed(digits)
}

export function formatDateTime(input: Date | string | number, locale = 'zh-CN'): string {
  const date = input instanceof Date ? input : new Date(input)
  if (Number.isNaN(date.getTime())) {
    return '—'
  }
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export function truncate(text: string, max = 20): string {
  if (text.length <= max) {
    return text
  }
  return `${text.slice(0, max)}…`
}
