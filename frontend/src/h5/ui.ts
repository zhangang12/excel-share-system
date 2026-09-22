/**
 * 🆕 H5 轻量 UI 组件（2026-09-19 用户目标「美化界面、升级操作体验」）。
 *
 * 之前全站 9 处用原生 window.confirm/prompt/alert：样式突兀、各 WebView 长相
 * 不一（企微 X5 尤其丑）、prompt 连多行都输不了——审批驳回理由就挤在那个小框里。
 * 操作成功后也没有任何反馈，用户不确定点没点上，只能等列表悄悄刷新。
 *
 * 这里给三件套，全按 h5-tokens 的设计体系（玻璃 + 蓝渐变 + 底部抽屉）：
 *   toast(msg, type)               —— 顶部滑入的轻提示，自动消失
 *   uiConfirm({...}) → boolean     —— 底部确认抽屉（危险操作红色主按钮）
 *   uiPrompt({...}) → string|null  —— 底部输入抽屉（支持多行，驳回理由不再挤小框）
 *
 * ⚠️ 刻意用纯 DOM 实现而不是 Vue 组件：
 *   · 任何文件一行 import 就能用，不用每个页面注册组件 + 维护 open 状态
 *   · 命令式 await uiConfirm(...) 与被替换的 confirm(...) 同构，替换是机械操作
 *   · 没有响应式开销，也永远不会和页面自身的弹层状态互相纠缠
 * ⚠️ z-index 90+：页面自己的抽屉 mask 用的是 60，这套必须压在其上
 *   （场景：新建待办抽屉里点「下发」失败要弹 toast）。
 */

let styleInjected = false
function ensureStyle() {
  if (styleInjected) return
  styleInjected = true
  const el = document.createElement('style')
  el.id = 'h5-ui-kit'
  el.textContent = `
.h5k-toasts{position:fixed;top:calc(12px + var(--h5-safe-top, env(safe-area-inset-top)));left:0;right:0;
  display:flex;flex-direction:column;align-items:center;gap:8px;z-index:99;pointer-events:none}
.h5k-toast{display:flex;align-items:center;gap:7px;max-width:86vw;
  background:rgba(23,24,26,.88);color:#fff;font:500 13.5px/1.5 var(--h5-font);
  border-radius:999px;padding:9px 16px;box-shadow:0 8px 24px rgba(24,32,50,.25);
  backdrop-filter:blur(8px);animation:h5kIn .22s cubic-bezier(.2,.9,.3,1.2)}
.h5k-toast.out{animation:h5kOut .18s ease forwards}
.h5k-toast .ic{flex:none;width:17px;height:17px;border-radius:50%;color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700}
.h5k-toast.ok .ic{background:#2A7A52}
.h5k-toast.err .ic{background:#C4362F}
.h5k-toast.info .ic{background:var(--h5-blue,#2B6EF6)}
@keyframes h5kIn{from{opacity:0;transform:translateY(-14px) scale(.94)}to{opacity:1;transform:none}}
@keyframes h5kOut{to{opacity:0;transform:translateY(-10px) scale(.96)}}
.h5k-mask{position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:90;
  display:flex;align-items:flex-end;justify-content:center;opacity:0;transition:opacity .18s ease}
.h5k-mask.show{opacity:1}
.h5k-sheet{width:100%;max-width:560px;background:var(--h5-bg,#ECEEF1);
  border-radius:18px 18px 0 0;padding:18px 18px calc(16px + env(safe-area-inset-bottom));
  box-shadow:0 -12px 40px rgba(24,32,50,.18);
  transform:translateY(100%);transition:transform .24s cubic-bezier(.25,.9,.3,1)}
.h5k-mask.show .h5k-sheet{transform:none}
.h5k-grab{width:36px;height:4px;border-radius:2px;background:rgba(0,0,0,.12);margin:-6px auto 12px}
.h5k-t{font:700 16.5px/1.4 var(--h5-font);color:var(--h5-ink,#17181A)}
.h5k-m{font:400 13.5px/1.7 var(--h5-font);color:var(--h5-ink-2,#4A4F57);
  margin-top:6px;white-space:pre-wrap;word-break:break-word}
.h5k-inp{width:100%;box-sizing:border-box;border:1px solid rgba(255,255,255,.9);
  background:rgba(255,255,255,.85);border-radius:12px;padding:11px 13px;margin-top:12px;
  font:400 15px/1.5 var(--h5-font);color:var(--h5-ink,#17181A);outline:none;resize:none}
.h5k-inp:focus{border-color:var(--h5-blue,#2B6EF6);
  box-shadow:0 0 0 3px rgba(43,110,246,.12)}
.h5k-req{font:400 12px/1 var(--h5-font);color:#C4362F;margin-top:6px;min-height:12px}
.h5k-acts{display:flex;gap:10px;margin-top:16px}
.h5k-b{flex:1;border:0;border-radius:999px;padding:12px 0;text-align:center;
  font:600 14.5px/1 var(--h5-font);cursor:pointer;
  transition:transform .12s ease,opacity .12s ease}
.h5k-b:active{transform:scale(.97)}
.h5k-b.ghost{background:rgba(255,255,255,.9);color:var(--h5-ink-2,#4A4F57);
  border:1px solid rgba(0,0,0,.06)}
.h5k-b.pri{background:var(--h5-grad-btn,#2B6EF6);color:#fff;
  box-shadow:var(--h5-sh-btn-sm,0 8px 18px rgba(43,110,246,.32))}
.h5k-b.dg{background:linear-gradient(135deg,#E05A52,#C4362F);color:#fff;
  box-shadow:0 8px 18px rgba(196,54,47,.28)}
@media (prefers-reduced-motion:reduce){
  .h5k-toast,.h5k-mask,.h5k-sheet,.h5k-b{animation:none!important;transition:none!important}
}`
  document.head.appendChild(el)
}

// ───────────────────────── toast ─────────────────────────
let toastBox: HTMLElement | null = null

export function toast(msg: string, type: 'ok' | 'err' | 'info' = 'ok') {
  ensureStyle()
  if (!toastBox || !document.body.contains(toastBox)) {
    toastBox = document.createElement('div')
    toastBox.className = 'h5k-toasts'
    document.body.appendChild(toastBox)
  }
  const t = document.createElement('div')
  t.className = `h5k-toast ${type}`
  const glyph = type === 'ok' ? '✓' : type === 'err' ? '!' : 'i'
  t.innerHTML = `<span class="ic">${glyph}</span><span></span>`
  ;(t.lastElementChild as HTMLElement).textContent = msg
  toastBox.appendChild(t)
  // 错误多给点阅读时间；同屏最多 3 条，旧的先走
  while (toastBox.children.length > 3) toastBox.firstElementChild?.remove()
  const ttl = type === 'err' ? 3200 : 2000
  setTimeout(() => {
    t.classList.add('out')
    setTimeout(() => t.remove(), 200)
  }, ttl)
}

// ───────────────────────── sheet 骨架 ─────────────────────────
function openSheet(build: (sheet: HTMLElement, close: (v: unknown) => void) => void): Promise<unknown> {
  ensureStyle()
  return new Promise((resolve) => {
    const mask = document.createElement('div')
    mask.className = 'h5k-mask'
    const sheet = document.createElement('div')
    sheet.className = 'h5k-sheet'
    sheet.innerHTML = '<div class="h5k-grab"></div>'
    mask.appendChild(sheet)
    let done = false
    const close = (v: unknown) => {
      if (done) return
      done = true
      mask.classList.remove('show')
      setTimeout(() => mask.remove(), 240)
      resolve(v)
    }
    mask.addEventListener('click', (e) => { if (e.target === mask) close(null) })
    build(sheet, close)
    document.body.appendChild(mask)
    // 下一帧再加 show，让入场动画能跑
    requestAnimationFrame(() => requestAnimationFrame(() => mask.classList.add('show')))
  })
}

function el(tag: string, cls: string, text?: string): HTMLElement {
  const e = document.createElement(tag)
  e.className = cls
  if (text !== undefined) e.textContent = text
  return e
}

// ───────────────────────── uiConfirm ─────────────────────────
export interface ConfirmOpts {
  title: string
  message?: string
  confirmText?: string
  cancelText?: string
  /** 危险操作（删除/撤销/驳回）：主按钮红色 */
  danger?: boolean
}

export function uiConfirm(opts: ConfirmOpts): Promise<boolean> {
  return openSheet((sheet, close) => {
    sheet.appendChild(el('div', 'h5k-t', opts.title))
    if (opts.message) sheet.appendChild(el('div', 'h5k-m', opts.message))
    const acts = el('div', 'h5k-acts')
    const no = el('button', 'h5k-b ghost', opts.cancelText || '取消')
    const yes = el('button', `h5k-b ${opts.danger ? 'dg' : 'pri'}`, opts.confirmText || '确定')
    no.onclick = () => close(false)
    yes.onclick = () => close(true)
    acts.append(no, yes)
    sheet.appendChild(acts)
  }).then((v) => v === true)
}

// ───────────────────────── uiPrompt ─────────────────────────
export interface PromptOpts {
  title: string
  message?: string
  placeholder?: string
  initial?: string
  /** 多行输入（驳回理由、完成说明这类要写几句话的） */
  textarea?: boolean
  /** 必填：空内容点确定时抖一下提示，不关抽屉 */
  required?: boolean
  confirmText?: string
  maxlength?: number
}

export function uiPrompt(opts: PromptOpts): Promise<string | null> {
  return openSheet((sheet, close) => {
    sheet.appendChild(el('div', 'h5k-t', opts.title))
    if (opts.message) sheet.appendChild(el('div', 'h5k-m', opts.message))
    const inp = document.createElement(opts.textarea ? 'textarea' : 'input') as
      HTMLInputElement | HTMLTextAreaElement
    inp.className = 'h5k-inp'
    inp.placeholder = opts.placeholder || ''
    inp.value = opts.initial || ''
    inp.maxLength = opts.maxlength || 500
    if (opts.textarea) (inp as HTMLTextAreaElement).rows = 3
    const req = el('div', 'h5k-req', '')
    const acts = el('div', 'h5k-acts')
    const no = el('button', 'h5k-b ghost', '取消')
    const yes = el('button', 'h5k-b pri', opts.confirmText || '确定')
    no.onclick = () => close(null)
    yes.onclick = () => {
      const v = inp.value.trim()
      if (opts.required && !v) { req.textContent = '这一项必须填写'; inp.focus(); return }
      close(v)
    }
    if (!opts.textarea) {
      inp.addEventListener('keyup', (e) => { if ((e as KeyboardEvent).key === 'Enter') yes.click() })
    }
    sheet.append(inp, req, acts)
    acts.append(no, yes)
    // 弹出即聚焦：少一次点击（键盘会顶起抽屉，visualViewport 由页面处理）
    setTimeout(() => inp.focus(), 260)
  }) as Promise<string | null>
}
