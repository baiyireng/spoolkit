/**
 * 最小请求层：统一 baseURL、超时、JSON 解析与错误归一。
 *
 * 后端还没定，所以这里只有一层可替换的封装；真实接口接进来时只需要加函数。
 */

export interface ApiError {
  status: number
  message: string
}

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'
const DEFAULT_TIMEOUT_MS = 8000

export class ApiRequestError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
  }
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...(init.headers ?? {}),
      },
    })
    if (!response.ok) {
      throw new ApiRequestError(response.status, `请求失败：${response.status}`)
    }
    return (await response.json()) as T
  } catch (error) {
    if (error instanceof ApiRequestError) {
      throw error
    }
    const message = error instanceof Error ? error.message : String(error)
    throw new ApiRequestError(0, message)
  } finally {
    clearTimeout(timer)
  }
}

export function get<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'GET' })
}
