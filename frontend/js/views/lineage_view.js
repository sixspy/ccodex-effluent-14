export const lineageViewTemplate = `
  <div class="banner ok regen-seal-banner">
    血缘同源闸：甲路径升代后<b>只保留新代父子边</b>上页；旧边置"已替代"留档，不与新代边并列造成血缘双叉。
  </div>
  <div class="grid-2">
    <div class="panel">
      <h3>指标血缘 · 新代父子边</h3>
      <div class="actions">
        <button class="secondary mini" @click="toggleLineageCurrent">
          {{ lineageCurrentOnly ? "当前：仅新代边（点看全部留档）" : "当前：全部边（点回仅新代）" }}
        </button>
      </div>
      <table>
        <thead><tr><th>从（代）</th><th>到（代）</th><th>边类型</th><th>说明</th><th>代际状态</th></tr></thead>
        <tbody>
          <tr v-for="e in lineageEdges" :key="e.id" :class="{'row-superseded': e.is_current === 0}">
            <td>{{ e.from_code }}<span class="gen-state" v-if="e.from_generation"> G{{ e.from_generation }}</span></td>
            <td>{{ e.to_code || "—" }}<span class="gen-state" v-if="e.to_generation"> G{{ e.to_generation }}</span></td>
            <td>{{ e.edge_type }}</td><td>{{ e.detail }}</td>
            <td>
              <span v-if="e.is_current === 1 || e.is_current === true" class="badge badge-current">新代当前边</span>
              <span v-else class="badge badge-superseded">已替代留档</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h3>登记血缘边</h3>
      <div class="form-row"><div><label>源采样 ID</label><input type="number" v-model.number="lineageForm.from_sample_id"/></div>
      <div><label>目标 ID</label><input type="number" v-model.number="lineageForm.to_sample_id"/></div></div>
      <label>边类型</label><input v-model="lineageForm.edge_type"/>
      <label>说明</label><input v-model="lineageForm.detail"/>
      <div class="actions"><button @click="createLineage">写入</button></div>
      <p class="hint">二次升代边由采样服务在废旧升新同一事务内自动写入，无需手工补边。</p>
    </div>
  </div>`;
