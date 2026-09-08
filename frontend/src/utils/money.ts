/** 金额输入框的解析器（金额审计 2026-09-09）。
 *
 * 为什么要有：Element Plus 的 el-input-number 内部用 `Number.parseFloat(value)` 解析输入串，
 * 从 Excel / 网银流水复制 `12,500.00` 粘进去，parseFloat 在逗号处截断 → 模型值变成 **12**，
 * 失焦后框里显示 12.00，人很容易没看见就提交了——付款金额、请款分配、工资八列都中招。
 * 用法：`<el-input-number :parser="moneyParser" … />`（EP ≥ 2.4 支持 formatter/parser）。
 *
 * 只保留数字、小数点和负号；全角逗号/空格/货币符一律去掉。formatter 不做（保持原生显示，不改用户习惯）。
 */
export function moneyParser(v: string): string {
  if (v == null) return ''
  return String(v).replace(/[^\d.-]/g, '')
}
