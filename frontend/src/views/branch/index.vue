<template>
  <section class="page" data-module="branch">
    <header class="page-head">
      <div>
        <h2>数据分支合并管理</h2>
        <p class="page-desc">维护数据分支的源目标配置、冲突处理与合并追踪，重入时只续做未完成冲突段，最终目标版本带唯一裁决与完整字段映射。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记分支记录</button>
        <RouterLink class="btn" :to="{ path: '/branch/audits', query: selectedBranchId ? { branch_id: String(selectedBranchId) } : {} }">
          合并审计清单
        </RouterLink>
        <button class="btn" type="button" @click="exportRows">导出数据分支合并清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>操作小组</span>
        <input v-model="team" placeholder="数据平台组" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>合并进度</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['合并进度'] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openMerge(row)">冲突段续做</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据分支合并数据，可先登记分支记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据分支合并记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="state" class="panel" data-panel="merge-reentry">
      <header class="panel-head">
        <div>
          <h3>冲突段续做 · {{ state.batch.batch_no }}</h3>
          <p class="page-desc">
            源分支 {{ state.batch.source_branch }} → 目标分支 {{ state.batch.target_branch }} ·
            归属小组 {{ state.batch.owner_team }} ·
            {{ state.read_only ? '受控版本（只读）' : '可操作版本' }}
          </p>
        </div>
        <button class="btn ghost" type="button" @click="state = null">收起</button>
      </header>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">已完成冲突段</span>
          <strong class="stat-value">{{ state.completed_segments.join('、') || '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待裁冲突段</span>
          <strong class="stat-value">{{ state.pending_segments.join('、') || '无' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">下次续做起点</span>
          <strong class="stat-value">{{ state.next_segment ?? '已完成' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">唯一目标版本</span>
          <strong class="stat-value">{{ state.target_version ?? '—' }}</strong>
        </article>
      </div>

      <p v-if="state.target_moved" class="notice">
        目标分支已前进到 {{ state.current_target_head }}，重入会选择“基于当前目标重放未完成段”或“恢复运行起点后重排全部冲突段”，最终只保留一个有效合并版本。
      </p>

      <table class="data-table">
        <thead>
          <tr><th>冲突段</th><th>字段</th><th>状态</th><th>裁决</th><th>字段映射</th><th>应用版本</th></tr>
        </thead>
        <tbody>
          <tr v-for="segment in state.segments" :key="segment.code">
            <td>{{ segment.code }}</td>
            <td>{{ segment.field_name ?? '—' }}</td>
            <td>{{ segment.status }}</td>
            <td>{{ segment.verdict ?? '待裁决' }}</td>
            <td>{{ segment.field_mapping.source_field }} → {{ segment.field_mapping.target_field }}</td>
            <td>{{ segment.applied_version ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <form class="filter-bar" @submit.prevent="reenter">
        <label class="filter-item">
          <span>本次续做冲突段</span>
          <input v-model="reentrySegments" placeholder="留空表示全部未完成段，例如 C14,C15" />
        </label>
        <label class="filter-item">
          <span>唯一裁决</span>
          <select v-model="reentryVerdict">
            <option v-for="item in verdicts" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>重入方式</span>
          <select v-model="rebaseMode">
            <option value="current_target">基于当前目标重放未完成段</option>
            <option value="run_start">恢复运行起点后重排全部冲突段</option>
          </select>
        </label>
        <button class="btn primary" type="submit" :disabled="state.read_only">续做冲突段</button>
      </form>

      <form class="filter-bar" @submit.prevent="commitTarget">
        <label class="filter-item">
          <span>登记目标分支新提交</span>
          <input v-model="targetHead" placeholder="main-reporting@c4e9d77" />
        </label>
        <button class="btn" type="submit" :disabled="state.read_only">登记新提交</button>
      </form>

      <p v-if="actionMessage" class="action-text">{{ actionMessage }}</p>
      <p v-if="reentry" class="action-text">
        本次续做 {{ reentry.applied_segments.join('、') || '无' }}，
        跳过已完成段 {{ reentry.skipped_completed_segments.join('、') || '无' }}，
        发布版本 {{ reentry.published_version }}，
        校验 {{ reentry.validation.ok ? '通过' : '未通过：' + reentry.validation.errors.join('；') }}
      </p>
      <p v-if="state.validation && !state.validation.ok" class="error-text">
        {{ state.validation.errors.join('；') }}
      </p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Segment = {
  code: string
  field_name?: string | null
  status: string
  verdict?: string | null
  field_mapping: { source_field?: string | null; target_field?: string | null }
  applied_version?: string | null
}
type MergeState = {
  batch: {
    batch_no: string
    source_branch?: string
    target_branch?: string
    owner_team?: string
    status?: string
  }
  segments: Segment[]
  completed_segments: string[]
  pending_segments: string[]
  next_segment?: string | null
  target_version?: string | null
  target_moved: boolean
  current_target_head?: string | null
  run_start_version?: string | null
  permissions: { can_edit: boolean; read_only: boolean; message?: string }
  read_only: boolean
  validation?: { ok: boolean; errors: string[] }
}
type Reentry = {
  applied_segments: string[]
  skipped_completed_segments: string[]
  published_version?: string | null
  validation: { ok: boolean; errors: string[] }
}

const ENDPOINT = '/api/branch'
const columns = ["分支编号", "表名称", "分支类型", "源分支", "目标分支", "冲突处理", "合并时间", "分支状态"]
const actions = ["执行合并", "删除分支", "归档分支"]
const verdicts = ["源分支优先", "目标分支优先", "合并保留"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const actionMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const team = ref('数据平台组')

const selectedBranchId = ref<number | null>(null)
const state = ref<MergeState | null>(null)
const reentry = ref<Reentry | null>(null)
const reentrySegments = ref('')
const reentryVerdict = ref(verdicts[0])
const rebaseMode = ref<'current_target' | 'run_start'>('current_target')
const targetHead = ref('')

const stats = computed(() => [
  { label: '分支记录总数', value: total.value },
  { label: '合并中分支', value: rows.value.filter((row) => row['分支状态'] === '合并中').length },
  { label: '已合并分支', value: rows.value.filter((row) => row['分支状态'] === '已合并').length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '分支记录登记入口尚未接入审批流'
}

async function openMerge(row: Row) {
  errorMessage.value = ''
  actionMessage.value = ''
  reentry.value = null
  selectedBranchId.value = Number(row.id)
  reentrySegments.value = ''
  targetHead.value = ''
  await loadState()
}

async function loadState() {
  if (selectedBranchId.value === null) return
  const query = new URLSearchParams({ team: team.value }).toString()
  try {
    const response = await request(`${ENDPOINT}/${selectedBranchId.value}/merge?${query}`)
    if (response.status === 404) {
      state.value = null
      errorMessage.value = '该分支还没有可续做的合并批次'
      return
    }
    if (!response.ok) {
      throw new Error('合并批次读取失败')
    }
    state.value = (await response.json()) as MergeState
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '合并批次读取失败'
  }
}

async function reenter() {
  if (selectedBranchId.value === null) return
  actionMessage.value = ''
  reentry.value = null
  try {
    const response = await request(`${ENDPOINT}/${selectedBranchId.value}/merge/reenter`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          team: team.value,
          verdict: reentryVerdict.value,
          rebase_mode: rebaseMode.value,
          segments: reentrySegments.value,
        },
      }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message ?? '冲突段续做失败')
    }
    actionMessage.value = payload.message
    state.value = payload.entry as MergeState
    reentry.value = (payload.entry?.reentry ?? null) as Reentry | null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '冲突段续做失败'
  }
}

async function commitTarget() {
  if (selectedBranchId.value === null || !targetHead.value.trim()) return
  actionMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selectedBranchId.value}/target-commit`, {
      method: 'POST',
      body: JSON.stringify({ values: { team: team.value, head: targetHead.value.trim() } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message ?? '目标分支提交登记失败')
    }
    actionMessage.value = payload.message
    state.value = payload.entry as MergeState
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '目标分支提交登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据分支合并动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据分支合并操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分支记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据分支合并列表读取失败'
  }
}

onMounted(() => {
  void reload()
  const branchId = new URLSearchParams(window.location.search).get('branch_id')
  if (branchId) {
    void openMerge({ id: Number(branchId) })
  }
})
</script>

<style scoped>
.panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; }
.panel-head h3 { margin: 0; }
.notice { background: #fff8e6; border: 1px solid #f3d27a; border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.action-text { font-size: 13px; color: #1f6feb; }
</style>
