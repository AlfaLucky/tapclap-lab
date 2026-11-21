'use strict';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const terminal = new Set(['passed','failed','error','cancelled','interrupted','skipped']);
const labels = {passed:'Пройден',failed:'Ошибка',error:'Ошибка запуска',cancelled:'Остановлен',interrupted:'Прерван',queued:'В очереди',running:'Выполняется',skipped:'Пропущен'};
const state = {admin:false,catalog:null,runs:[],selected:null,run:null,events:[],lastSeq:0,mode:'snapshot',streamKey:'',filesKey:'',steps:[],capture:null,rect:null,polling:false};
const badge = status => `<span class="pill ${esc(status)}">${esc(labels[status] || status)}</span>`;
const formatTime = value => value ? new Date(value).toLocaleString('ru-RU', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
const seconds = value => `${Number(value || 0).toFixed(1)} с`;
let toastTimer;

function toast(message, error=false) {
  $('#toast').textContent = message; $('#toast').classList.toggle('error', error); $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden=true, 6000);
}
async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? 'GET':'POST', credentials:'same-origin',
    headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function needAdmin() { if (state.admin) return true; $('#loginDialog').showModal(); $('#adminToken').focus(); return false; }
function renderRole() {
  $('#role').textContent = state.admin ? 'Администратор' : 'Просмотр'; $('#role').classList.toggle('admin', state.admin);
  $('#authButton').textContent = state.admin ? 'Выйти' : 'Войти';
  $('#launchHint').textContent = state.admin ? 'Максимум 60 проверок · один worker' : 'Запуск доступен администратору';
}
function route() {
  const name = location.hash.slice(1) || 'overview';
  const valid = ['overview','history','stability','calibration','schedules'].includes(name) ? name : 'overview';
  $$('[data-page]').forEach(el => el.hidden=el.dataset.page !== valid);
  $$('[data-nav]').forEach(el => el.classList.toggle('active',el.dataset.nav === valid));
}
function options() {
  return {browser:$('#browser').value,profile:$('#profile').value,repeat:Number($('#repeat').value),timeout:Number($('#timeout').value),
    tests:$$('#testPicker input:checked').map(el => el.value)};
}
function renderCatalog() {
  if (!state.catalog) return;
  const previous = new Set($$('#testPicker input:checked').map(e=>e.value));
  const initial = !$('#testPicker').children.length;
  const tests = [...state.catalog.tests, ...Object.entries(state.catalog.scenarios).map(([id,s])=>({id:`scenario:${id}`,name:s.name,description:'Откалиброванный визуальный сценарий'}))];
  $('#testPicker').innerHTML = tests.map((test,i) => `<label class="test-option" title="${esc(test.description)}"><input type="checkbox" value="${esc(test.id)}" ${previous.has(test.id) || (initial && i<7) ? 'checked':''}><span>${esc(test.name)}</span></label>`).join('');
  const templates = Object.values(state.catalog.templates);
  $('#templateList').innerHTML = templates.length ? templates.map(t=>`<span class="template-chip">${esc(t.id)}<small>${esc(Object.keys(t.variants||{}).join(', ')||t.browser+'/'+t.profile)}</small></span>`).join('') : '<span class="muted">Шаблонов пока нет</span>';
  const selected = $('#stepTemplate').value;
  $('#stepTemplate').innerHTML = templates.map(t=>`<option value="${esc(t.id)}">${esc(t.id)}</option>`).join('');
  if (templates.some(t=>t.id === selected)) $('#stepTemplate').value=selected;
  $('#scenarioList').innerHTML = Object.entries(state.catalog.scenarios).map(([id,s])=>`<span class="template-chip">${esc(s.name)}<small>${s.steps.length} шагов</small></span>`).join('');
  $('#baselineName').textContent=`baseline_${$('#profile').value}_${$('#browser').value}`;
}
async function refreshCatalog() {state.catalog=await api('/api/catalog'); renderCatalog();}
function selectRun(id) {
  if (state.selected === id) return;
  state.selected=id;state.events=[];state.lastSeq=0;state.streamKey='';state.filesKey='';state.run=null;
  $('#liveImage').removeAttribute('src');$('#liveImage').hidden=true;$('#screenEmpty').hidden=false;
  $('#runVideo').pause();$('#runVideo').removeAttribute('src');$('#eventLog').innerHTML='';
}
function renderMetrics(stats) {
  $('#metricTotal').textContent=stats.runs;$('#metricPassed').textContent=stats.passed;$('#metricFailed').textContent=stats.failed;
  const queued=state.runs.filter(r=>r.status==='queued').length;
  $('#metricQueued').textContent=queued;
  $('#workerStatus').textContent=state.runs.some(r=>r.status==='running')?'Worker выполняет прогон':'Worker свободен';
}
function renderHistory() {
  const signature=JSON.stringify([$('#historyFilter').value,state.runs.map(r=>[r.id,r.status,r.results.length])]);
  if(signature===state.historyKey)return;
  state.historyKey=signature;
  const filtered=state.runs.filter(r=>$('#historyFilter').value==='all'||r.status===$('#historyFilter').value);
  $('#historyTable').innerHTML=filtered.length ? `<table><thead><tr><th>Прогон</th><th>Окружение</th><th>Результат</th><th>Проверки</th><th>Источник</th><th></th></tr></thead><tbody>${filtered.map(r=>`<tr><td><strong class="mono">${esc(r.id.slice(0,8))}</strong><small>${esc(formatTime(r.created))}</small></td><td>${esc(r.options.browser)}<small>${esc(r.options.profile)} · ×${r.options.repeat}</small></td><td>${badge(r.status)}</td><td>${r.results.filter(t=>t.status==='passed').length} / ${r.options.tests.length*r.options.repeat}<small>${r.results.filter(t=>t.status==='skipped').length} skipped</small></td><td>${esc(r.source)}</td><td><button class="button secondary small" data-open="${r.id}">Открыть ↗</button></td></tr>`).join('')}</tbody></table>` : '<div class="table-empty">Прогонов пока нет. Первый запуск создаст историю.</div>';
  ['#compareA','#compareB'].forEach((selector,index)=>{
    const old=$(selector).value;
    const choices=state.runs.filter(r=>terminal.has(r.status));
    $(selector).innerHTML=choices.map(r=>`<option value="${r.id}">${esc(r.id.slice(0,8))} · ${esc(r.options.browser)}/${esc(r.options.profile)} · ${esc(formatTime(r.created))}</option>`).join('');
    if(choices.some(r=>r.id===old))$(selector).value=old;else if(index && choices.length>1)$(selector).selectedIndex=1;
  });
}
function renderStability(stats) {
  $('#stabilityTable').innerHTML=stats.tests.length ? `<table><thead><tr><th>Проверка</th><th>Окружение</th><th>Pass rate</th><th>История</th><th>Среднее время</th><th>Длительность, последние 20</th></tr></thead><tbody>${stats.tests.map((t,i)=>`<tr><td><strong>${esc(t.name)}</strong>${t.unstable?'<small class="amber">Есть passed и failed</small>':''}</td><td>${esc(t.browser)}<small>${esc(t.profile)}</small></td><td>${t.pass_rate===null?'—':t.pass_rate+'%'}</td><td><span class="green">${t.passed} passed</span> / ${t.failed} failed<small>${t.skipped} skipped</small></td><td>${t.average===null?'—':seconds(t.average)}</td><td><canvas class="sparkline" width="300" height="64" id="spark${i}" aria-label="Длительность по прогонам"></canvas></td></tr>`).join('')}</tbody></table>` : '<div class="table-empty">Недостаточно данных. Выполни проверки, чтобы увидеть статистику.</div>';
  stats.tests.forEach((t,i)=>{
    const canvas=$(`#spark${i}`);if(!canvas||!t.durations.length)return;
    const ctx=canvas.getContext('2d'),values=t.durations,max=Math.max(...values,1);
    ctx.strokeStyle=t.unstable?'#e9b870':'#8be0c5';ctx.lineWidth=3;ctx.beginPath();
    values.forEach((v,j)=>{let x=8+j*284/Math.max(values.length-1,1),y=56-v/max*46;j?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();
    values.forEach((v,j)=>{ctx.beginPath();ctx.fillStyle='#8be0c5';ctx.arc(8+j*284/Math.max(values.length-1,1),56-v/max*46,3,0,Math.PI*2);ctx.fill();});
  });
}
function renderRun() {
  const run=state.run;if(!run)return;
  $('#runStatus').textContent=labels[run.status]||run.status;$('#runStatus').className=`pill ${run.status}`;
  $('#liveDot').classList.toggle('live',run.status==='running');
  $('#runMeta').textContent=`${run.id.slice(0,8)} · ${run.options.browser} / ${run.options.profile}`;
  $('#stopButton').disabled=!state.admin || !['running','queued'].includes(run.status)||run.cancel;
  $('#stopButton').textContent=run.cancel?'Останавливается…':'■ Остановить';
  const count=run.options.tests.length*run.options.repeat;
  $('#progressFill').style.width=`${run.results.length/count*100}%`;
  $('#runSummary').textContent=run.summary;$('#runSummary').hidden=!run.summary;
  const names=Object.fromEntries([...state.catalog.tests.map(t=>[t.id,t.name]),...Object.entries(state.catalog.scenarios).map(([id,s])=>[`scenario:${id}`,s.name])]);
  const lastStart=[...state.events].reverse().find(e=>e.type==='test_start');
  const rows=[];
  for(let repetition=1;repetition<=run.options.repeat;repetition++)for(const id of run.options.tests){
    const result=run.results.find(r=>r.id===id && r.repeat===repetition);
    const running=!result && run.status==='running' && lastStart?.data.id===id && lastStart?.data.repeat===repetition;
    const status=result?.status || (running?'running':terminal.has(run.status)?'not_run':'pending');
    const icon={passed:'✓',failed:'×',skipped:'−',running:'◌',cancelled:'■',not_run:'·',pending:'·'}[status]||'·';
    rows.push(`<div class="test-result ${esc(status)}"><span class="result-icon">${icon}</span><div>${esc(names[id]||id)}${run.options.repeat>1?` <span class="mono">#${repetition}</span>`:''}${result?.message?`<small>${esc(result.message)}</small>`:''}${status==='not_run'?'<small>Не выполнен</small>':''}</div><span class="mono">${result?seconds(result.duration):running?'в работе':'—'}</span>${result?.screenshot?`<a href="/api/runs/${run.id}/artifacts/${encodeURIComponent(result.screenshot)}" target="_blank">Кадр ↗</a>`:'<span></span>'}</div>`);
  }
  $('#testResults').className='';$('#testResults').innerHTML=rows.join('');
  $('#resultLinks').innerHTML=`<a href="/api/runs/${run.id}/report" target="_blank">Отчёт ↗</a>${terminal.has(run.status)?`<a href="/api/runs/${run.id}/download">ZIP ↓</a><button data-retry="${run.id}" class="text-button">Повторить всё</button>${run.results.some(t=>t.status==='failed')?`<button data-retry="${run.id}" data-failed-only="true" class="text-button">Только ошибки</button>`:''}`:''}`;
  const lastStep=[...state.events].reverse().find(e=>['step','error','run_end'].includes(e.type));
  $('#currentStep').textContent=lastStep?.data.message || labels[run.status] || 'Ожидание браузера';
  if(terminal.has(run.status)&&state.mode==='stream')setMonitorMode('snapshot');
  updateScreen();
}
function updateScreen() {
  const run=state.run;if(!run)return;
  $('#liveImage').hidden=state.mode==='video';$('#runVideo').hidden=state.mode!=='video';
  $('#videoSelect').hidden=state.mode!=='video'||!$('#videoSelect').options.length;
  $('#screenCaption').hidden=state.mode==='video';
  $('#screenCaption').textContent=state.mode==='stream'?'LIVE / MJPEG':run.status==='running'?'LIVE / SNAPSHOT':'ПОСЛЕДНИЙ КАДР';
  if(state.mode==='snapshot'){
    $('#liveImage').src=`/api/runs/${run.id}/snapshot?t=${Date.now()}`;
  }else if(state.mode==='stream'){
    const key=`${run.id}:stream`;
    if(state.streamKey!==key){$('#liveImage').src=`/api/runs/${run.id}/stream`;state.streamKey=key;}
  }else{
    $('#screenEmpty').hidden=!!$('#runVideo').getAttribute('src');
    $('#screenEmpty h3').textContent='Запись ещё не готова';
    $('#screenEmpty p').textContent='Видео появляется после завершения теста и закрытия браузерного контекста.';
  }
}
function setMonitorMode(mode) {
  if(state.mode===mode)return;
  if(state.mode==='stream'){$('#liveImage').removeAttribute('src');state.streamKey='';}
  if(state.mode==='video')$('#runVideo').pause();
  state.mode=mode;
  $$('[data-monitor]').forEach(el=>el.classList.toggle('selected',el.dataset.monitor===mode));updateScreen();
}
function renderEvents() {
  $('#eventCount').textContent=`${state.events.length} EVENTS`;
  $('#eventLog').innerHTML=state.events.slice(-150).map(e=>`<div class="event ${esc(e.type)}"><time>${esc(new Date(e.time).toLocaleTimeString('ru-RU'))}</time><span><span class="mono">${esc(e.type)}</span> &nbsp; ${esc(e.data.message || e.data.name || e.data.status || e.data.template || '')}</span></div>`).join('') || '<span class="muted">Ожидание событий…</span>';
  $('#eventLog').scrollTop=$('#eventLog').scrollHeight;
}
async function loadSelected() {
  if(!state.selected)return;
  const id=state.selected;
  const [run,events]=await Promise.all([api(`/api/runs/${id}`),api(`/api/runs/${id}/events?after=${state.lastSeq}`)]);
  if(id!==state.selected)return;
  state.run=run;
  if(events.length){state.events.push(...events);state.events=state.events.slice(-1000);state.lastSeq=events.at(-1).seq;renderEvents();}
  renderRun();
  const files=await api(`/api/runs/${id}/files`);
  if(id!==state.selected)return;
    const videos=[...run.results.map(t=>t.video).filter(v=>v&&files.includes(v)),...files.filter(p=>p.endsWith('.webm')&&!run.results.some(t=>t.video===p))];
  const key=JSON.stringify(videos);
  if(key!==state.filesKey){
    const previous=$('#videoSelect').value;
    $('#videoSelect').innerHTML=videos.map((v,i)=>`<option value="${esc(v)}">Запись ${i+1} · ${esc(v.split('/').at(-1))}</option>`).join('');
    if(videos.includes(previous))$('#videoSelect').value=previous;
    state.filesKey=key;
    if(videos.length)$('#runVideo').src=`/api/runs/${id}/artifacts/${$('#videoSelect').value}`;
  }
  $('#videoSelect').hidden=state.mode!=='video'||!videos.length;
}
async function renderSchedules() {
  const schedules=await api('/api/schedules');
  $('#scheduleList').innerHTML=schedules.length?schedules.map(s=>`<article class="schedule-card"><div class="schedule-card-header"><h3>${esc(s.name)}</h3><button data-schedule="${s.id}" data-enabled="${!s.enabled}" class="button small secondary">${s.enabled?'Приостановить':'Включить'}</button></div><p>${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')} · UTC${s.offset>=0?'+':''}${s.offset/60} · ${s.enabled?'Включено':'Пауза'}</p><p class="mono">${esc(s.options.browser)} / ${esc(s.options.profile)} · ${s.options.tests.length} тестов × ${s.options.repeat}</p><p>Следующий: ${s.enabled?esc(formatTime(s.next_at)):'—'}<br>Последний: ${esc(formatTime(s.last_at))}</p></article>`).join(''):'<div class="table-empty">Расписаний пока нет</div>';
}
let pollCount=0;
async function poll() {
  try{
    state.runs=await api('/api/runs');
    if(!state.selected&&state.runs.length)selectRun((state.runs.find(r=>r.status==='running')||state.runs[0]).id);
    const stats=await api('/api/stats');renderMetrics(stats);renderHistory();
    if(pollCount%4===0){renderStability(stats);await renderSchedules();}
    await loadSelected();$('#connection').textContent='Сервис подключён';pollCount++;
  }catch(error){$('#connection').textContent='Нет соединения';if(!pollCount)toast(error.message,true);}
  finally{setTimeout(poll,1500);}
}

$('#authButton').addEventListener('click',async()=>{
  if(!state.admin){$('#loginDialog').showModal();$('#adminToken').focus();return;}
  try{await api('/api/logout',{});state.admin=false;renderRole();toast('Вы вышли');}catch(e){toast(e.message,true);}
});
$('#closeLogin').addEventListener('click',()=>$('#loginDialog').close());
$('#loginForm').addEventListener('submit',async event=>{
  event.preventDefault();$('#loginError').textContent='';
  try{await api('/api/login',{token:$('#adminToken').value});$('#adminToken').value='';state.admin=true;renderRole();$('#loginDialog').close();await refreshCatalog();toast('Вход выполнен');}
  catch(error){$('#loginError').textContent=error.message;}
});
$('#runForm').addEventListener('submit',async event=>{
  event.preventDefault();if(!needAdmin())return;
  $('#launchButton').disabled=true;
  try{const run=await api('/api/runs',options());selectRun(run.id);await loadSelected();toast('Прогон добавлен в очередь');}
  catch(error){toast(error.message,true);}finally{$('#launchButton').disabled=false;}
});
$('#selectSmoke').addEventListener('click',()=>{$$('#testPicker input').forEach(el=>el.checked=state.catalog.tests.slice(0,7).some(t=>t.id===el.value));});
$('#stopButton').addEventListener('click',async()=>{if(!needAdmin()||!state.run)return;try{await api(`/api/runs/${state.run.id}/cancel`,{});await loadSelected();toast('Запрошена остановка');}catch(e){toast(e.message,true);}});
$$('[data-monitor]').forEach(el=>el.addEventListener('click',()=>setMonitorMode(el.dataset.monitor)));
$('#liveImage').addEventListener('load',()=>{
  if(state.mode==='video')return;
  $('#screenEmpty').hidden=true;$('#liveImage').hidden=false;
  $('#frameInfo').textContent=`${$('#liveImage').naturalWidth} × ${$('#liveImage').naturalHeight}`;
});
$('#liveImage').addEventListener('error',()=>{if(state.mode==='video')return;$('#liveImage').hidden=true;$('#screenEmpty').hidden=false;$('#screenEmpty h3').textContent='Ожидание кадра';$('#screenEmpty p').textContent=state.run?.summary||'Браузер запускается или артефакты удалены по сроку хранения.';});
$('#videoSelect').addEventListener('change',()=>{if(state.run){$('#runVideo').src=`/api/runs/${state.run.id}/artifacts/${$('#videoSelect').value}`;$('#screenEmpty').hidden=true;}});
$('#historyFilter').addEventListener('change',renderHistory);
document.addEventListener('click',async event=>{
  const open=event.target.closest('[data-open]');
  if(open){selectRun(open.dataset.open);location.hash='overview';try{await loadSelected();}catch(e){toast(e.message,true);}return;}
  const retry=event.target.closest('[data-retry]');
  if(retry&&needAdmin()){try{const run=await api(`/api/runs/${retry.dataset.retry}/retry`,{failed_only:retry.dataset.failedOnly==='true'});selectRun(run.id);await loadSelected();toast('Повтор добавлен в очередь');}catch(e){toast(e.message,true);}}
  const schedule=event.target.closest('[data-schedule]');
  if(schedule&&needAdmin()){try{await api(`/api/schedules/${schedule.dataset.schedule}`,{enabled:schedule.dataset.enabled==='true'});await renderSchedules();}catch(e){toast(e.message,true);}}
  const remove=event.target.closest('[data-remove-step]');
  if(remove){state.steps.splice(Number(remove.dataset.removeStep),1);renderSteps();}
});
$('#compareButton').addEventListener('click',async()=>{
  try{
    const {a,b}=await api(`/api/compare?a=${$('#compareA').value}&b=${$('#compareB').value}`);
    const different=a.options.browser!==b.options.browser||a.options.profile!==b.options.profile;
    const rows=new Map();
    a.results.forEach(r=>rows.set(`${r.id}:${r.repeat}`,{name:r.name,a:r}));
    b.results.forEach(r=>{const key=`${r.id}:${r.repeat}`;rows.set(key,{...(rows.get(key)||{name:r.name}),b:r});});
    $('#compareResult').innerHTML=(different?'<p class="notice">Разные окружения: сравнение длительности и изображения может быть некорректным.</p>':'')+`<table><thead><tr><th>Проверка</th><th>A · ${esc(a.id.slice(0,8))}</th><th>B · ${esc(b.id.slice(0,8))}</th><th>Δ время</th></tr></thead><tbody>${[...rows.values()].map(r=>`<tr><td>${esc(r.name)}</td><td>${r.a?badge(r.a.status)+' '+seconds(r.a.duration):'—'}</td><td>${r.b?badge(r.b.status)+' '+seconds(r.b.duration):'—'}</td><td>${r.a&&r.b?((r.b.duration-r.a.duration)>=0?'+':'')+(r.b.duration-r.a.duration).toFixed(1)+' с':'—'}</td></tr>`).join('')}</tbody></table>`;
  }catch(e){toast(e.message,true);}
});
$('#scheduleForm').addEventListener('submit',async event=>{
  event.preventDefault();if(!needAdmin())return;
  const [hour,minute]=$('#scheduleTime').value.split(':').map(Number);
  try{await api('/api/schedules',{name:$('#scheduleName').value,hour,minute,offset:Number($('#scheduleZone').value),options:options()});await renderSchedules();toast('Ежедневное расписание создано');}catch(e){toast(e.message,true);}
});

// Capture and crop the REAL worker screenshot; no page assets or mock game images.
const captureCanvas=$('#captureCanvas');const captureContext=captureCanvas.getContext('2d');
function drawCapture() {
  if(!state.capture)return;
  const image=state.capture.image;captureCanvas.width=image.naturalWidth;captureCanvas.height=image.naturalHeight;
  captureContext.drawImage(image,0,0);
  if(state.rect){const r=state.rect;captureContext.fillStyle='#8be0c522';captureContext.fillRect(r.x,r.y,r.w,r.h);captureContext.strokeStyle='#8be0c5';captureContext.lineWidth=3;captureContext.strokeRect(r.x,r.y,r.w,r.h);}
  $('#captureEmpty').hidden=true;
  $('#captureInfo').textContent=`${state.capture.browser} / ${state.capture.profile} · ${captureCanvas.width} × ${captureCanvas.height}`+(state.rect?` · выделение ${Math.round(Math.abs(state.rect.w))} × ${Math.round(Math.abs(state.rect.h))}`:'');
}
$('#captureButton').addEventListener('click',async()=>{
  if(!state.run)return toast('Сначала выбери или запусти прогон',true);
  try{
    const response=await fetch(`/api/runs/${state.run.id}/snapshot`);if(!response.ok)throw new Error('Кадр ещё не получен или удалён');
    const blob=await response.blob(),url=URL.createObjectURL(blob),image=new Image();
    await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=reject;image.src=url;});
    state.capture={image,profile:state.run.options.profile,browser:state.run.options.browser};state.rect=null;drawCapture();URL.revokeObjectURL(url);
  }catch(error){toast(error.message||'Не удалось прочитать кадр',true);}
});
let dragStart=null;
const point=event=>{const r=captureCanvas.getBoundingClientRect();return{x:Math.max(0,Math.min(captureCanvas.width,(event.clientX-r.left)*captureCanvas.width/r.width)),y:Math.max(0,Math.min(captureCanvas.height,(event.clientY-r.top)*captureCanvas.height/r.height))};};
captureCanvas.addEventListener('pointerdown',event=>{if(!state.capture)return;captureCanvas.setPointerCapture(event.pointerId);dragStart=point(event);});
captureCanvas.addEventListener('pointermove',event=>{if(!dragStart)return;const p=point(event);state.rect={x:Math.min(p.x,dragStart.x),y:Math.min(p.y,dragStart.y),w:Math.abs(p.x-dragStart.x),h:Math.abs(p.y-dragStart.y)};drawCapture();});
captureCanvas.addEventListener('pointerup',()=>dragStart=null);
captureCanvas.addEventListener('pointercancel',()=>dragStart=null);
$('#selectFullFrame').addEventListener('click',()=>{if(state.capture){state.rect={x:0,y:0,w:captureCanvas.width,h:captureCanvas.height};drawCapture();}});
$('#saveTemplate').addEventListener('click',async()=>{
  if(!needAdmin())return;
  if(!state.capture||!state.rect||state.rect.w<8||state.rect.h<8)return toast('Выдели область не менее 8 × 8 px',true);
  const crop=document.createElement('canvas'),r=state.rect;crop.width=Math.round(r.w);crop.height=Math.round(r.h);
  crop.getContext('2d').drawImage(state.capture.image,Math.round(r.x),Math.round(r.y),crop.width,crop.height,0,0,crop.width,crop.height);
  try{await api('/api/templates',{id:$('#templateName').value.trim(),image:crop.toDataURL('image/png'),profile:state.capture.profile,browser:state.capture.browser});await refreshCatalog();toast('Шаблон сохранён');}catch(e){toast(e.message,true);}
});
const actionNames={wait:'Ждать',click:'Нажать',assert:'Проверить',reload:'Перезагрузить',screenshot:'Скриншот',drag:'Перетащить'};
function renderSteps() {$('#scenarioSteps').innerHTML=state.steps.map((s,i)=>`<div class="scenario-row"><span class="step-number">${i+1}</span><span>${esc(actionNames[s.action])} ${esc(s.template||s.coords?.join(', ')||'')}</span><button data-remove-step="${i}" aria-label="Удалить шаг ${i+1}">×</button></div>`).join('');}
$('#stepAction').addEventListener('change',()=>{const a=$('#stepAction').value;$('#stepTemplate').hidden=!['wait','click','assert'].includes(a);$('#dragCoords').hidden=a!=='drag';});
$('#addStep').addEventListener('click',()=>{
  if(state.steps.length>=30)return toast('Не более 30 шагов',true);
  const action=$('#stepAction').value,step={action};
  if(['wait','click','assert'].includes(action)){if(!$('#stepTemplate').value)return toast('Сначала создай шаблон',true);step.template=$('#stepTemplate').value;}
  if(action==='drag'){const coords=$('#dragCoords').value.split(',').map(Number);if(coords.length!==4||coords.some(c=>!Number.isFinite(c)||c<0||c>1))return toast('Нужны 4 числа от 0 до 1 через запятую',true);step.coords=coords;}
  state.steps.push(step);renderSteps();
});
$('#saveScenario').addEventListener('click',async()=>{if(!needAdmin())return;try{await api('/api/scenarios',{name:$('#scenarioName').value,steps:state.steps});state.steps=[];renderSteps();$('#scenarioName').value='';await refreshCatalog();toast('Сценарий добавлен в набор проверок');}catch(e){toast(e.message,true);}});
['#profile','#browser'].forEach(s=>$(s).addEventListener('change',()=>{$('#baselineName').textContent=`baseline_${$('#profile').value}_${$('#browser').value}`;}));
window.addEventListener('hashchange',route);
async function init() {
  route();renderRole();
  try{const session=await api('/api/session');state.admin=session.admin;renderRole();await refreshCatalog();}
  catch(e){toast(e.message,true);if(!state.admin)$('#loginDialog').showModal();}
  poll();
}
init();
