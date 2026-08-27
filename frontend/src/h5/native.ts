/**
 * 原生壳桥接。
 *
 * ⚠️ **刻意不 import `@capacitor/core`**：那个包会进 H5 产物，而网页版（服务器 /h5/）
 *   根本没有壳，白背一份体积。Capacitor 在 APP 里会自己往 window 上挂 `Capacitor.Plugins`，
 *   这里直接鸭子类型取用即可 —— H5 包的依赖仍然只有 vue + vue-router + axios。
 *
 * 每个方法在浏览器里都是安全空实现：**H5 必须能在没有壳的情况下照常跑**，
 * 否则改一次桥接就把网页版带塌了。
 */

type PluginMap = Record<string, any>

function plugins(): PluginMap {
  return (typeof window !== 'undefined' && (window as any).Capacitor?.Plugins) || {}
}

function updater(): any | null {
  return plugins().PmsUpdater || null
}

let reported = false

/**
 * 告诉壳「这个前端包启动成功了」。
 *
 * ⚠️ 这是热更新回滚机制的**唯一依据**：壳装好新包后先标成「试用」，
 *   只有收到这一声才转正。收不到（白屏、JS 报错、包缺文件）下次启动自动退回旧包。
 *   桌面客户端 1.0.30/1.0.31「装上打不开」就是因为没有这道回执 ——
 *   包发出去了，坏了也没人知道，只能等用户来喊。
 */
export function notifyReady(): void {
  if (reported) return
  reported = true
  try { updater()?.notifyReady() } catch { /* 没壳就没这回事 */ }
}

/**
 * 启动失败，别让壳干等那 10 秒超时。
 *
 * ⚠️ **报过平安之后一律忽略**：起来之后某个组件抛异常是业务 bug，
 *   把整个前端包回滚掉是过度反应 —— 回滚只该对付「装上打不开」。
 */
export function notifyFailed(reason: string): void {
  if (reported) return
  reported = true
  try { updater()?.notifyFailed({ reason: String(reason).slice(0, 500) }) } catch { /* 同上 */ }
}

export interface ShellInfo {
  /** 壳（APK）版本，如 2.0.0 */
  shellVersion: string
  /** 当前生效的前端包版本；用内置包时是内置包的版本 */
  bundleVersion: string
  /** 当前前端包是不是内置的（还没热更新过） */
  builtin: boolean
}

export async function shellInfo(): Promise<ShellInfo | null> {
  try { return (await updater()?.info()) ?? null } catch { return null }
}

/** 手动查一次更新（设置页那个「检查更新」按钮）。返回 null 表示不在壳里。 */
export async function checkUpdate(): Promise<{ status: string; version?: string; message?: string } | null> {
  try { return (await updater()?.check()) ?? null } catch { return null }
}

// ─────────────────────────── 语音 ───────────────────────────
// Android WebView **没有** Web Speech API（`webkitSpeechRecognition` 是 Chrome 浏览器
// 的功能，WebView 里从来就取不到）。所以壳里必须把系统的 SpeechRecognizer 桥出来，
// 否则 APP 里麦克风按钮永远是隐藏的 —— 这正是「网页上能用、APP 里没有」的那类不兼容。

export function nativeSpeechAvailable(): boolean {
  return !!plugins().PmsSpeech
}

export interface NativeSpeechHandle { stop: () => void }

/**
 * 🆕 云端录音前先把麦克风的**运行时权限**要到手。
 * WebView 的 getUserMedia 只放行应用已持有的权限，自己不弹授权框——
 * 不先要权限，getUserMedia 必然 NotAllowedError。
 * ⚠️ 老 APK 没有 ensureMic 方法：调用会抛异常 → 返回 true 放行，
 *    让 getUserMedia 自己试（行为等同没有这层，绝不能因为壳旧把功能整个堵死）。
 */
export async function ensureNativeMic(): Promise<boolean> {
  const p = plugins().PmsSpeech
  if (!p) return true            // 不在 APP 里：浏览器自己会弹权限框
  try {
    const r = await p.ensureMic()
    return r?.granted !== false
  } catch {
    return true                  // 老壳没这个方法：放行，别把路堵死
  }
}

/**
 * 起一次原生识别。onText(文本, 是否最终结果)，onError(可读文案)。
 * 权限请求在原生侧做（RECORD_AUDIO），这里不用管。
 */
export async function startNativeSpeech(
  onText: (text: string, final: boolean) => void,
  onError: (msg: string) => void,
  onEnd: () => void,
): Promise<NativeSpeechHandle | null> {
  const p = plugins().PmsSpeech
  if (!p) return null
  const listeners: any[] = []
  try {
    listeners.push(await p.addListener('partial', (e: any) => onText(String(e?.text || ''), false)))
    listeners.push(await p.addListener('result', (e: any) => onText(String(e?.text || ''), true)))
    listeners.push(await p.addListener('error', (e: any) => onError(String(e?.message || '语音识别不可用，请打字'))))
    listeners.push(await p.addListener('end', () => onEnd()))
    await p.start({ lang: 'zh-CN' })
  } catch (e: any) {
    listeners.forEach((l) => { try { l.remove() } catch { /* ignore */ } })
    onError(e?.message || '语音识别启动失败，请打字')
    return null
  }
  return {
    stop: () => {
      try { p.stop() } catch { /* 已经停了 */ }
      listeners.forEach((l) => { try { l.remove() } catch { /* ignore */ } })
    },
  }
}
