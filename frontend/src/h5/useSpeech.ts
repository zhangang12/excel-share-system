/**
 * 语音输入（Web Speech API）。
 *
 * 老实说清楚它的边界，别让人以为哪都能用：
 *   Android Chrome / 鸿蒙浏览器  可用
 *   iOS Safari 14.5+            可用，但必须由用户手势触发，且识别在苹果服务器上做
 *   企业微信 / 微信内置浏览器     多半不可用（内核裁剪掉了这个 API）
 *
 * 所以这里做能力探测：拿不到 API 就把麦克风按钮整个隐藏，
 * 而不是显示一个点了没反应的按钮——那比没有更糟。
 *
 * 隐私：识别由浏览器/系统完成，音频不经过我们的服务器，也不落库。
 * 若将来要在企微里用，得换企微 JS-SDK 的 translateVoice，那是另一套鉴权，不在本期。
 */
import { ref, onUnmounted } from 'vue'

type SR = any

function getCtor(): SR | null {
  const w = window as any
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

export function useSpeech(onText: (text: string, final: boolean) => void) {
  const supported = !!getCtor()
  const listening = ref(false)
  const error = ref('')
  let rec: SR = null

  function stop() {
    try { rec?.stop() } catch { /* 已经停了 */ }
    listening.value = false
  }

  function start() {
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

  function toggle() { listening.value ? stop() : start() }

  onUnmounted(stop)
  return { supported, listening, error, start, stop, toggle }
}
