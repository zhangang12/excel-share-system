/**
 * 单元格级 Excel 公式引擎（零依赖）
 *
 * 支持语法：
 *   数字：123、1.5、-0.5
 *   字符串："abc"
 *   字段引用：A1、B2、AA12（按列字母 + 行号，1-based）
 *   算术：+ - * / %
 *   一元负号：-A1
 *   比较：> >= < <= = == <> !=
 *   括号：( )
 *   函数：
 *     IF(cond, a, b)、AND(...)、OR(...)、NOT(x)
 *     SUM(...)、MIN(...)、MAX(...)、AVERAGE(...)
 *     ROUND(num, digits)、CONCAT(...)、LEN(s)
 *
 * 安全：完全自实现解析器，不使用 eval / new Function。
 *
 * 示例：
 *   =A2+B2
 *   =IF(C2>100, "大", "小")
 *   =ROUND(A2*B2*0.95, 2)
 */

export interface FormulaContext {
  /**
   * 给定 'A' / 'B' / 'AB' 等列字母 + 行号（1-based），返回单元格值。
   * 返回 null/undefined/'' 视为空。
   */
  getCell(col: string, row: number): unknown
}

const MAX_DEPTH = 16

/** 判断一个值是否是公式字符串（= 开头） */
export function isFormula(v: unknown): v is string {
  return typeof v === 'string' && v.trim().startsWith('=')
}

/** 求值入口；任何异常都向上抛出 */
export function evalFormula(formula: string, ctx: FormulaContext): unknown {
  const src = formula.trim().replace(/^=/, '')
  if (!src) throw new Error('空公式')
  const tokens = tokenize(src)
  const parser = new Parser(tokens, ctx)
  const result = parser.parseExpression()
  if (parser.pos < tokens.length) {
    throw new Error(`末尾多余符号: ${tokens[parser.pos]?.value}`)
  }
  return result
}

/** 列字母 → 0-based 列索引（A=0、Z=25、AA=26） */
export function colLetterToIndex(letter: string): number {
  let n = 0
  for (const c of letter.toUpperCase()) {
    n = n * 26 + (c.charCodeAt(0) - 64)
  }
  return n - 1
}

// =============== Tokenizer ===============
type TokenType = 'num' | 'str' | 'ident' | 'op' | 'lparen' | 'rparen' | 'comma'
interface Token { type: TokenType; value: string }

function tokenize(s: string): Token[] {
  const tokens: Token[] = []
  let i = 0
  while (i < s.length) {
    const c = s[i]
    if (/\s/.test(c)) { i++; continue }
    if (c === '(') { tokens.push({ type: 'lparen', value: c }); i++; continue }
    if (c === ')') { tokens.push({ type: 'rparen', value: c }); i++; continue }
    if (c === ',' || c === '，') { tokens.push({ type: 'comma', value: ',' }); i++; continue }
    if (c === '"' || c === "'") {
      const quote = c
      let j = i + 1
      let str = ''
      while (j < s.length && s[j] !== quote) {
        if (s[j] === '\\' && j + 1 < s.length) { str += s[j + 1]; j += 2 }
        else { str += s[j]; j++ }
      }
      if (j >= s.length) throw new Error('字符串未闭合')
      tokens.push({ type: 'str', value: str })
      i = j + 1
      continue
    }
    if (/[0-9.]/.test(c)) {
      let j = i + 1
      while (j < s.length && /[0-9.]/.test(s[j])) j++
      tokens.push({ type: 'num', value: s.substring(i, j) })
      i = j
      continue
    }
    if (/[A-Za-z_一-龥]/.test(c)) {
      let j = i + 1
      while (j < s.length && /[A-Za-z_0-9一-龥]/.test(s[j])) j++
      tokens.push({ type: 'ident', value: s.substring(i, j) })
      i = j
      continue
    }
    // 多字符操作符 <= >= == != <>
    if ('<>!='.includes(c)) {
      if (i + 1 < s.length && '=>'.includes(s[i + 1])) {
        tokens.push({ type: 'op', value: s.substring(i, i + 2) })
        i += 2
        continue
      }
      tokens.push({ type: 'op', value: c })
      i++
      continue
    }
    if ('+-*/%'.includes(c)) {
      tokens.push({ type: 'op', value: c })
      i++
      continue
    }
    if (c === '&') {
      // & 字符串拼接（Excel）
      tokens.push({ type: 'op', value: '&' })
      i++
      continue
    }
    throw new Error(`非法字符: ${c}`)
  }
  return tokens
}

// =============== Parser ===============
class Parser {
  pos = 0
  constructor(public tokens: Token[], public ctx: FormulaContext) {}

  peek(): Token | undefined { return this.tokens[this.pos] }
  consume(): Token { return this.tokens[this.pos++] }
  expect(type: TokenType, value?: string): Token {
    const t = this.consume()
    if (!t || t.type !== type || (value !== undefined && t.value !== value)) {
      throw new Error(`期望 ${value || type}，得到 ${t?.value || 'EOF'}`)
    }
    return t
  }
  isOp(...vals: string[]): boolean {
    const p = this.peek()
    return !!p && p.type === 'op' && vals.includes(p.value)
  }

  parseExpression(): unknown {
    return this.parseComparison()
  }

  parseComparison(): unknown {
    let left = this.parseConcat()
    while (this.isOp('=', '==', '<>', '!=', '>', '>=', '<', '<=')) {
      const op = this.consume().value
      const right = this.parseConcat()
      left = compare(op, left, right)
    }
    return left
  }

  parseConcat(): unknown {
    let left = this.parseAddSub()
    while (this.isOp('&')) {
      this.consume()
      const right = this.parseAddSub()
      left = String(coerceDisplay(left)) + String(coerceDisplay(right))
    }
    return left
  }

  parseAddSub(): unknown {
    let left = this.parseMulDiv()
    while (this.isOp('+', '-')) {
      const op = this.consume().value
      const right = this.parseMulDiv()
      left = op === '+' ? num(left) + num(right) : num(left) - num(right)
    }
    return left
  }

  parseMulDiv(): unknown {
    let left = this.parseUnary()
    while (this.isOp('*', '/', '%')) {
      const op = this.consume().value
      const right = this.parseUnary()
      const a = num(left), b = num(right)
      if (op === '*') left = a * b
      else if (op === '/') {
        if (b === 0) throw new Error('除以 0')
        left = a / b
      } else left = a % b
    }
    return left
  }

  parseUnary(): unknown {
    if (this.isOp('-', '+')) {
      const op = this.consume().value
      const v = num(this.parseUnary())
      return op === '-' ? -v : v
    }
    return this.parseAtom()
  }

  parseAtom(): unknown {
    const t = this.consume()
    if (!t) throw new Error('表达式不完整')
    if (t.type === 'num') return parseFloat(t.value)
    if (t.type === 'str') return t.value
    if (t.type === 'lparen') {
      const v = this.parseExpression()
      this.expect('rparen')
      return v
    }
    if (t.type === 'ident') {
      // 函数调用
      if (this.peek()?.type === 'lparen') {
        this.consume()
        const args: unknown[] = []
        if (this.peek()?.type !== 'rparen') {
          args.push(this.parseExpression())
          while (this.peek()?.type === 'comma') {
            this.consume()
            args.push(this.parseExpression())
          }
        }
        this.expect('rparen')
        return callFunction(t.value, args)
      }
      // A1 / AB12 字段引用
      const m = /^([A-Za-z]+)(\d+)$/.exec(t.value)
      if (m) return this.ctx.getCell(m[1].toUpperCase(), parseInt(m[2], 10))
      const up = t.value.toUpperCase()
      if (up === 'TRUE') return true
      if (up === 'FALSE') return false
      if (up === 'NULL' || up === 'EMPTY') return null
      throw new Error(`未知标识符: ${t.value}`)
    }
    throw new Error(`无法解析: ${t.value}`)
  }
}

// =============== 工具 / 函数 ===============
function num(v: unknown): number {
  if (typeof v === 'number') return isFinite(v) ? v : 0
  if (typeof v === 'boolean') return v ? 1 : 0
  if (v == null || v === '') return 0
  const n = Number(v)
  if (isNaN(n)) throw new Error(`不是数字: "${v}"`)
  return n
}

function isTruthy(v: unknown): boolean {
  if (typeof v === 'boolean') return v
  if (typeof v === 'number') return v !== 0
  if (v == null || v === '') return false
  return true
}

function coerceDisplay(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'number') {
    return Number.isInteger(v) ? String(v) : String(parseFloat(v.toFixed(10)))
  }
  if (typeof v === 'boolean') return v ? 'TRUE' : 'FALSE'
  return String(v)
}

function compare(op: string, a: unknown, b: unknown): boolean {
  const eq = (x: unknown, y: unknown) => {
    if (typeof x === 'number' || typeof y === 'number') {
      return num(x) === num(y)
    }
    return String(x ?? '') === String(y ?? '')
  }
  if (op === '=' || op === '==') return eq(a, b)
  if (op === '<>' || op === '!=') return !eq(a, b)
  // 大小比较：数值优先
  let na: number, nb: number
  try { na = num(a); nb = num(b) }
  catch {
    const sa = String(a ?? ''), sb = String(b ?? '')
    switch (op) {
      case '>': return sa > sb
      case '>=': return sa >= sb
      case '<': return sa < sb
      case '<=': return sa <= sb
    }
    return false
  }
  switch (op) {
    case '>': return na > nb
    case '>=': return na >= nb
    case '<': return na < nb
    case '<=': return na <= nb
  }
  return false
}

function callFunction(name: string, args: unknown[]): unknown {
  const upper = name.toUpperCase()
  switch (upper) {
    case 'IF':
      if (args.length !== 3) throw new Error('IF 需要 3 个参数')
      return isTruthy(args[0]) ? args[1] : args[2]
    case 'AND':
      if (args.length === 0) return true
      return args.every(isTruthy)
    case 'OR':
      if (args.length === 0) return false
      return args.some(isTruthy)
    case 'NOT':
      if (args.length !== 1) throw new Error('NOT 需要 1 个参数')
      return !isTruthy(args[0])
    case 'SUM':
      return args.reduce<number>((s, v) => s + num(v), 0)
    case 'MIN':
      if (args.length === 0) return 0
      return Math.min(...args.map(num))
    case 'MAX':
      if (args.length === 0) return 0
      return Math.max(...args.map(num))
    case 'AVERAGE':
    case 'AVG':
      if (args.length === 0) return 0
      return args.reduce<number>((s, v) => s + num(v), 0) / args.length
    case 'ROUND': {
      const v = num(args[0])
      const d = args.length > 1 ? num(args[1]) : 0
      const p = Math.pow(10, d)
      return Math.round(v * p) / p
    }
    case 'ABS':
      return Math.abs(num(args[0]))
    case 'CONCAT':
    case 'CONCATENATE':
      return args.map(coerceDisplay).join('')
    case 'LEN':
      if (args.length !== 1) throw new Error('LEN 需要 1 个参数')
      return coerceDisplay(args[0]).length
    case 'LEFT': {
      const s = coerceDisplay(args[0])
      const n = args.length > 1 ? num(args[1]) : 1
      return s.substring(0, Math.max(0, Math.floor(n)))
    }
    case 'RIGHT': {
      const s = coerceDisplay(args[0])
      const n = args.length > 1 ? num(args[1]) : 1
      return s.substring(s.length - Math.max(0, Math.floor(n)))
    }
    default:
      throw new Error(`不支持的函数: ${name}`)
  }
}

// =============== 带循环检测的"安全求值"封装 ===============
/**
 * 用于在显示层调用：包装好上下文 + 循环引用保护 + 错误兜底。
 * lookupCell 返回的可能仍然是公式（字符串 `=...`），会被自动递归解析。
 */
export function evalCellFormula(
  formula: string,
  lookupCell: (col: string, row: number) => unknown,
): { ok: true; value: unknown } | { ok: false; error: string } {
  const stack: string[] = []  // 引用栈，键 = `${col}${row}`
  let depth = 0

  const ctx: FormulaContext = {
    getCell: (col: string, row: number): unknown => {
      const key = `${col}${row}`
      if (stack.includes(key)) {
        throw new Error(`循环引用: ${key}`)
      }
      depth++
      if (depth > MAX_DEPTH) throw new Error('公式嵌套太深')
      const v = lookupCell(col, row)
      try {
        if (isFormula(v)) {
          stack.push(key)
          try { return evalFormula(v as string, ctx) }
          finally { stack.pop() }
        }
        return v
      } finally {
        depth--
      }
    },
  }

  try {
    return { ok: true, value: evalFormula(formula, ctx) }
  } catch (e: any) {
    return { ok: false, error: e?.message || '公式错误' }
  }
}
