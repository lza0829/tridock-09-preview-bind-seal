<template>
  <div>
    <div class="hero">
      <div>
        <h1>总览</h1>
        <p>当前到货日三腿行数、对账批次与结案状态。结案后当日预报/实收/回执不可再改。</p>
      </div>
      <button @click="load">刷新</button>
    </div>
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">到货日</div><div class="v">{{ d.dock_day }}</div></div>
      <div class="stat"><div class="k">预报 / 实收 / 回执</div><div class="v">{{ d.expect_count }} / {{ d.receive_count }} / {{ d.confirm_count }}</div></div>
      <div class="stat"><div class="k">三腿合计</div><div class="v">{{ d.expect_total }} / {{ d.receive_total }} / {{ d.confirm_total }}</div></div>
      <div class="stat"><div class="k">入湖水位 / 已入批</div><div class="v">{{ d.current_rows }} / {{ d.scanned_rows }}</div></div>
      <div class="stat">
        <div class="k">未入批行</div>
        <div class="v"><span class="tag" :class="d.unscanned_rows > 0 ? 'bad' : 'ok'">{{ d.unscanned_rows > 0 ? d.unscanned_rows + ' 行待重扫' : '0' }}</span></div>
      </div>
      <div class="stat"><div class="k">未决差额</div><div class="v">{{ d.pending_variance }}</div></div>
      <div class="stat">
        <div class="k">结案</div>
        <div class="v"><span class="tag" :class="d.sealed ? 'seal' : 'bad'">{{ d.sealed ? '已结案' : '未结案' }}</span></div>
      </div>
    </div>
    <div class="panel" v-if="d && d.latest_batch">
      <h2>最近对账批次</h2>
      <p>{{ d.latest_batch.batch_no }} · 对齐 {{ d.latest_batch.matched_count }} · 差额 {{ d.latest_batch.variance_count }} · {{ d.latest_batch.status }}</p>
      <p>批快照：扫描 {{ d.latest_batch.scanned_rows }} 行 · 三腿行数 {{ d.latest_batch.expect_rows }}/{{ d.latest_batch.receive_rows }}/{{ d.latest_batch.confirm_rows }} · 三腿合计 {{ d.latest_batch.expect_total }}/{{ d.latest_batch.receive_total }}/{{ d.latest_batch.confirm_total }}</p>
    </div>
    <p class="msg">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const d = ref(null)
const err = ref('')
async function load() {
  err.value = ''
  try { d.value = await api('/dashboard') } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
