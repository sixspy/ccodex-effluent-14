export const reconcileViewTemplate = `
  <div class="grid-2">
    <div class="panel">
      <h3>许可对账 · 双计灯</h3>
      <div class="actions"><button class="secondary" @click="loadReconcile">刷新全部</button></div>
      <table>
        <thead><tr><th>许可</th><th>对齐</th><th>证明</th><th>封批数</th><th>双计灯</th></tr></thead>
        <tbody>
          <tr v-for="r in reconAllItems" :key="r.permit_code">
            <td>{{ r.permit_code }}</td>
            <td :class="r.balanced?'pass':'fail'">{{ r.balanced ? "齐" : "不齐" }}</td>
            <td :class="r.proof_ready?'pass':'warn'">{{ r.proof_ready ? "可证" : "未齐" }}</td>
            <td>{{ r.seal_count || 0 }}</td>
            <td>
              <span class="lamp" :class="r.double_count ? 'lamp-on' : 'lamp-off'">
                {{ r.double_count_lamp || (r.double_count ? "双计灯亮" : "双计灯灭") }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="hint">同口双正式并存时双计灯亮；甲路径废旧升新后同口仅一代计入，灯灭。影子新代未升正一律剔除，不双计。</p>
    </div>
    <div class="panel">
      <h3>单许可 / 采样证明</h3>
      <label>许可号</label><input v-model="reconCode"/>
      <div class="actions"><button @click="loadReconcileOne">对账</button></div>
      <div v-if="reconOne">
        <p>
          双计灯：
          <span class="lamp" :class="reconOne.double_count ? 'lamp-on' : 'lamp-off'">{{ reconOne.double_count_lamp }}</span>
          <span v-if="reconOne.shadow_quarantined" class="badge badge-shadow">影子隔离 {{ reconOne.shadow_quarantined }}</span>
          <span v-if="reconOne.voided_count" class="badge badge-void">作废 {{ reconOne.voided_count }}</span>
        </p>
        <div class="actions" v-if="reconOne.double_count">
          <button class="warn mini" @click="remediateDual">核销同口双正式（废旧留新·同事务）</button>
        </div>
        <table>
          <thead><tr><th>采样</th><th>代</th><th>状态</th><th>对账</th><th>口径</th></tr></thead>
          <tbody>
            <tr v-for="s in reconOne.samples" :key="s.sample_code"
                :class="{'row-void': s.voided, 'row-shadow': s.shadow}">
              <td>{{ s.sample_code }}</td><td>G{{ s.generation }}</td><td>{{ s.status }}</td>
              <td :class="s.counted ? 'pass' : 'muted'">{{ s.counted === false ? "剔除不计" : "计入" }}</td>
              <td class="hint">{{ s.count_note }}</td>
            </tr>
          </tbody>
        </table>
        <pre style="white-space:pre-wrap;font-size:0.78rem;margin-top:0.6rem">{{ JSON.stringify({balanced:reconOne.balanced,double_count:reconOne.double_count,counted_sample_codes:reconOne.counted_sample_codes}, null, 2) }}</pre>
      </div>
      <label>采样号证明</label><input v-model="proofCode"/>
      <div class="actions"><button class="secondary" @click="loadProof">证明</button></div>
      <pre v-if="proofOne" style="white-space:pre-wrap;font-size:0.82rem">{{ JSON.stringify(proofOne, null, 2) }}</pre>
    </div>
  </div>`;
