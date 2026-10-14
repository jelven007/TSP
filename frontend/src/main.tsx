import React from 'react'
import ReactDOM from 'react-dom/client'
import { RouterProvider } from 'react-router-dom'
import { QueryClient, QueryCache } from '@tanstack/react-query'
import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { initializeFrontendExtensions } from './extensions/bootstrap'
import { ApiError } from './lib/api/client'
import { api } from './lib/api'
import { accountScope, clearAccountCache } from './lib/accountSession'
import { createAppPersister, shouldPersistQuery, PERSIST_BUSTER } from './lib/queryPersist'
// 字体自托管 (@fontsource): 替代 rsms.me / Google Fonts 渲染阻塞外链,
// 内网/离线部署不再白屏等字体。权重覆盖 tailwind 全部用量 (300-900)。
import '@fontsource/inter/300.css'
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/inter/900.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import '@fontsource/jetbrains-mono/600.css'
import '@fontsource/jetbrains-mono/700.css'
import './index.css'

// 全局认证拦截: 会话失效或系统尚无账户时跳转登录页。
const _redirectToLogin = (() => {
  let redirecting = false
  return (err: unknown) => {
    if (redirecting) return
    if (!(err instanceof ApiError)) return
    const needsAuth = err.status === 401
      || (err.status === 403 && err.message.includes('尚未创建账户'))
    if (!needsAuth) return
    if (window.location.pathname === '/login') return
    redirecting = true
    clearAccountCache()
    const redirect = encodeURIComponent(window.location.pathname + window.location.search)
    window.location.href = `/login?redirect=${redirect}`
  }
})()

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (err) => _redirectToLogin(err),
  }),
  defaultOptions: {
    queries: {
      staleTime: 5_000,           // 5s 内复用,与 §4.2 Repository 不变量一致
      refetchOnWindowFocus: false,
    },
    mutations: {
      onError: (err) => _redirectToLogin(err),
    },
  },
})

async function bootstrap() {
  // Confirm identity before importing stores or restoring any private cache.
  const auth = await api.authStatus()
  try { window.localStorage.removeItem('tf-query-cache') } catch { /* Storage unavailable. */ }
  queryClient.setQueryData(['auth-status'], auth)
  await initializeFrontendExtensions()
  const { router } = await import('./router')
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <PersistQueryClientProvider
        client={queryClient}
        persistOptions={{
          persister: createAppPersister(),
          buster: `${PERSIST_BUSTER}:${accountScope()}`,
          // 恢复超过 1 天的缓存直接丢弃 (慢变族一天内必然后台刷新过)
          maxAge: 24 * 60 * 60 * 1000,
          dehydrateOptions: { shouldDehydrateQuery: shouldPersistQuery },
        }}
      >
        <RouterProvider router={router} />
      </PersistQueryClientProvider>
    </React.StrictMode>,
  )
}

void bootstrap().catch(() => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <main className="grid min-h-screen place-items-center bg-base text-foreground">
      <div className="space-y-4 text-center">
        <p>暂时无法连接服务，请重试。</p>
        <button className="rounded-btn bg-accent px-4 py-2 text-white" onClick={() => window.location.reload()}>重新连接</button>
      </div>
    </main>,
  )
})
