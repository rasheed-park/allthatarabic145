const DATA_URL = 'data/review-data.json';
const ISSUE_URL = 'https://github.com/rasheed-park/allthatarabic145/issues/new';
const state = { data:null, unit:'', pattern:'', type:'', unresolved:false };
const stateInfo = {
  'needs-review':['신규 검토 필요','new'], 'needs-recheck':['재검수 필요','recheck'],
  'feedback':['피드백 접수','feedback'], 'in-progress':['수정 중','working'],
  'passed':['통과','passed'], 'deleted':['시트 삭제됨','deleted']
};
const activeStates = new Set(['needs-review','needs-recheck','feedback','in-progress']);
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

function options(select, values, label) {
  const current = select.value;
  [...select.querySelectorAll('option:not(:first-child)')].forEach(node => node.remove());
  values.forEach(value => { const option=document.createElement('option'); option.value=value; option.textContent=label(value); select.append(option); });
  select.value = current;
}
function statusOf(record) { return record.review?.state || 'needs-review'; }
function isUnresolved(record) { return activeStates.has(statusOf(record)); }
function filtered() {
  return state.data.records.filter(record =>
    (!state.unit || record.u === state.unit) && (!state.pattern || record.ptn === state.pattern) &&
    (!state.type || record.type === state.type) && (!state.unresolved || isUnresolved(record)));
}
function grouped(records, keyOf = row => row.u) {
  return records.reduce((groups, row) => {
    const key = keyOf(row);
    (groups[key] ||= []).push(row);
    return groups;
  }, {});
}
function badge(record) { const [label, css] = stateInfo[statusOf(record)] || stateInfo['needs-review']; return `<span class="badge ${css}">${label}</span>`; }
function changed(record) { return statusOf(record)==='needs-recheck' || (record.review?.changedFields || []).length > 0; }
function audio(record, label='듣기') {
  const urls = record.audio?.candidates || [];
  if (!urls.length) return '<span class="audio-missing">오디오 URL이 없습니다.</span>';
  return `<audio controls preload="none" data-candidates="${escape(JSON.stringify(urls))}" aria-label="${label}"><source src="${escape(urls[0])}"></audio>`;
}
function initAudioFallback(root=document) {
  root.querySelectorAll('audio[data-candidates]').forEach(player => {
    const urls = JSON.parse(player.dataset.candidates); let index = 0;
    player.addEventListener('error', () => { index += 1; if (urls[index]) { player.src=urls[index]; player.load(); } }, { once:false });
  });
}
function issueLink(record, category='검수 의견') {
  const title = `[ATA QA] ${record.u} ${record.ptn || '유닛'} ${record.id} — ${category}`;
  const body = `## 검수 항목\n- 유닛: ${record.u}\n- 패턴: ${record.ptn || '없음'}\n- 콘텐츠 ID: ${record.id}\n- 타입: ${record.type}\n- 현재 상태: ${stateInfo[statusOf(record)][0]}\n\n## 문제 유형\n- [ ] 원문 오류\n- [ ] TSS·발음\n- [ ] 화자\n- [ ] 방언\n- [ ] 자연스러움\n- [ ] 학습범위\n- [ ] 구조·노출\n\n## 재현 위치와 수정 요청\n`; 
  return `${ISSUE_URL}?${new URLSearchParams({title, body}).toString()}`;
}
function renderRecord(record) {
  const template = $('itemTemplate').content.cloneNode(true);
  const card = template.querySelector('.item-card');
  const context = template.querySelector('.item-context');
  const body = template.querySelector('.item-body');
  const actions = template.querySelector('.item-actions');
  const comparison = template.querySelector('.comparison');
  const wasChanged = changed(record);
  card.classList.toggle('changed', wasChanged);
  context.innerHTML = `<span class="chip">${escape(record.u)}</span>${record.ptn?`<span class="chip">${escape(record.ptn)}</span>`:''}<span class="chip">${escape(record.type)}</span>${badge(record)}${wasChanged?'<span class="badge changed">수정됨</span>':''}`;
  const lines = String(record.arabic || '').split('\n').filter(Boolean);
  const arabic = record.type.startsWith('nass') && lines.length > 1
    ? `<div class="nass-lines">${lines.map((line,index)=>`<div class="nass-line arabic"><span class="speaker">${escape((record.voi||'').charAt(index) || '·')}</span>${escape(line)}</div>`).join('')}</div>`
    : `<div class="arabic">${escape(record.arabic)}</div>`;
  body.innerHTML = `${arabic}<div class="meaning">${escape(record.korean)}</div><div class="details"><span><b>ID</b> ${escape(record.id)}</span><span><b>방언</b> ${escape(record.lahja || '—')}</span><span><b>화자</b> ${escape(record.voi || '—')}</span><span><b>시트 상태</b> ${escape(record.sheetStatus || '—')}</span></div>${record.tss?`<div class="tts"><b>TSS 입력</b> ${escape(record.tss)}</div>`:''}<div class="audio-row">${audio(record,'현재 오디오 듣기')}</div>`;
  const feedback = document.createElement('a'); feedback.className='action'; feedback.target='_blank'; feedback.rel='noreferrer'; feedback.href=issueLink(record); feedback.textContent='피드백 남기기'; actions.append(feedback);
  if (wasChanged && record.review?.previous) {
    const toggle = document.createElement('button'); toggle.className='action'; toggle.textContent='수정 보기'; toggle.addEventListener('click', () => { comparison.hidden=!comparison.hidden; toggle.textContent=comparison.hidden?'수정 보기':'비교 닫기'; }); actions.prepend(toggle);
    const previous=record.review.previous, fields=(record.review.changedFields||[]).join(' · ') || '검수 대상 값';
    comparison.innerHTML = `<h3>수정 전·후 비교</h3><p class="changed-fields">바뀐 항목: ${escape(fields)}</p><div class="compare-grid"><div class="compare-box"><div class="compare-label">이전 통과본</div><div class="arabic">${escape(previous.arabic || '—')}</div><div class="meaning">${escape(previous.korean || '')}</div>${previous.tss?`<div class="tts"><b>TSS</b> ${escape(previous.tss)}</div>`:''}<div class="audio-row">${audio(previous,'이전 오디오 듣기')}</div></div><div class="compare-box"><div class="compare-label">수정본</div><div class="arabic">${escape(record.arabic || '—')}</div><div class="meaning">${escape(record.korean || '')}</div>${record.tss?`<div class="tts"><b>TSS</b> ${escape(record.tss)}</div>`:''}<div class="audio-row">${audio(record,'수정본 오디오 듣기')}</div></div></div>`;
  }
  return template;
}
function aggregate(rows) {
  const states = rows.map(statusOf); if (states.some(s=>s==='needs-review'||s==='needs-recheck')) return 'needs-recheck'; if (states.some(s=>s==='feedback')) return 'feedback'; if (states.some(s=>s==='in-progress')) return 'in-progress'; if (states.some(s=>s==='passed')) return 'passed'; return 'deleted';
}
function renderNavigator(rows) {
  const nav=$('navigator'); nav.replaceChildren(); const groups=grouped(rows); const units=state.data.units.filter(unit=>groups[unit.id]);
  units.forEach(unit => {
    const unitRows=groups[unit.id], holder=document.createElement('div'); holder.className='nav-unit';
    const unitButton=document.createElement('button'); const unitState=aggregate(unitRows); unitButton.innerHTML=`<i class="dot dot-${stateInfo[unitState][1]}"></i><span class="nav-unit-name">${escape(unit.id)} ${escape(unit.korean||'')}</span><span class="count">${unitRows.filter(isUnresolved).length}/${unitRows.length}</span>`;
    const patterns=document.createElement('div'); patterns.className='nav-patterns';
    const patternGroups=grouped(unitRows,row=>row.ptn||'기타'); Object.entries(patternGroups).forEach(([ptn, records])=>{ const button=document.createElement('button'); button.className='nav-pattern'; if(state.unit===unit.id && state.pattern===ptn) button.classList.add('active'); const pState=aggregate(records); button.innerHTML=`<i class="dot dot-${stateInfo[pState][1]}"></i><span class="nav-unit-name">${escape(ptn)}</span><span class="count">${records.filter(isUnresolved).length}/${records.length}</span>`; button.addEventListener('click',()=>{state.unit=unit.id;state.pattern=ptn==='기타'?'':ptn; syncFilters();render();}); patterns.append(button); });
    unitButton.addEventListener('click',()=>{ if(state.unit===unit.id && !state.pattern) holder.classList.toggle('open'); else { state.unit=unit.id;state.pattern='';syncFilters();render(); }});
    if(state.unit===unit.id) holder.classList.add('open'); if(state.unit===unit.id&&!state.pattern)unitButton.classList.add('active'); holder.append(unitButton,patterns);nav.append(holder);
  });
  $('navCount').textContent=`${rows.filter(isUnresolved).length}개 미통과`;
}
function render() {
  const rows=filtered(); const list=$('contentList'); list.replaceChildren(); rows.forEach(row=>list.append(renderRecord(row))); $('emptyState').hidden=rows.length>0; initAudioFallback(list); renderNavigator(state.data.records); const unresolved=state.data.records.filter(isUnresolved).length; $('summary').textContent=`전체 ${state.data.records.length}개 · 미통과 ${unresolved}개`;
}
function syncFilters() { $('unitFilter').value=state.unit; $('patternFilter').value=state.pattern; $('typeFilter').value=state.type; $('unresolvedOnly').checked=state.unresolved; }
function setupFilters() {
  const records=state.data.records; options($('unitFilter'),state.data.units.map(unit=>unit.id),value=>value); options($('patternFilter'),[...new Set(records.map(row=>row.ptn).filter(Boolean))].sort(),value=>value); options($('typeFilter'),[...new Set(records.map(row=>row.type))].sort(),value=>value);
  [['unitFilter','unit'],['patternFilter','pattern'],['typeFilter','type']].forEach(([id,key])=>$ (id).addEventListener('change',event=>{state[key]=event.target.value;render();})); $('unresolvedOnly').addEventListener('change',event=>{state.unresolved=event.target.checked;render();});
}
async function start() {
  try { const response=await fetch(DATA_URL,{cache:'no-store'}); if(!response.ok)throw new Error('검수 데이터를 찾지 못했습니다.'); state.data=await response.json(); $('snapshotMeta').textContent=`스냅샷 ${new Date(state.data.generatedAt).toLocaleString('ko-KR')} · 시트 기준`; setupFilters(); render(); }
  catch(error) { $('snapshotMeta').textContent='검수 데이터를 불러오지 못했습니다.'; $('emptyState').hidden=false; $('emptyState').innerHTML=`<strong>검수 데이터 로딩 실패</strong><span>${escape(error.message)}</span>`; }
}
start();
