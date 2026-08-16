/**
 * 🆕 企业微信内嵌：静默登录。
 *
 * 企微工作台/消息卡片打开 `/api/auth/wecom/entry` → 后端 302 到企微授权 →
 * 企微带 `?code=xxx` 回到 `/h5/` → 这里把 code 换成 token，用户全程无感。
 *
 * ⚠️ 只在**企微客户端内**尝试。放到普通浏览器里会白等一次网络请求，
 *    而且 code 只能从企微来，拿不到也没意义。
 * ⚠️ 失败一律**静默退回普通登录页**，绝不弹错。这条路径是「顺手帮你登上」，
 *    不是必经之路；它挂了用户还能输密码，弹一个看不懂的错只会吓人。
 */
import { http } from './http'
import { setSession } from './session'

/** 企微内置浏览器的 UA 里一定有 wxwork（企业微信），微信是 micromessenger */
export function inWecom(): boolean {
  return /wxwork/i.test(navigator.userAgent)
}

/**
 * 从 URL 上摘 code。
 *
 * ⚠️ 企微把 `?code=` 挂在 **hash 之前**（`/h5/?code=X&state=Y#/`），
 *    但 hash 路由跑起来之后地址可能变成 `/h5/#/?code=X`。两处都得看，
 *    只看 location.search 在部分机型上取不到。
 */
function pickCode(): string {
  const fromSearch = new URLSearchParams(location.search).get('code')
  if (fromSearch) return fromSearch
  const h = location.hash || ''
  const q = h.indexOf('?')
  return q >= 0 ? (new URLSearchParams(h.slice(q + 1)).get('code') || '') : ''
}

/** 把 code 从地址栏抹掉：留着的话刷新会拿一个**已用过**的 code 再试一次，必然失败。 */
function stripCode() {
  const url = new URL(location.href)
  url.searchParams.delete('code')
  url.searchParams.delete('state')
  history.replaceState(null, '', url.pathname + url.search + url.hash)
}

/** 返回 true = 已经登上，调用方直接进首页。 */
export async function tryWecomLogin(): Promise<boolean> {
  if (!inWecom()) return false
  const code = pickCode()
  if (!code) return false
  stripCode()
  try {
    const { data } = await http.post('/auth/wecom', { code })
    setSession(data.access_token, data.user)
    return true
  } catch {
    return false      // 静默退回普通登录页
  }
}
