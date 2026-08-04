/**
 * 语音输入。**两套后端**，能力探测决定用哪个：
 *
 *   ① 原生（APP 内）—— 壳把系统 SpeechRecognizer 桥出来
 *   ② Web Speech API（浏览器内）—— webkitSpeechRecognition
 *
 * ⚠️ 为什么必须有 ①：**Android WebView 里根本没有 Web Speech API**。
 *   `webkitSpeechRecognition` 是 Chrome 浏览器的功能，WebView 不带语音识别服务绑定，
 *   一律取不到构造函数。所以旧版 APP（纯 WebView 壳）里麦克风按钮**永远是隐藏的** ——
 *   网页上能用、APP 里没有，用户看到的就是「不兼容」。走原生桥接才补得上。
 *
 * 各端边界（说清楚，别让人以为哪都能用）：
 *   同辉 APP（原生桥接）        可用
 *   Android Chrome / 鸿蒙浏览器  可用（Web Speech）
 *   iOS Safari 14.5+            可用，但必须由用户手势触发，且识别在苹果服务器上做
 *   企业微信 / 微信内置浏览器     多半不可用（内核裁剪掉了这个 API）
 *
 * 拿不到任何一种就把麦克风按钮整个隐藏，而不是显示一个点了没反应的按钮——那比没有更糟。
 *
 * 隐私：识别由系统/浏览器完成，音频不经过我们的服务器，也不落库。
 * 若将来要在企微里用，得换企微 JS-SDK 的 translateVoice，那是另一套鉴权，不在本期。
 */
import { ref, onUnmounted } from 'vue'
import { nativeSpeechAvailable, startNativeSpeech, type NativeSpeechHandle } from './native'

type SR = any

function getCtor(): SR | null {
  const w = window as any
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

export function useSpeech(onText: (text: string, final: boolean) => void) {
  const useNative = nativeSpeechAvailable()
  const supported = useNative || !!getCtor()
  const listening = ref(false)
  const error = ref('')
  let rec: SR = null
  let native: NativeSpeechHandle | null = null
  /** 防重入：原生 start 要往返一次，连点两下会起两路识别 */
  let starting = false

  function stop() {
    if (useNative) {
      native?.stop()
      native = null
    } else {
      try { rec?.stop() } catch { /* 已经停了 */ }
    }
    listening.value = false
  }

  async function startNative() {
    if (starting || listening.value) return
    starting = true
    error.value = ''
    // 先亮起来：起原生会话要往返一次，不先置 true 会有一段「点了没反应」
    listening.value = true
    try {
      native = await startNativeSpeech(
        onText,
        (msg) => { error.value = msg; listening.value = false; native = null },
        () => { listening.value = false; native = null },
      )
      if (!native) listening.value = false
    } finally {
      starting = false
    }
  }

  function startWeb() {
    const Ctor = getCtor()
    if (!Ctor || listening.value) return
    error.value = ''
    rec = new Ctor()
    rec.lang = 'zh-CN'
    rec.continuous = false
    rec.interimResults = true      // 边说边出字，比说完才蹦出来踏实

    rec.onresult = (e: any) => {
      let text = ''
      let final = false
      for (let i = e.resultIndex; i < e.results.length; i++) {
        text += e.results[i][0].transcript
        if (e.results[i].isFinal) final = true
      }
      onText(text, final)
    }
    rec.onerror = (e: any) => {
      // not-allowed = 用户拒了麦克风权限；no-speech = 没说话。两种都不该报红
      error.value = e.error === 'not-allowed' ? '需要允许麦克风权限'
        : e.error === 'no-speech' ? ''
        : '语音识别不可用，请打字'
      listening.value = false
    }
    rec.onend = () => { listening.value = false }

    try {
      rec.start()
      listening.value = true
    } catch {
      error.value = '语音识别启动失败，请打字'
      listening.value = false
    }
  }

  function start() { useNative ? void startNative() : startWeb() }
  function toggle() { listening.value ? stop() : start() }

  onUnmounted(stop)
  return { supported, listening, error, start, stop, toggle }
}
