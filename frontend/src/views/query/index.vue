<template>
  <section class="page" data-module="query">
    <header class="page-head">
      <div>
        <h2>交互查询管理</h2>
        <p class="page-desc">维护交互查询语句的输入、执行约束与结果记录；执行时跨模块携带表达式、参数与数据集版本，走带身份与期限的结果缓存。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记查询语句</button>
        <button class="btn" type="button" @click="exportRows">导出交互查询清单</button>
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
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openExecute(row)">执行查询（走缓存）</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无交互查询数据，可先登记查询语句</td>
        </tr>
      </tbody>
    </table>

    <div v-if="execTarget" class="cache-panel" role="dialog" aria-label="执行查询并查看缓存判定">
      <header class="cache-panel-head">
        <strong>执行 {{ execTarget['语句编号'] }} —— {{ execTarget['查询表达式'] }}</strong>
        <button class="link" type="button" @click="execTarget = null">关闭</button>
      </header>
      <p class="page-desc">参数参与缓存完整身份；不同参数、不同数据集版本都不会串用结果。</p>
      <label class="filter-item">
        <span>查询参数（JSON）</span>
        <textarea v-model="paramText" rows="3" spellcheck="false"></textarea>
      </label>
      <label class="filter-item cache-check">
        <input v-model="failFill" type="checkbox" />
        <span>模拟缓存填充失败（验证绕过缓存但查询仍成功）</span>
      </label>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="executing" @click="executeWithCache">
          {{ executing ? '执行中…' : '执行' }}
        </button>
      </div>

      <div v-if="lastCache" class="cache-result">
        <h4>缓存判定</h4>
        <table class="data-table">
          <tbody>
            <tr><th>命中</th><td>{{ lastCache.hit ? '是（复用历史结果）' : '否' }}</td></tr>
            <tr><th>是否绕过缓存</th><td>{{ lastCache.bypassed ? '是' : '否' }}</td></tr>
            <tr><th>缓存状态</th><td>{{ lastCache.status ?? '—' }}</td></tr>
            <tr><th>缓存键（候选匹配用）</th><td class="mono">{{ lastCache.cache_key ?? '—' }}</td></tr>
            <tr><th>数据集版本</th><td class="mono">{{ lastCache.dataset_version ?? '—' }}</td></tr>
            <tr v-if="lastCache.entry">
              <th>原始到期时点</th>
              <td>{{ lastCache.entry.expires_at_label }}（重启不延寿）</td>
            </tr>
            <tr v-if="lastCache.entry">
              <th>累计命中</th><td>{{ lastCache.entry.hit_count ?? 0 }}</td>
            </tr>
            <tr><th>判定说明</th><td>{{ lastCache.reason }}</td></tr>
          </tbody>
        </table>
        <h4>查询结果</h4>
        <pre class="result-pre">{{ JSON.stringify(lastCache.result, null, 2) }}</pre>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条交互查询记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type CacheDetail = {
  hit: boolean
  bypassed: boolean
  status?: string
  reason: string
  cache_key?: string
  dataset_version?: string
  entry?: Record<string, unknown> | null
  result?: Record<string, unknown> | null
}

const ENDPOINT = '/api/query'
const columns = ["语句编号", "查询表达式", "目标数据集", "返回字段", "排序条件", "分页大小", "最大行数", "超时秒数"]
const actions = ["提交执行", "启动查询", "终止查询", "保存结果"]
const statuses = ["草稿", "待执行", "执行中", "已完成", "已超时"]
const stats = [{"label": "查询语句总数", "value": 0}, {"label": "已完成查询", "value": 0}, {"label": "执行中查询", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const execTarget = ref<Row | null>(null)
const paramText = ref('{}')
const failFill = ref(false)
const executing = ref(false)
const lastCache = ref<CacheDetail | null>(null)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '查询语句登记入口尚未接入审批流'
}

function openExecute(row: Row) {
  execTarget.value = row
  paramText.value = '{}'
  failFill.value = false
  lastCache.value = null
  errorMessage.value = ''
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('交互查询动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交互查询操作失败'
  }
}

async function executeWithCache() {
  if (!execTarget.value) return
  errorMessage.value = ''
  let parameters: Record<string, unknown> = {}
  try {
    parameters = JSON.parse(paramText.value || '{}') as Record<string, unknown>
  } catch {
    errorMessage.value = '查询参数不是合法 JSON，请检查后再执行'
    return
  }
  executing.value = true
  try {
    const response = await request(`${ENDPOINT}/${execTarget.value.id}/execute`, {
      method: 'POST',
      body: JSON.stringify({ parameters, fail_fill: failFill.value }),
    })
    if (!response.ok) {
      throw new Error('查询执行请求失败')
    }
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message ?? '查询执行失败')
    }
    lastCache.value = payload.entry.cache as CacheDetail
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询执行失败'
  } finally {
    executing.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('查询语句列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交互查询列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.cache-panel {
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.cache-panel-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.cache-check { display: flex; align-items: center; gap: 6px; margin: 8px 0; }
textarea { width: 100%; font-family: ui-monospace, monospace; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
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
.cache-result h4 { margin: 12px 0 6px; font-size: 14px; }
</style>
