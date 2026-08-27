/**
 * 语音输入。**三套后端**，能力探测决定用哪个：
 *
 *   ① 原生（APP 内）—— 壳把系统 SpeechRecognizer 桥出来（免费、边说边出字）
 *   ② Web Speech API（浏览器内）—— webkitSpeechRecognition
 *   ③ 🆕 云端识别（阿里云一句话识别）—— 录 PCM 传后端代理，说完出整段
 *
 * ⚠️ 为什么必须有 ①：**Android WebView 里根本没有 Web Speech API**。
 * ⚠️ 为什么必须有 ③：原生 SpeechRecognizer 依赖系统语音服务，无 GMS 的
 *   国产机（生产实测：华为）直接报「这台手机没有可用的语音识别服务」。
 *   云端识别是唯一能覆盖所有手机的路。①失败会**自动切到③**（若已开通）。
 *
 * 选路顺序：原生可用 → ①；否则浏览器有 Web Speech → ②；否则问一次后端
 * `/speech/available`，开通了且拿得到 getUserMedia → ③；全没有 → 按钮隐藏。
 *
 * ③ 的录音：getUserMedia + ScriptProcessor 采 PCM，降采样到 16k/16bit 单声道。
 * ⚠️ 刻意不用 MediaRecorder：它吐 webm/opus 容器，各 WebView 支持参差、
 *   服务端还得解封装；裸 PCM 在哪都一样，后端原样转发即可。
 * ⚠️ ScriptProcessor 虽是废弃 API，但老 WebView 全兼容——这里要的就是兼容。
 *
 * 隐私：①② 音频不经过我们服务器；③ 会经服务器转发到阿里云识别，不落库不落盘。
 */
import { ref, onUnmounted } from 'vue'
import { http } from './http'
import { nativeSpeechAvailable, startNativeSpeech, type NativeSpeechHandle } from './native'

type SR = any

function getCtor(): SR | null {
  const w = window as any
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

function canRecord(): boolean {
  // window.AudioContext 在 lib.dom 里恒有类型，但老 WebView 运行时可能没有——
  // 走 any 探测，别让 TS 以为「总是真」
  const w = window as any
  return typeof navigator.mediaDevices?.getUserMedia === 'function'
    && !!(w.AudioContext || w.webkitAudioContext)
}

/** Float32 任意采样率 → Int16 16kHz 单声道（线性插值）。 */
function toPcm16k(chunks: Float32Array[], srcRate: number): ArrayBuffer {
  let n = 0
  for (const c of chunks) n += c.length
  const all = new Float32Array(n)
  let off = 0
  for (const c of chunks) { all.set(c, off); off += c.length }
  const ratio = srcRate / 16000
  const outLen = Math.floor(all.length / ratio)
  const out = new Int16Array(outLen)
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio
    const i0 = Math.floor(pos)
    const frac = pos - i0
    const s = all[i0] * (1 - frac) + (all[Math.min(i0 + 1, all.length - 1)] || 0) * frac
    out[i] = Math.max(-1, Math.min(1, s)) * 0x7fff
  }
  return out.buffer
}

export function useSpeech(onText: (text: string, final: boolean) => void,
                          onEnd?: () => void) {
  const useNative = nativeSpeechAvailable()
  // supported 是 ref：云端那条要问一次后端才知道，探到了再把按钮亮出来
  const supported = ref(useNative || !!getCtor())
  const listening = ref(false)
  /** 'idle' | 'rec'（正在听）| 'asr'（云端识别中） */
  const phase = ref<'idle' | 'rec' | 'asr'>('idle')
  const error = ref('')
  /** 🆕 云端录音的实时音量（0~1 一串），驱动 UI 的声波条。原生/浏览器路径拿不到，
   *  为空时 UI 退回 CSS 循环动画 —— 有真数据用真数据，没有也别一片死寂。 */
  const levels = ref<number[]>([])
  let rec: SR = null
  let native: NativeSpeechHandle | null = null
  let starting = false
  /** 原生报「没有语音服务」之后置 true，后续都直接走云端 */
  let preferCloud = false
  let cloudEnabled = false
  /** 🆕 会话结束通知（去重）。**自动发送挂在这上面**，不挂在识别器的 final 标记上——
   *  有些手机的识别器只给中间结果、从不报 final，等 final 的自动发送永远等不到
   *  （用户实测：字出来了但不发）。`end` 是唯一每条路径都保证有的信号。
   *  取消/出错时置 ended=true 拦掉通知：用户主动放弃或报错后不该替他把话发出去。 */
  let sessionEnded = true
  function notifyEnd() {
    if (sessionEnded) return
    sessionEnded = true
    onEnd?.()
  }

  // 云端录音现场
  let stream: MediaStream | null = null
  let ctx: AudioContext | null = null
  let proc: ScriptProcessorNode | null = null
  let pcmChunks: Float32Array[] = []
  let recTimer = 0

  // 探测云端：只有原生和 Web Speech 都没有时才问（省一次请求）
  if (!supported.value && canRecord()) {
    http.get('/speech/available')
      .then(({ data }) => {
        if (data?.enabled) { cloudEnabled = true; supported.value = true }
      })
      .catch(() => { /* 探测失败按不可用处理，按钮维持隐藏 */ })
  } else if (useNative && canRecord()) {
    // APP 里也悄悄探一次：原生报「没有语音服务」时要有云端可切
    http.get('/speech/available')
      .then(({ data }) => { cloudEnabled = !!data?.enabled })
      .catch(() => { /* 没有就没有 */ })
  }

  function cleanupCloud() {
    if (recTimer) { clearTimeout(recTimer); recTimer = 0 }
    try { proc?.disconnect() } catch { /* 已断开 */ }
    try { ctx?.close() } catch { /* 已关闭 */ }
    stream?.getTracks().forEach((t) => t.stop())
    proc = null; ctx = null; stream = null
  }

  async function startCloud() {
    if (listening.value) return
    error.value = ''
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      })
    } catch {
      error.value = '需要允许麦克风权限（在系统设置里给本应用开麦克风）'
      return
    }
    const AC = window.AudioContext || (window as any).webkitAudioContext
    ctx = new AC()
    const src = ctx!.createMediaStreamSource(stream)
    proc = ctx!.createScriptProcessor(4096, 1, 1)
    pcmChunks = []
    levels.value = []
    proc.onaudioprocess = (e) => {
      const buf = e.inputBuffer.getChannelData(0)
      pcmChunks.push(new Float32Array(buf))
      // RMS → 0~1。×4 是经验增益：正常说话 RMS 大约 0.05~0.2，不放大条子几乎不动
      let sum = 0
      for (let i = 0; i < buf.length; i += 8) sum += buf[i] * buf[i]
      const rms = Math.sqrt(sum / (buf.length / 8))
      levels.value.push(Math.min(1, rms * 4))
      if (levels.value.length > 28) levels.value.shift()
    }
    src.connect(proc)
    proc.connect(ctx!.destination)
    listening.value = true
    phase.value = 'rec'
    sessionEnded = false
    // 一句话识别上限 60 秒，到点自动收——别让人白说后半段
    recTimer = window.setTimeout(() => { void stopCloud() }, 58_000)
  }

  async function stopCloud() {
    if (phase.value !== 'rec') { cleanupCloud(); listening.value = false; return }
    const rate = ctx?.sampleRate || 48000
    cleanupCloud()
    phase.value = 'asr'
    try {
      const pcm = toPcm16k(pcmChunks, rate)
      pcmChunks = []
      if (pcm.byteLength < 3200) {
        error.value = '没听清，说长一点再试'
        return
      }
      const { data } = await http.post('/speech/recognize', pcm, {
        headers: { 'Content-Type': 'application/octet-stream' },
        timeout: 25_000,
      })
      if (data?.text) onText(data.text, true)
    } catch (e: any) {
      error.value = e?.response?.data?.detail || '识别失败，再说一次试试'
    } finally {
      phase.value = 'idle'
      listening.value = false
      notifyEnd()          // 失败也通知——上层看有没有字决定发不发
    }
  }

  /** 🆕 取消：录到一半不想要了。云端路径**直接丢弃不上传**（不花钱不等待）；
   *  其余路径等同 stop。 */
  function cancel() {
    sessionEnded = true          // 用户主动放弃：不触发自动发送
    if (phase.value === 'rec') {
      pcmChunks = []
      cleanupCloud()
      phase.value = 'idle'
      listening.value = false
      return
    }
    stop()
  }

  function stop() {
    if (phase.value === 'rec') { void stopCloud(); return }
    if (useNative) {
      native?.stop()
      native = null
      // ⚠️ 手动停时 native.stop() 会先摘掉事件监听器，end 事件到不了——
      //    这里补一次通知（notifyEnd 自带去重，自然结束那条不会重复）
      notifyEnd()
    } else {
      try { rec?.stop() } catch { /* 已经停了 */ }
    }
    listening.value = false
  }

  async function startNative() {
    if (starting || listening.value) return
    starting = true
    error.value = ''
    listening.value = true
    sessionEnded = false
    try {
      native = await startNativeSpeech(
        onText,
        (msg) => {
          listening.value = false; native = null
          sessionEnded = true          // 出错不自动发送
          // 🆕 这台机没有系统语音服务（华为等无 GMS 机型）→ 自动切云端
          if (cloudEnabled && /语音识别服务/.test(msg)) {
            preferCloud = true
            error.value = '本机没有语音服务，已切换云端识别，请再按一次'
          } else {
            error.value = msg
          }
        },
        () => { listening.value = false; native = null; notifyEnd() },
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
      error.value = e.error === 'not-allowed' ? '需要允许麦克风权限'
        : e.error === 'no-speech' ? ''
        : '语音识别不可用，请打字'
      sessionEnded = true          // 出错不自动发送
      listening.value = false
    }
    rec.onend = () => { listening.value = false; notifyEnd() }

    try {
      rec.start()
      listening.value = true
      sessionEnded = false
    } catch {
      error.value = '语音识别启动失败，请打字'
      listening.value = false
    }
  }

  function start() {
    if (preferCloud && cloudEnabled) { void startCloud(); return }
    if (useNative) { void startNative(); return }
    if (getCtor()) { startWeb(); return }
    if (cloudEnabled) { void startCloud() }
  }
  function toggle() { listening.value ? stop() : start() }

  onUnmounted(() => { cleanupCloud(); stop() })
  return { supported, listening, phase, error, levels, start, stop, cancel, toggle }
}
