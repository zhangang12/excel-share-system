<script setup lang="ts">
/**
 * 🆕 反馈#385：「全流程进度这个功能，同步给所有部门带项目编号的功能上」
 *
 * 原来只有销售台账有这个入口。别的部门看到一个项目编号，想知道它卡在哪一环
 * （设计出图没有？采购到货没有？仓库备齐没有？），只能挨个 tab 翻。
 *
 * 这里把「按钮 + 弹窗 + 拉数据」整个收进一个组件，各部门页面一行就能挂上：
 *     <ProjectFlowButton :project-id="row.project_id" :code="row.code" />
 * 接口 GET /projects/{pid}/workflow 本来就是 get_current_user（全员可读），不用改后端。
 *
 * ⚠️ 数据是**点开才拉**的：这个组件会出现在成百上千行的表格里，
 *    挂载即请求会让每个列表页多打几百个请求。
 */
import { ref } from 'vue'
import { collabApi, type Workflow } from '@/api/collab'
import WorkflowGraph from './WorkflowGraph.vue'

const props = withDefaults(defineProps<{
  projectId?: number | null
  code?: string | null
  /** link=纯文字链接（放在表格操作列）；button=描边小按钮 */
  variant?: 'link' | 'button'
  label?: string
}>(), { variant: 'link', label: '流程' })

const visible = ref(false)
const loading = ref(false)
const data = ref<Workflow | null>(null)

async function open() {
  if (!props.projectId) return
  data.value = null
  visible.value = true
  loading.value = true
  try { data.value = await collabApi.workflow(props.projectId) }
  finally { loading.value = false }
}
</script>

<template>
  <el-button v-if="projectId" :link="variant === 'link'" :type="variant === 'link' ? 'primary' : undefined"
             size="small" @click.stop="open">{{ label }}</el-button>
  <el-dialog v-model="visible" :title="`🔗 全流程进度 · ${code || ''}`" width="92%" top="5vh" append-to-body>
    <div v-loading="loading" style="min-height:200px">
      <WorkflowGraph v-if="data" :wf="data" />
    </div>
  </el-dialog>
</template>
