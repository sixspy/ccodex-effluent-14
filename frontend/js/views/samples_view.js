export const samplesViewTemplate = `
  <div v-if="regenDecision && regenDecision.sealed" class="banner ok regen-seal-banner">
    二次升代拍板：<b>{{ regenDecision.decision_label || "甲路径" }}</b>
    ｜同事务废旧 + 升新正式，否则整笔回退｜{{ regenDecision.sealed_at }} 当日封死，折中禁止
  </div>
  <div class="grid-2">
    <div class="panel">
      <h3>采样批次 · 同口二次采样升代互斥</h3>
      <table>
        <thead><tr>
          <th>编号</th><th>口</th><th>许可</th><th>流量</th><th>状态/作废标记</th><th>代</th><th>同口再采</th>
        </tr></thead>
        <tbody>
          <tr v-for="s in samples" :key="s.id"
              :class="{'row-void': isVoidSample(s), 'row-shadow': isShadowSample(s)}"
              @click="loadSample(s.id)" style="cursor:pointer">
            <td>{{ s.sample_code }}</td>
            <td>{{ s.outfall_code }}</td>
            <td>{{ s.permit_ref }}</td>
            <td>{{ s.flow_m3h }}</td>
            <td>
              <span :class="sampleClass(s.status)">{{ s.status }}</span>
              <span v-if="isVoidSample(s)" class="badge badge-void">已作废</span>
              <span v-if="isShadowSample(s)" class="badge badge-shadow">影子·未升正</span>
              <div v-if="s.void_reason" class="void-reason" :title="s.void_reason">作废因：{{ s.void_reason }}</div>
            </td>
            <td>G{{ s.generation }}<span v-if="s.gen_state && s.gen_state!=='正式'" class="gen-state">（{{ s.gen_state }}）</span></td>
            <td @click.stop>
              <button class="mini warn" :disabled="isVoidSample(s) || isShadowSample(s)"
                      @click="regenFromRow(s)">同口再采升代</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="sampleDetail" class="panel" style="margin-top:0.8rem;border-style:dashed">
        <h3>批次详情</h3>
        <div class="kv">
          <span>指标</span><span>{{ fmtMetrics(sampleDetail.sample.metrics) }}</span>
          <span>世代态</span><span>{{ sampleDetail.sample.gen_state || "正式" }}</span>
          <span>作废</span><span>{{ sampleDetail.sample.status === "作废" ? ("是 · " + (sampleDetail.sample.void_reason || "")) : "否" }}</span>
          <span>挂起</span><span>{{ sampleDetail.exceed_holds.length }}</span>
          <span>准排</span><span>{{ sampleDetail.discharge_releases.length }}</span>
          <span>封批</span><span>{{ sampleDetail.discharge_seals.length }}</span>
        </div>
      </div>
    </div>
    <div>
      <div class="panel regen-panel">
        <h3>同口二次采样升代（甲路径）</h3>
        <p class="hint">同一事务内：旧批作废标记 ＋ 新代正式入库 ＋ 血缘父子边；任一步失败整笔回退。两路会话同抢一口时后到 409 只留争用痕。</p>
        <div class="form-row"><div><label>排放口 ID</label><input type="number" v-model.number="regenForm.outfall_id"/></div>
        <div><label>许可号</label><input v-model="regenForm.permit_ref"/></div></div>
        <label>新代编号（可空自动）</label><input v-model="regenForm.new_sample_code" placeholder="如 SM-N14b"/>
        <div class="form-row">
          <div><label>COD</label><input type="number" v-model.number="regenForm.cod"/></div>
          <div><label>氨氮</label><input type="number" v-model.number="regenForm.nh3n"/></div>
        </div>
        <div class="form-row">
          <div><label>总磷</label><input type="number" v-model.number="regenForm.tp"/></div>
          <div><label>流量 m3/h</label><input type="number" v-model.number="regenForm.flow_m3h"/></div>
        </div>
        <label>会话标识（争用区分两路会话，可空）</label><input v-model="regenForm.session_id"/>
        <div class="actions"><button class="warn" @click="createRegen()">废旧升新·同事务提交</button></div>
      </div>
      <div class="panel">
        <h3>首次入库采样</h3>
        <div class="form-row"><div><label>排放口 ID</label><input type="number" v-model.number="sampleForm.outfall_id"/></div>
        <div><label>许可号</label><input v-model="sampleForm.permit_ref"/></div></div>
        <div class="form-row">
          <div><label>COD</label><input type="number" v-model.number="sampleForm.cod"/></div>
          <div><label>氨氮</label><input type="number" v-model.number="sampleForm.nh3n"/></div>
        </div>
        <div class="form-row">
          <div><label>总磷</label><input type="number" v-model.number="sampleForm.tp"/></div>
          <div><label>流量 m3/h</label><input type="number" v-model.number="sampleForm.flow_m3h"/></div>
        </div>
        <label>编号（可空自动）</label><input v-model="sampleForm.sample_code"/>
        <div class="actions"><button @click="createSample">入库</button></div>
      </div>
    </div>
  </div>`;
