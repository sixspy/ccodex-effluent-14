export const sealViewTemplate = `
  <div class="banner warn regen-seal-banner">
    封批互斥：已在二次升代中<b>作废的旧批不得升代封批</b>；影子新代未升正同样禁封。后端对违规操作返回 409。
  </div>
  <div class="grid-2">
    <div class="panel">
      <h3>封批快照</h3>
      <table>
        <thead><tr><th>封批号</th><th>采样</th><th>代</th><th>快照</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="s in seals" :key="s.id">
            <td>{{ s.seal_ref }}</td><td>{{ s.sample_code }}</td><td>{{ s.sample_generation }}</td>
            <td>{{ s.snapshot }}</td><td>{{ s.sealed_at }}</td>
          </tr>
        </tbody>
      </table>
      <h3 style="margin-top:1rem">禁止升代封批清单（同源采样状态）</h3>
      <table>
        <thead><tr><th>编号</th><th>口</th><th>世代</th><th>状态</th><th>禁封原因</th></tr></thead>
        <tbody>
          <tr v-for="s in forbiddenSealSamples" :key="'f-'+s.id" class="row-void">
            <td>{{ s.sample_code }}</td><td>{{ s.outfall_code }}</td><td>G{{ s.generation }}</td>
            <td><span class="badge badge-void" v-if="isVoidSample(s)">已作废</span>
                <span class="badge badge-shadow" v-else-if="isShadowSample(s)">影子未升正</span></td>
            <td>{{ isVoidSample(s) ? "旧批已作废，禁止再升代封批" : "影子代未原子升正，禁止封批" }}</td>
          </tr>
          <tr v-if="!forbiddenSealSamples.length"><td colspan="5" class="muted">暂无已废旧批/影子代</td></tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h3>执行封批</h3>
      <label>可封批采样（已排除作废/影子）</label>
      <select v-model.number="sealForm.sample_id">
        <option v-for="s in sealableSamples" :key="s.id" :value="s.id">{{ s.sample_code }} · {{ s.status }} · G{{ s.generation }}</option>
      </select>
      <p v-if="!sealableSamples.length" class="hint">当前没有“已裁定/已准排”的在账批次可封批。</p>
      <label>快照（可空自动）</label><input v-model="sealForm.snapshot"/>
      <div class="actions"><button class="warn" @click="createSeal">封批升代</button></div>
    </div>
  </div>`;
