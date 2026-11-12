<template>
  <section class="page" data-module="cache">
    <header class="page-head">
      <div>
        <h2>查询缓存管理</h2>
        <p class="page-desc">
          维护缓存策略与带身份、期限的结果留存：缓存键仅作候选匹配，命中前核对完整身份；
          每条结果展示为何仍有效与原始到期时点，重启不延寿，过期结果不会被迟到填充复活。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记缓存策略</button>
        <button class="btn" type="button" @click="exportRows">导出策略清单</button>
      </div>
    </header>

    <h3 class="section-title">缓存策略</h3>
    <form class="filter-bar" @submit.prevent="reloadPolicies">
      <label v-for="field in policyFilterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in policyColumns" :key="column">{{ column }}</th>
          <th>期限确认</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in policies" :key="String(row.id)">
          <td v-for="column in policyColumns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="row.ttl_confirmed ? 'tag ok' : 'tag warn'">
              {{ row.ttl_confirmed ? '期限有效' : '待确认（非永不过期）' }}
            </span>
          </td>
          <td class="row-actions">
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
        <tr v-if="!policies.length">
          <td :colspan="policyColumns.length + 2" class="empty-state">暂无缓存策略数据</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">结果留存（带身份与绝对期限）</h3>
    <div class="stat-row">
      <article class="stat-card"><span class="stat-label">留存总数</span><strong class="stat-value">{{ resultStats.total ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">有效</span><strong class="stat-value">{{ resultStats.active ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">已过期</span><strong class="stat-value">{{ resultStats.expired ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">已淘汰</span><strong class="stat-value">{{ resultStats.evicted ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">累计命中</span><strong class="stat-value">{{ resultStats.hits ?? 0 }}</strong></article>
    </div>

    <form class="filter-bar" @submit.prevent="reloadResults">
      <label class="filter-item">
        <span>状态</span>
        <select v-model="resultStatus">
          <option value="">全部</option>
          <option v-for="item in resultStatuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>缓存键/来源/身份片段</span>
        <input v-model="resultKeyword" placeholder="按缓存键、来源或身份检索" />
      </label>
      <button class="btn" type="submit">查询留存</button>
      <button class="btn ghost" type="button" @click="resetResultFilters">重置</button>
      <button class="btn" type="button" @click="reloadResults">刷新</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in resultColumns" :key="column">{{ column }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in results" :key="String(row.id)">
          <td>{{ row.id }}</td>
          <td class="mono">{{ shortKey(row.cache_key) }}</td>
          <td>{{ row.source ?? '—' }}</td>
          <td><span :class="statusTagClass(row.status)">{{ row.status }}</span></td>
          <td>{{ row.stored_at_label ?? '—' }}</td>
          <td>{{ row.expires_at_label ?? '—' }}</td>
          <td>{{ row.hit_count ?? 0 }}</td>
          <td class="reason-cell">{{ row.validity_reason ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="viewResult(row)">详情</button>
            <button
              v-if="row.status === '有效'"
              class="link danger"
              type="button"
              @click="evict(row)"
            >
              淘汰
            </button>
          </td>
        </tr>
        <tr v-if="!results.length">
          <td :colspan="resultColumns.length + 1" class="empty-state">暂无缓存结果留存，可先到交互查询页执行查询</td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="detail-panel" role="dialog" aria-label="缓存结果详情">
      <header class="cache-panel-head">
        <strong>留存 #{{ detail.id }} 详情</strong>
        <button class="link" type="button" @click="detail = null">关闭</button>
      </header>
      <table class="data-table">
        <tbody>
          <tr><th>状态</th><td>{{ detail.status }}</td></tr>
          <tr><th>完整身份</th><td class="mono reason-cell">{{ detail.identity }}</td></tr>
          <tr><th>缓存键</th><td class="mono">{{ detail.cache_key }}</td></tr>
          <tr><th>来源</th><td>{{ detail.source ?? '—' }}</td></tr>
          <tr><th>存入时间</th><td>{{ detail.stored_at_label ?? '—' }}</td></tr>
          <tr><th>原始到期时点</th><td>{{ detail.expires_at_label ?? '—' }}（重启不延寿）</td></tr>
          <tr v-if="detail.evicted_at_label"><th>淘汰时间</th><td>{{ detail.evicted_at_label }}</td></tr>
          <tr><th>有效性解释</th><td class="reason-cell">{{ detail.validity_reason }}</td></tr>
          <tr><th>缓存结果</th><td><pre class="result-pre">{{ JSON.stringify(detail.result, null, 2) }}</pre></td></tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span>共 {{ policyTotal }} 条策略、{{ resultTotal }} 条结果留存</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type ResultRow = Record<string, unknown>
type Stats = Record<string, number>

const ENDPOINT = '/api/cache'
const policyColumns = ["策略编号", "目标查询", "缓存键规则", "过期秒数", "刷新触发", "命中率阈值", "最大条目数", "淘汰算法"]
const policyFilterFields = policyColumns.slice(0, 3)
const actions = ["提交校验", "启用缓存", "停用缓存"]
const resultColumns = ["ID", "缓存键", "来源", "状态", "存入时间", "原始到期时点", "命中次数", "为何仍有效/失效"]
const resultStatuses = ["有效", "已过期", "已淘汰"]

const policies = ref<Row[]>([])
const policyTotal = ref(0)
const results = ref<ResultRow[]>([])
const resultTotal = ref(0)
const resultStats = ref<Stats>({})
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const resultStatus = ref('')
const resultKeyword = ref('')
const detail = ref<ResultRow | null>(null)

function shortKey(key: unknown): string {
  const text = String(key ?? '')
  return text.length > 18 ? `${text.slice(0, 18)}…` : text
}

function statusTagClass(status: unknown): string {
  if (status === '有效') return 'tag ok'
  if (status === '已过期') return 'tag warn'
  return 'tag muted'
}

function resetFilters() {
  filters.value = {}
  void reloadPolicies()
}

function resetResultFilters() {
  resultStatus.value = ''
  resultKeyword.value = ''
  void reloadResults()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '缓存策略登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('查询缓存动作未生效，请稍后重试')
    }
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '动作被拒绝'
    }
    await Promise.all([reloadPolicies(), reloadResults(), reloadStats()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询缓存操作失败'
  }
}

async function evict(row: ResultRow) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/results/${row.id}/evict`, { method: 'POST' })
    if (!response.ok) {
      throw new Error('淘汰操作未生效')
    }
    await Promise.all([reloadResults(), reloadStats()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '淘汰失败'
  }
}

async function viewResult(row: ResultRow) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/results/${row.id}`)
    if (!response.ok) {
      throw new Error('留存详情读取失败')
    }
    detail.value = (await response.json()) as ResultRow
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '留存详情读取失败'
  }
}

async function reloadPolicies() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('缓存策略列表读取失败')
    }
    const payload = await response.json()
    policies.value = payload.items ?? []
    policyTotal.value = payload.total ?? policies.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '缓存策略列表读取失败'
  }
}

async function reloadResults() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (resultStatus.value) params.set('status', resultStatus.value)
  if (resultKeyword.value) params.set('keyword', resultKeyword.value)
  try {
    const response = await request(`${ENDPOINT}/results?${params.toString()}`)
    if (!response.ok) {
      throw new Error('缓存结果留存读取失败')
    }
    const payload = await response.json()
    results.value = payload.items ?? []
    resultTotal.value = payload.total ?? results.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '缓存结果留存读取失败'
  }
}

async function reloadStats() {
  try {
    const response = await request(`${ENDPOINT}/results/stats`)
    if (response.ok) {
      resultStats.value = (await response.json()) as Stats
    }
  } catch {
    // 统计失败不阻塞主表格
  }
}

onMounted(() => {
  void reloadPolicies()
  void reloadResults()
  void reloadStats()
})
</script>

<style scoped>
.section-title { margin: 20px 0 8px; font-size: 15px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.tag.ok { background: #e7f6ec; color: #1a7f37; }
.tag.warn { background: #fff4e0; color: #b25e09; }
.tag.muted { background: #eef1f5; color: #667085; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
.reason-cell { max-width: 320px; line-height: 1.5; }
.link.danger { color: #b42318; }
.detail-panel,
.cache-panel {
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.cache-panel-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.result-pre {
  margin: 0;
  max-height: 260px;
  overflow: auto;
  background: #0f172a;
  color: #e2e8f0;
  padding: 10px;
  border-radius: 6px;
  font-size: 12px;
}
.cache-check { display: flex; align-items: center; gap: 6px; margin: 8px 0; }
textarea { width: 100%; font-family: ui-monospace, monospace; }
</style>
