import { onScopeDispose } from 'vue'

/** 一次页面加载及其后续请求共享取消信号；新加载使旧响应失效。 */
export function useRequestScope<T>(currentKey: () => T) {
  let active: { key: T; controller: AbortController } | null = null
  function begin() {
    active?.controller.abort()
    active = { key: currentKey(), controller: new AbortController() }
    return active
  }
  function isCurrent(scope: typeof active) {
    return scope !== null && scope === active && !scope.controller.signal.aborted && scope.key === currentKey()
  }
  onScopeDispose(() => {
    active?.controller.abort()
    active = null
  })
  return { begin, capture: () => active, isCurrent }
}
