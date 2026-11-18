<template>
  <section class="page" data-module="branch-audits">
    <header class="page-head">
      <div>
        <h2>合并审计清单</h2>
        <p class="page-desc">
          同一合并批次的所有冲突处置结果归在一起；重入不会追加重复记录，目标版本带唯一裁决和完整字段映射。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/branch">返回分支列表</RouterLink>
        <button class="btn" type="button" @click="exportAudits">导出合并审计清单</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>分支编号 / id</span>
        <input v-model="filters.branch_id" placeholder="4" />
      </label>
      <label class="filter-item">
        <span>合并批次号</span>
        <input v-model="filters.batch_no" placeholder="MERGE-20261118-01" />
      </label>
      <label class="filter-item">
        <span>操作小组</span>
        <input v-model="filters.team" placeholder="数据平台组" />
      </label>
      <label class="filter-item">
        <span>历史版本</span>
        <select v-model="filters.include_history">
          <option value="false">只看有效版本</option>
          <option value="true">带出已替代版本</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <article v-for="batch in batches" :key="batch.batch_no" class="batch-card" :data-batch="batch.batch_no">
      <header class="batch-head">
        <div>
          <h3>{{ batch.batch_no }} · {{ batch.table_name }}</h3>
          <p class="page-desc">
            {{ batch.source_branch }} → {{ batch.target_branch }} ·
            归属 {{ batch.owner_team }} · {{ batch.status }} ·
            {{ batch.read_only ? '受控版本（只读）' : '可操作版本' }}
          </p>
        </div>
        <div class="batch-version">
          <span class="stat-label">唯一目标版本</span>
          <strong>{{ batch.effective_version.version }}</strong>
          <span class="tag" :class="batch.effective_version.mapping_complete ? 'ok' : 'bad'">
            字段映射{{ batch.effective_version.mapping_complete ? '完整' : '不完整' }}
          </span>
          <span class="tag">{{ batch.effective_version.verdict ?? '无裁决' }}</span>
          <span class="tag">{{ batch.version_status }}</span>
        </div>
      </header>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">已完成冲突段</span>
          <strong class="stat-value">{{ batch.completed_segments.join('、') || '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待裁冲突段</span>
          <strong class="stat-value">{{ batch.pending_segments.join('、') || '无' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">版本依据</span>
          <strong class="stat-value small">{{ batch.version_basis ?? '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">校验结果</span>
          <strong class="stat-value small" :class="batch.validation.ok ? '' : 'error-text'">
            {{ batch.validation.ok ? '通过' : batch.validation.errors.join('；') }}
          </strong>
        </article>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>冲突段</th>
            <th>字段</th>
            <th>状态</th>
            <th>裁决</th>
            <th>源字段</th>
            <th>目标字段</th>
            <th>目标版本</th>
            <th>校验</th>
            <th>消费方</th>
            <th>版本依据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in batch.items" :key="item.segment">
            <td>{{ item.segment }}</td>
            <td>{{ item.field_name ?? '—' }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.verdict ?? '待裁决' }}</td>
            <td>{{ item.field_mapping?.source_field || '—' }}</td>
            <td>{{ item.field_mapping?.target_field || '—' }}</td>
            <td>{{ item.target_version ?? '—' }}</td>
            <td>{{ item.validation }}</td>
            <td>{{ (item.consumers ?? []).join('、') || '—' }}</td>
            <td>{{ item.version_basis ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <p v-if="batch.read_only" class="notice">
        {{ batch.permissions.message }}
      </p>

      <details v-if="(batch.version_history ?? []).length" class="history">
        <summary>版本历史（只有一条有效）</summary>
        <table class="data-table">
          <thead><tr><th>版本</th><th>状态</th><th>有效</th><th>裁决</th><th>版本依据</th><th>发布时间</th></tr></thead>
          <tbody>
            <tr v-for="version in batch.version_history" :key="version.version">
              <td>{{ version.version }}</td>
              <td>{{ version.status }}</td>
              <td>{{ version.effective ? '是' : '否' }}</td>
              <td>{{ version.verdict }}</td>
              <td>{{ version.version_basis }}</td>
              <td>{{ version.published_at }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <form class="filter-bar consume-bar" @submit.prevent="consume(batch)">
        <label class="filter-item">
          <span>消费模块</span>
          <input v-model="consumers[batch.batch_no]" placeholder="表快照管理" />
        </label>
        <button class="btn" type="submit" :disabled="batch.read_only">同步版本依据到合并审计清单</button>
        <span v-if="consumerMessage[batch.batch_no]" class="action-text">{{ consumerMessage[batch.batch_no] }}</span>
      </form>
    </article>

    <p v-if="!batches.length && !errorMessage" class="empty-state">暂无合并审计清单数据</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type AuditItem = {
  segment: string
  field_name?: string | null
  status: string
  verdict?: string | null
  field_mapping?: { source_field?: string | null; target_field?: string | null }
  target_version?: string | null
  validation: string
  consumers?: string[]
  version_basis?: string | null
}
type Batch = {
  batch_no: string
  table_name: string
  source_branch?: string
  target_branch?: string
  owner_team?: string
  status: string
  version_status?: string
  version_basis?: string | null
  completed_segments: string[]
  pending_segments: string[]
  items: AuditItem[]
  validation: { ok: boolean; errors: string[] }
  effective_version: { version?: string | null; verdict?: string | null; mapping_complete: boolean }
  permissions: { read_only: boolean; message?: string }
  read_only: boolean
  version_history?: { version: string; status: string; effective: boolean; verdict: string; version_basis: string; published_at: string }[]
}

const filters = reactive({ branch_id: '', batch_no: '', team: '数据平台组', include_history: 'false' })
const batches = ref<Batch[]>([])
const errorMessage = ref('')
const consumers = reactive<Record<string, string>>({})
const consumerMessage = reactive<Record<string, string>>({})

function resetFilters() {
  filters.branch_id = ''
  filters.batch_no = ''
  filters.team = '数据平台组'
  filters.include_history = 'false'
  void reload()
}

function exportAudits() {
  window.open('/api/branch/merge/audits?include_history=true', '_blank')
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.branch_id.trim()) query.set('branch_id', filters.branch_id.trim())
  if (filters.batch_no.trim()) query.set('batch_no', filters.batch_no.trim())
  if (filters.team.trim()) query.set('team', filters.team.trim())
  query.set('include_history', filters.include_history)
  try {
    const response = await request(`/api/branch/merge/audits?${query.toString()}`)
    if (response.status === 404) {
      batches.value = []
      errorMessage.value = '没有匹配的合并批次或合并审计清单'
      return
    }
    if (!response.ok) {
      throw new Error('合并审计清单读取失败')
    }
    const payload = await response.json()
    batches.value = payload.batches ?? []
    for (const batch of batches.value) {
      consumers[batch.batch_no] ??= '表快照管理'
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '合并审计清单读取失败'
  }
}

async function consume(batch: Batch) {
  consumerMessage[batch.batch_no] = ''
  try {
    const response = await request(`/api/branch/merge/audits/${batch.batch_no}/consume`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          module: consumers[batch.batch_no],
          team: filters.team,
        },
      }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message ?? '版本依据同步失败')
    }
    consumerMessage[batch.batch_no] = payload.message
    await reload()
  } catch (error) {
    consumerMessage[batch.batch_no] = error instanceof Error ? error.message : '版本依据同步失败'
  }
}

onMounted(() => {
  const query = new URLSearchParams(window.location.search)
  filters.branch_id = query.get('branch_id') ?? ''
  filters.batch_no = query.get('batch_no') ?? ''
  void reload()
})
</script>

<style scoped>
.batch-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 16px; }
.batch-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.batch-head h3 { margin: 0; }
.batch-version { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.tag { border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 12px; color: var(--muted); }
.tag.ok { border-color: #2f9e44; color: #2f9e44; }
.tag.bad { border-color: #b42318; color: #b42318; }
.stat-value.small { font-size: 13px; font-weight: 500; }
.notice { background: #fff8e6; border: 1px solid #f3d27a; border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.history { margin: 10px 0; }
.consume-bar { margin-top: 10px; }
.action-text { font-size: 12px; color: #1f6feb; }
</style>
