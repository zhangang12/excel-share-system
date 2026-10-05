import { computed } from 'vue'
import { useAuthStore } from '../stores/auth'

/**
 * 采购员按清单分工的列可见性（采购部项目一览、采购管理下单共用）。
 *
 * 🆕 2026-10-05：分工改由后端 dept_config.BUYER_SHEET_MAP 下发（auth.user.buyer_sheets），
 * 不再按用户名写死在前端。起因：方步森的外协资料交接给李昌奇，老写法要改两个页面再发客户端；
 * 现在换人只改后端配置，不用发客户端。
 *   buyer_sheets 为空 = 不按清单分工（其他采购员、采购主管、管理层、财务）→ 看全部。
 *   清单域：standard 标准件清单 / elec_po 电工采购单 / material 不锈钢原料下料单 / laser 激光件 / outsource 外协加工
 */
export function useBuyerSheets() {
  const auth = useAuthStore()
  const sheets = computed(() => auth.user?.buyer_sheets ?? null)
  const seeAll = computed(() => !sheets.value)
  const has = (k: string) => seeAll.value || !!sheets.value?.includes(k)
  return {
    showDesigner:      computed(() => has('material') || has('laser') || has('outsource')),
    showOutsource:     computed(() => has('outsource')),   // 外协加工表
    showSheetmetal:    computed(() => has('outsource')),   // 钣金装配表（外协采购员看）
    showMaterial:      computed(() => has('material')),    // 不锈钢原料下料单
    showLaser:         computed(() => has('laser')),       // 激光件清单
    showCadLaser:      computed(() => has('laser')),       // CAD激光图纸
    showElecPo:        computed(() => has('elec_po')),     // 电工采购单
    showStandardSheet: computed(() => has('standard')),    // 标准件清单
    showOutImg:        computed(() => has('standard')),    // 外购附图
  }
}
