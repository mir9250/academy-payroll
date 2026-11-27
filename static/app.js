"use strict";
// Local session authentication; every payroll API is also authorized on the server.
const app = document.getElementById('app');
const toastElement = document.getElementById('toast');
const today = new Date();
const defaultDay = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
const initialMonth = defaultDay.slice(0,7);
const state = {
  page:'home', month:initialMonth, selectedDate:defaultDay, teacherId:null,
  manageTab:'register', editTeacherId:null, boot:null, pageData:null,
};
let toastTimer;
const PAY_LABEL = {hourly:'시급',daily:'일급',monthly:'월급',mixed:'유형 변경'};
const contractLabel = t => t.role==='director'?'원장님':`${PAY_LABEL[t.pay_type] || '시급'} ${KRW(t.amount ?? t.hourly_rate)}`;
const KRW = n => n===null ? '정산 확인 필요' : `${Number(n || 0).toLocaleString('ko-KR')}원`;
const HOURS = mins => `${Math.floor(mins / 60)}시간${mins % 60 ? ` ${mins % 60}분` : ''}`;
const escapeHtml = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const el = (tag, cls, text) => {const d=document.createElement(tag);d.className=cls;d.textContent=text;return d;};
const pad = n => String(n).padStart(2,'0');
const monthLabel = m => `${m.slice(0,4)}년 ${Number(m.slice(5))}월`;
const personRole = r => r === 'director' ? '원장님' : '일반 선생님';
const fmtDay = d => `${Number(d.slice(5,7))}월 ${Number(d.slice(8))}일`;
const icons = {
  person:'♧',director:'▤',manage:'⚙',arrow:'→',
};
const logoGraphic = `<svg viewBox="0 0 80 80" fill="none" aria-hidden="true"><path d="M10 20q15-7 30 5v36q-15-12-30-5V20Zm60 0q-15-7-30 5v36q15-12 30-5V20Z" stroke="currentColor" stroke-width="3.5" stroke-linejoin="round"/><path d="M40 26v35" stroke="currentColor" stroke-width="3"/><path d="m40 11 2.7 7.5L50 21l-7.3 2.5L40 31l-2.7-7.5L30 21l7.3-2.5L40 11Z" fill="currentColor"/></svg>`;

async function api(url, opts={}) {
  const res=await fetch(url, opts);
  let payload;
  try {payload=await res.json();}catch{throw new Error('서버 응답을 읽지 못했습니다.');}
  if(!res.ok) {
    const msg = typeof payload.detail==='string' ? payload.detail : '요청을 처리하지 못했습니다.';
    if(res.status===401 && url!=='/api/auth/login' && state.boot?.user) {state.pageData=null;refresh();}
    if(res.status===401 && url!=='/api/auth/login' && state.boot?.user){state.pageData=null;refresh();}
    const error=new Error(msg);error.status=res.status;throw error;
  }
  return payload;
}
const json = (method, value) => ({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
function toast(message, error=false){
  clearTimeout(toastTimer);
  toastElement.textContent=message;
  toastElement.classList.toggle('error',error);
  toastElement.hidden=false;
  toastTimer=setTimeout(()=>toastElement.hidden=true,6500);
}
function backupToast(result){
  if(result?.backup && !result.backup.ok) toast(result.backup.message,true);
  else toast('저장 완료 · 엑셀 자동 백업 갱신');
}
function errorReport(e){console.error(e);toast(e?.message || '오류가 발생했습니다.',true);}
function titleSection(title,desc,goBack='home'){
  return `<section class="page-header"><div><button class="back" data-action="${goBack}">‹ 뒤로가기</button><p class="eyebrow">ACADEMY PAYROLL</p><h1>${escapeHtml(title)}</h1><p>${escapeHtml(desc)}</p></div></section>`;
}
function changeMonth(delta){
  const [y,m]=state.month.split('-').map(Number);
  const next=new Date(y,m-1+delta,1);
  state.month=`${next.getFullYear()}-${pad(next.getMonth()+1)}`;
  state.selectedDate=`${state.month}-01`;
  refresh();
}
function needsLogin(route,boot){
  const user=boot.user;
  if(['director','manage','payslip'].includes(route.page) || route.page==='teacher' && route.readOnly) return user?.role!=='director';
  if(route.page==='teacher')return user?.role!=='teacher' || user.id!==route.teacherId;
  return false;
}
function renderSetup(){
  return titleSection('최초 원장님 계정 설정','기존 데이터는 유지됩니다. 학원 컴퓨터에서 한 번만 관리자 계정을 설정합니다.')+
    `<div class="card"><form id="setup-form"><div class="field"><label>원장님 이름<input name="name" required maxlength="80"></label></div>
    <div class="field"><label>로그인 아이디<input name="login_id" required pattern="[A-Za-z0-9_.-]{3,80}" autocomplete="username"></label></div>
    <div class="field"><label>비밀번호 (숫자 4자리)<input name="password" type="password" required inputmode="numeric" pattern="[0-9]{4}" minlength="4" maxlength="4" title="숫자 4자리로 입력해 주세요." autocomplete="new-password"></label></div>
    <button class="btn" type="submit">최초 관리자 등록</button></form></div>`;
}
function renderLogin(){
  const teacher=state.page==='teacher' && !state.readOnly;
  const selected=state.boot.teachers.find(t=>t.id===state.teacherId && t.role==='teacher');
  if(teacher && !selected)return titleSection('선생님을 찾을 수 없습니다.','선생님 목록에서 이름을 선택해 주세요.','choose');
  return titleSection(teacher?`${selected.name} 선생님 로그인`:'원장님 로그인',teacher?`${selected.login_id || '로그인 정보 미설정'} · 본인 비밀번호를 입력해 주세요.`:'원장님 아이디와 비밀번호를 입력해 주세요.',teacher?'choose':'home')+
    `<div class="card"><form id="login-form">${teacher?`<input type="hidden" name="teacher_id" value="${selected.id}">`:'<div class="field"><label>원장님 로그인 아이디<input name="login_id" required autocomplete="username"></label></div>'}
    <div class="field"><label>비밀번호 (숫자 4자리)<input name="password" type="password" required inputmode="numeric" pattern="[0-9]{4}" minlength="4" maxlength="4" title="숫자 4자리로 입력해 주세요." autocomplete="current-password"></label></div>
    <button class="btn" type="submit">로그인</button></form><p class="muted small">로그인 정보가 미설정된 기존 계정은 원장님이 선생님 관리에서 아이디와 비밀번호를 설정해야 합니다.</p></div>`;
}
async function logout(){
  ++refreshVersion;state.pageData=null;app.innerHTML='';
  try {await api('/api/auth/logout',json('POST',{}));await refresh();}catch(e){errorReport(e);}
}
// Hash changes are the single navigation event source (no popstate listener).
function routeHash(route){
  const id=route.teacherId;
  if(route.page==='teacher') return `#${route.readOnly?'director/':''}teacher/${id}`;
  if(route.page==='payslip') return `#director/teacher/${id}/payslip`;
  if(route.page==='manage'){
    if(route.manageTab==='edit') return `#management/edit${route.editTeacherId?'/'+route.editTeacherId:''}`;
    if(route.manageTab==='status') return '#management/status';
    return '#management'; // Registration is already the management landing screen.
  }
  return {home:'#home',choose:'#teachers',director:'#director'}[route.page] || '#home';
}
function parseRoute(hash){
  const base={page:'home',teacherId:null,readOnly:false,manageTab:'register',editTeacherId:null};
  const simple={'#home':'home','#teachers':'choose','#director':'director','#management':'manage'};
  if(simple[hash]) return {...base,page:simple[hash]};
  let match=hash.match(/^#(director\/)?teacher\/([1-9]\d*)(\/payslip)?$/);
  if(match && (!match[3] || match[1]) && Number.isSafeInteger(Number(match[2])))
    return {...base,page:match[3]?'payslip':'teacher',teacherId:Number(match[2]),readOnly:!!match[1]};
  match=hash.match(/^#management\/(edit|status)(?:\/([1-9]\d*))?$/);
  if(match && (!match[2] || match[1]==='edit') && (!match[2] || Number.isSafeInteger(Number(match[2]))))
    return {...base,page:'manage',manageTab:match[1],editTeacherId:match[2]?Number(match[2]):null};
  return base;
}
async function goto(page, extra={}){
  const route={...state,page,...extra};
  const hash=routeHash(route);
  if(location.hash===hash) return;
  location.hash=hash;
}
function applyRoute(){
  const route=parseRoute(location.hash);
  const hash=routeHash(route);
  // Normalize invalid URLs in place, preserving the actual visit order.
  if(location.hash!==hash) history.replaceState(null,'',hash);
  Object.assign(state,route);
  if(state.page==='teacher' || state.page==='payslip') state.selectedDate = state.selectedDate.startsWith(state.month) ? state.selectedDate : `${state.month}-01`;
  return refresh();
}
let refreshVersion=0;
async function refresh(){
  const version=++refreshVersion;
  const route={...state};
  app.innerHTML='<div class="empty">데이터를 불러오는 중...</div>';
  try {
    const boot=await api('/api/bootstrap');
    if(version!==refreshVersion)return;
    state.boot=boot;
    state.pageData=null;
    document.getElementById('session-controls').innerHTML=boot.user?`<span>${escapeHtml(boot.user.name)} · ${personRole(boot.user.role)}</span> <button class="btn secondary small-btn" id="logout-button">로그아웃</button>`:'';
    document.getElementById('logout-button')?.addEventListener('click',logout);
    document.getElementById('header-academy').textContent=boot.settings.academy_name;
    const missingTeacher=(route.page==='teacher' || route.page==='payslip') && !boot.teachers.some(t=>t.id===route.teacherId && t.role==='teacher');
    const missingEdit=route.page==='manage' && route.editTeacherId && !boot.teachers.some(t=>t.id===route.editTeacherId);
    if(missingTeacher || missingEdit){
      const fallback=parseRoute(missingEdit?'#management/edit':route.readOnly?'#director':'#teachers');
      history.replaceState(null,'',routeHash(fallback));
      return applyRoute();
    }
    if(boot.setup_required || needsLogin(route,boot)){render();return;}
    let pageData=null;
    if(route.page==='teacher' || route.page==='payslip'){
      pageData=await api(`/api/teachers/${route.teacherId}/months/${route.month}`);
    } else if(route.page==='director') {
      pageData=await api(`/api/overview/${route.month}`);
    }
    if(version!==refreshVersion)return;
    state.boot=boot;
    state.pageData=pageData;
    document.getElementById('header-academy').textContent=boot.settings.academy_name;
    render();
  } catch(e){
    if(version!==refreshVersion)return;
    app.innerHTML=`<div class="card"><h2>화면을 불러올 수 없습니다</h2><p class="muted">${escapeHtml(e.message)}</p><button class="btn" data-action="home">홈으로 돌아가기</button></div>`;
    if(e.status===401 || e.status===403){state.pageData=null;return refresh();}
    errorReport(e);
  }
}
function render(){
  if(state.boot.setup_required){app.innerHTML=renderSetup();return;}
  if(needsLogin(state,state.boot)){app.innerHTML=renderLogin();return;}
  const pages={home:renderHome,choose:renderChoose,director:renderDirector,teacher:renderTeacher,manage:renderManage,payslip:renderPayslip};
  app.innerHTML=pages[state.page] ? pages[state.page]() : renderHome();
  if(state.page==='payslip') requestAnimationFrame(()=>paintPayslip(state.pageData,state.boot.settings));
}
function renderHome(){
  const academy=escapeHtml(state.boot?.settings.academy_name || '학원 급여 관리');
  return `<section class="hero"><button class="hero-logo" data-action="home" title="홈으로 돌아가기">${logoGraphic}</button>
    <p class="eyebrow">SIMPLE & LOCAL PAYROLL</p><h1>${academy}</h1>
    <p class="subtitle">선생님 근무시간부터 월별 예상 급여까지 한 곳에서 편리하게 관리하세요.</p></section>
    <section class="action-grid" aria-label="메인 메뉴">
      <button class="action-card" data-action="choose"><span class="action-icon">👩‍🏫</span><div class="action-title">선생님 <span class="action-arrow">↗</span></div><div class="action-desc">내 근무시간 입력 및 월별 급여 확인</div></button>
      <button class="action-card" data-action="director"><span class="action-icon">📋</span><div class="action-title">원장님 <span class="action-arrow">↗</span></div><div class="action-desc">선생님별 기록 조회 및 급여명세서</div></button>
      <button class="action-card" data-action="manage"><span class="action-icon">⚙️</span><div class="action-title">선생님 관리 <span class="action-arrow">↗</span></div><div class="action-desc">등록 · 정보 수정 · 비활성화 · 백업</div></button>
    </section><p class="muted small" style="text-align:center">본인 계정으로 로그인해 주세요. 원장님 조회·선생님 관리는 관리자 인증이 필요합니다.</p>`;
}
function personButton(t,action){
  return `<button class="person-row" data-action="${action}" data-id="${t.id}">
    <span class="person-avatar">${escapeHtml(t.name.slice(0,1))}</span>
    <span class="person-details"><strong>${escapeHtml(t.name)}</strong><span>${escapeHtml(t.login_id || '로그인 정보 미설정')}${t.amount ? ' · '+contractLabel(t):''}</span></span>
    <span class="chevron">›</span></button>`;
}
function renderChoose(){
  const teachers=state.boot.teachers.filter(t=>t.is_active && t.role==='teacher');
  return titleSection('선생님 선택','본인의 이름을 선택하면 출퇴근 기록 화면으로 이동합니다.')+
    `<div class="card" style="max-width:740px"><h2>등록된 선생님 <span class="muted small">${teachers.length}명</span></h2>
    <div class="person-list">${teachers.length ? teachers.map(t=>personButton(t,'open-teacher')).join('') : '<div class="empty"><strong>등록된 선생님이 없습니다.</strong>선생님 관리에서 신규 등록해 주세요.</div>'}</div></div>`;
}
function metrics(s){
  return `<div class="summary-grid"><article class="metric"><div class="label">이번 달 총 근무시간</div><div class="value">${HOURS(s.total_minutes)}</div><div class="helper">출근 ${s.work_days || 0}일 · 실제 기록 합계</div></article>
    <article class="metric featured"><div class="label">이번 달 예상 실수령액</div><div class="value">${KRW(s.net)}</div><div class="helper">${s.needs_review?'자동 산정 보류':'세전 급여에서 3.3% 공제'}</div></article>
    <article class="metric"><div class="label">세전 급여</div><div class="value">${KRW(s.gross)}</div><div class="helper">예상 공제액 ${KRW(s.withholding)}</div></article></div>`;
}
function calendarView(records){
  const [year,month]=state.month.split('-').map(Number);
  const first=new Date(year,month-1,1).getDay();
  const days=new Date(year,month,0).getDate();
  const count=Math.ceil((first+days)/7)*7;
  const worked=new Set(records.map(r=>r.work_date));
  const week=['일','월','화','수','목','금','토'];
  let grid=week.map(w=>`<div class="weekday">${w}</div>`).join('');
  for(let i=0;i<count;i++){
    const day=i-first+1;
    if(day<1 || day>days){grid+='<span class="calendar-day blank"></span>';continue;}
    const dt=`${state.month}-${pad(day)}`;
    const selected=dt===state.selectedDate;
    grid+=`<button class="calendar-day ${worked.has(dt)?'worked':''} ${selected?'selected':''}" data-action="pick-day" data-date="${dt}" aria-pressed="${selected}">${day}</button>`;
  }
  return `<div class="calendar-top"><h2>근무 달력</h2><div class="calendar-control"><button class="circle-btn" data-action="month-prev" aria-label="이전 달">◀</button>
    <span class="month-label">${monthLabel(state.month)}</span><button class="circle-btn" data-action="month-next" aria-label="다음 달">▶</button></div></div>
    <div class="calendar">${grid}</div><p class="calendar-guide"><i></i>근무기록이 있는 날짜</p>`;
}
function renderWorkForm(t, records){
  const selected=records.find(r=>r.work_date===state.selectedDate);
  if(!t.is_active) return '<div class="readonly-hint">비활성화된 선생님입니다. 관리 화면에서 재활성화한 뒤 기록을 수정할 수 있습니다.</div>';
  const workMin = selected?.minutes || 0;
  const applied = selected ? selected.applied_amount : t.amount;
  const payType=selected?.applied_pay_type || t.pay_type;
  const amount = Math.floor(workMin * applied / 60);
  return `<div class="date-banner"><strong>${fmtDay(state.selectedDate)} 기록</strong><span>${selected?'기존 기록 수정':'새 근무기록'}</span></div>
    <form id="work-form"><div class="form-pair"><div class="field"><label for="start-time">출근 시각</label><input id="start-time" name="start_time" type="time" step="60" value="${selected?.start_time || ''}" required></div>
    <div class="field"><label for="end-time">퇴근 시각</label><input id="end-time" name="end_time" type="time" step="60" value="${selected?.end_time || ''}" required></div></div>
    <div class="estimate"><span>근무시간 미리보기</span><strong id="work-preview">${selected?HOURS(workMin):'-'}</strong></div>
    <p class="muted small">${selected?'기존 기록의 적용 계약: '+PAY_LABEL[payType]+' '+KRW(applied):'현재 계약: '+contractLabel(t)} · 휴게시간 미포함 · 자정 넘김 불가</p>
    <div class="form-actions"><button type="submit" class="btn">${selected?'수정 내용 저장':'근무기록 저장'}</button>${selected?'<button type="button" class="btn danger" data-action="delete-record">기록 삭제</button>':''}</div></form>`;
}
function tableOfRecords(records,readonly){
  if(!records.length) return '<div class="empty">이 달에 등록된 근무기록이 없습니다.</div>';
  return `<div class="table-scroll"><table class="data-table"><thead><tr><th>근무일</th><th>출근</th><th>퇴근</th><th>근무시간</th><th>적용 계약</th>${readonly?'':'<th>수정</th>'}</tr></thead><tbody>`+
    records.map(r=>`<tr><td>${fmtDay(r.work_date)}</td><td>${r.start_time}</td><td>${r.end_time}</td><td>${HOURS(r.minutes)}</td><td>${PAY_LABEL[r.applied_pay_type]} ${KRW(r.applied_amount)}</td>${readonly?'':`<td><button class="btn small-btn secondary" data-action="edit-day" data-date="${r.work_date}">수정</button></td>`}</tr>`).join('')+
    '</tbody></table></div>';
}
function renderTeacher(){
  const {teacher:t,records,salary:s}=state.pageData;
  const readOnly=!!state.readOnly;
  return titleSection(`${t.name} ${readOnly?'근무기록 조회':'선생님 페이지'}`,
    `${monthLabel(state.month)} · ${personRole(t.role)}${readOnly?' · 읽기 전용':''}`,readOnly?'director':'choose')+
    `<p class="readonly-hint">현재 계약: ${contractLabel(t)}${s.needs_review?' · '+escapeHtml(s.review_reason):''}</p>`+metrics(s)+`<section class="layout-2"><div class="card">${calendarView(records)}</div>
    <div class="card"><h2>${readOnly?'선택한 날짜의 근무기록':'출퇴근 시각 입력'}</h2>
      ${readOnly?renderReadonlyDate(records):renderWorkForm(t,records)}</div></section>
    <section class="card" style="margin-top:19px"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
    <h2 style="margin:0 0 12px">월별 근무내역 <span class="muted small">${records.length}일</span></h2>
    ${readOnly?'<button class="btn secondary small-btn" data-action="payslip">급여명세서 보기 · PNG</button>':''}</div>${tableOfRecords(records,readOnly)}</section>`;
}
function renderReadonlyDate(records){
  const r=records.find(v=>v.work_date===state.selectedDate);
  return `<div class="date-banner"><strong>${fmtDay(state.selectedDate)}</strong><span>읽기 전용</span></div>`+
    (r? `<div class="readonly-hint"><strong>${r.start_time} ~ ${r.end_time}</strong><div style="margin-top:8px">근무시간 ${HOURS(r.minutes)} · 적용 시급 ${PAY_LABEL[r.applied_pay_type]} ${KRW(r.applied_amount)}</div></div>`:
      '<div class="readonly-hint">이 날짜에는 근무기록이 없습니다.</div>')+
    '<p class="muted small" style="margin-top:15px">원장님 조회 화면에서는 근무기록을 변경할 수 없습니다.</p>';
}
function renderDirector(){
  const {items,totals}=state.pageData;
  return titleSection('원장님 관리 화면','전체 선생님의 월별 근무 현황을 조회하고 급여명세서를 출력합니다.')+
    `<div class="action-strip"><button class="circle-btn" data-action="month-prev">◀</button><strong>${monthLabel(state.month)}</strong><button class="circle-btn" data-action="month-next">▶</button></div>`+
    (state.pageData.needs_review?'<div class="warn-banner">정산 확인이 필요한 선생님이 있습니다. 합계는 산정 가능한 금액만 포함합니다.</div>':'')+metrics(totals)+`<section class="card"><h2>선생님별 월간 현황</h2><div class="person-list">`+
    (items.length?items.map(({teacher:t,salary:s})=>`<button class="person-row" data-action="open-director-teacher" data-id="${t.id}">
       <span class="person-avatar">${escapeHtml(t.name.slice(0,1))}</span>
       <span class="person-details"><strong>${escapeHtml(t.name)} ${t.is_active?'':'<span class="pill off">비활성화</span>'}</strong>
       <span>${contractLabel(t)} · ${s.work_days}일 · ${HOURS(s.total_minutes)} · 세전 ${KRW(s.gross)} · 예상 실수령액 ${KRW(s.net)}</span></span><span class="chevron">›</span></button>`).join(''):
      '<div class="empty">등록된 선생님이 없습니다.</div>')+'</div></section>';
}
function teacherFields(t){
  const editing=!!t;
  t=t || {name:'',phone:'',role:'teacher',login_id:'',pay_type:'hourly',amount:16000};
  const latest=t.latest_contract;
  const payType=latest?.pay_type || t.pay_type;
  const amount=latest?.amount || t.amount;
  return `<div class="form-pair"><div class="field"><label for="teacher-name">이름 *</label><input id="teacher-name" name="name" required maxlength="80" value="${escapeHtml(t.name)}"></div>
    <div class="field"><label for="teacher-role">역할</label><select id="teacher-role" name="role" ${editing?'disabled':''}><option value="teacher" ${t.role==='teacher'?'selected':''}>일반 선생님</option><option value="director" ${t.role==='director'?'selected':''}>원장님</option></select>${editing?`<input type="hidden" name="role" value="${t.role}">`:''}</div></div>
    <div class="form-pair"><div class="field"><label for="teacher-login">로그인 아이디 *</label><input id="teacher-login" name="login_id" required pattern="[A-Za-z0-9_.-]{3,80}" maxlength="80" autocomplete="off" value="${escapeHtml(t.login_id || '')}"></div>
    <div class="field"><label for="teacher-password">${editing && t.credentials_set?'새 비밀번호 (숫자 4자리, 변경 시에만 입력)':'비밀번호 (숫자 4자리) *'}</label><input id="teacher-password" name="password" type="password" inputmode="numeric" pattern="[0-9]{4}" minlength="4" maxlength="4" title="숫자 4자리로 입력해 주세요." autocomplete="new-password" ${!editing || !t.credentials_set?'required':''}></div></div>
    <div id="teacher-pay-fields" ${t.role==='director'?'hidden':''}>
    <div class="field"><label for="teacher-phone">전화번호</label><input id="teacher-phone" name="phone" maxlength="40" value="${escapeHtml(t.phone)}"></div>
    <div class="form-pair"><div class="field"><label for="teacher-pay-type">급여 계약 유형</label><select id="teacher-pay-type" name="pay_type">${Object.entries(PAY_LABEL).filter(([k])=>k!=='mixed').map(([k,v])=>`<option value="${k}" ${payType===k?'selected':''}>${v}제</option>`).join('')}</select></div>
    <div class="field"><label id="teacher-amount-label" for="teacher-amount">${PAY_LABEL[payType]} (원/${payType==='daily'?'근무일':payType==='monthly'?'월':'시간'}) *</label><input id="teacher-amount" name="amount" type="number" min="1" max="1000000000" step="1" value="${amount || 16000}" ${t.role==='teacher'?'required':''}></div></div>
    <div class="field"><label for="contract-date">${editing?'새 계약 적용 시작일 (계약 변경 시)':'계약 적용 시작일'}</label><input id="contract-date" name="effective_date" type="date" value="${defaultDay}"></div>
    <p class="muted small">계약 이력과 과거 근무일의 적용 금액은 보존됩니다. 월중 유형 변경 및 월급제 중도 변경은 정산 확인이 필요합니다.</p>
    ${t.contract_history?.length?`<details><summary>급여 계약 이력</summary>${t.contract_history.map(c=>`<p class="small">${c.effective_date} · ${PAY_LABEL[c.pay_type]} ${KRW(c.amount)}</p>`).join('')}</details>`:''}</div>`;
}

function renderManage(){
  const teachers=state.boot.teachers;
  const tab=state.manageTab;
  let content='';
  if(tab==='register'){
    content=`<div class="card"><h2>신규 선생님 등록</h2><form id="create-teacher-form">${teacherFields(null)}<button type="submit" class="btn">선생님 등록</button></form></div>`;
  }else if(tab==='edit'){
    const editTeacher=teachers.find(t=>t.id===state.editTeacherId);
    content=`<div class="card"><h2>수정할 선생님 선택</h2><div class="person-list">${teachers.map(t=>personButton(t,'manage-edit-teacher')).join('')||'<div class="empty">선생님이 없습니다.</div>'}</div></div>`+
      (editTeacher?`<div class="card" style="margin-top:18px"><h2>${escapeHtml(editTeacher.name)} 정보 수정</h2><form id="edit-teacher-form">${teacherFields(editTeacher)}<p class="muted small">계약 변경은 입력한 적용 시작일부터 반영됩니다. 과거 기록과 계약 이력은 보존됩니다.</p><button type="submit" class="btn">변경사항 저장</button></form>${editTeacher.role==='teacher'?`<hr><h2>월별 정산 확인</h2><form id="review-form"><div class="form-pair"><div class="field"><label>정산 월<input name="month" type="month" value="${state.month}" required></label></div><div class="field"><label>확인 사유 (비우면 수동 표시 해제)<input name="reason" maxlength="200" value="${escapeHtml(editTeacher.review_flags?.[state.month] || '')}" placeholder="결근 등 별도 정산 필요 사유"></label></div></div><button class="btn secondary" type="submit">정산 확인 상태 저장</button></form>`:''}</div>`:'');
  }else{
    const active=teachers.filter(t=>t.is_active), inactive=teachers.filter(t=>!t.is_active);
    content=`<div class="card"><h2>선생님 비활성화</h2><p class="muted small">퇴사자를 체크한 뒤 비활성화하세요. 근무기록은 삭제하지 않습니다.</p>
      <div class="person-list">${active.map(t=>`<label class="manage-row"><span style="display:flex;align-items:center;gap:13px"><input type="checkbox" class="deactivate-check" value="${t.id}" style="width:19px;height:19px;accent-color:var(--sea)">
      <span><strong>${escapeHtml(t.name)}</strong><span class="note" style="display:block">${personRole(t.role)} · ${contractLabel(t)}</span></span></span><span class="pill">재직</span></label>`).join('')||'<div class="empty">현재 재직 중인 선생님이 없습니다.</div>'}</div>
      <button class="btn danger" style="margin-top:17px" data-action="bulk-deactivate" ${active.length?'':'disabled'}>선택한 선생님 비활성화</button></div>
      <div class="card" style="margin-top:18px"><h2>비활성화된 선생님</h2>
      ${inactive.map(t=>`<div class="manage-row"><div><strong>${escapeHtml(t.name)}</strong><div class="note">${personRole(t.role)}</div></div>
      <button class="btn small-btn secondary" data-action="toggle-teacher" data-id="${t.id}">재활성화</button></div>`).join('')||'<div class="empty">비활성화된 선생님이 없습니다.</div>'}</div>`;
  }
  return titleSection('선생님 관리','신규 등록, 정보 수정, 비활성화와 데이터 백업을 관리합니다.')+
    `<div class="tabs"><button class="tab ${tab==='register'?'active':''}" data-action="tab-register">신규 등록</button><button class="tab ${tab==='edit'?'active':''}" data-action="tab-edit">정보 수정</button><button class="tab ${tab==='status'?'active':''}" data-action="tab-status">비활성화·복구</button></div>`+
    `<div class="stack">${content}<div class="card"><h2>학원 정보</h2><form id="settings-form"><div class="form-pair"><div class="field"><label for="academy-name">학원 이름</label><input id="academy-name" name="academy_name" maxlength="100" required value="${escapeHtml(state.boot.settings.academy_name)}"></div>
    <div class="field"><label for="principal-name">급여명세서 하단 발행인 (선택)</label><input id="principal-name" name="principal_name" maxlength="80" value="${escapeHtml(state.boot.settings.principal_name)}" placeholder="원장 이름"></div></div><button class="btn secondary" type="submit">학원 정보 저장</button></form></div>
    <div class="card"><h2>엑셀 백업 · 복원</h2><p class="muted small">등록/수정 시 서버의 data/backups 폴더에 Excel과 SQLite 백업이 자동 생성됩니다. 엑셀은 복원 가능한 원본 양식이며, 월별급여 시트는 조회용입니다.</p>
      <div class="action-strip"><a class="btn secondary" href="/api/backup/excel" download>엑셀 백업 다운로드</a><button class="btn danger" data-action="restore-pick">엑셀 백업 복원</button></div>
      <input id="restore-file" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden>
      <div class="warn-banner">복원은 현재 선생님·근무기록을 파일 내용으로 전체 교체합니다. 복원 직전 원본 SQLite 안전 백업을 만들며, 별도 USB/클라우드에도 정기적으로 백업하세요.</div>
    </div></div>`;
}
function renderPayslip(){
  const {teacher:t}=state.pageData;
  return titleSection(`${t.name} 급여명세서`,`${monthLabel(state.month)} · 디지털 급여명세서 미리보기`,'back-teacher')+
    `<div class="sheet-actions"><button class="btn" data-action="download-png">PNG 이미지 다운로드</button><button class="btn secondary" data-action="back-teacher">근무내역으로 돌아가기</button></div>
    <div class="sheet-preview"><canvas id="payslip-canvas" role="img" aria-label="${escapeHtml(t.name)} ${monthLabel(state.month)} 급여명세서"></canvas></div>
    <p class="muted small" style="text-align:center">양식은 참고 사진의 디지털 버전입니다. 근무 날짜만 표시하며 시간 상세내역은 개인 기록 화면에서 확인할 수 있습니다.</p>`;
}

function paintPayslip(payload,settings){
  const canvas=document.getElementById('payslip-canvas');
  if(!canvas)return;
  canvas.width=1800;canvas.height=2500;
  const ctx=canvas.getContext('2d');
  ctx.scale(2,2);
  const W=900,H=1250;
  const ink='#203b36',teal='#225e54',muted='#71837c',line='#d7e1db',light='#eff6f1';
  function text(s,x,y,size=20,color=ink,weight='400',align='left'){
    ctx.fillStyle=color;ctx.font=`${weight} ${size}px "Apple SD Gothic Neo","Malgun Gothic", sans-serif`;
    ctx.textAlign=align;ctx.textBaseline='middle';ctx.fillText(String(s),x,y);
  }
  function rect(x,y,w,h,fill,stroke=null){ctx.fillStyle=fill;ctx.fillRect(x,y,w,h);if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=1;ctx.strokeRect(x,y,w,h);}}
  function stroke(x1,y1,x2,y2,c=line,width=1){ctx.beginPath();ctx.strokeStyle=c;ctx.lineWidth=width;ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();}
  rect(0,0,W,H,'#fff');rect(0,0,W,15,teal);
  text(settings.academy_name,75,75,24,teal,'700');
  text('MONTHLY PAY STATEMENT',825,76,14,muted,'500','right');
  stroke(75,113,825,113,line,2);
  text(`${monthLabel(payload.month)} 급여명세서`,450,178,34,ink,'800','center');
  text(`이름   ${payload.teacher.name}`,450,224,21,ink,'650','center');
  rect(75,275,750,54,light);
  text('월별 근무 현황',95,303,22,teal,'750');
  text('● 근무일',805,303,16,teal,'500','right');
  const x0=75,y0=342,cw=750/7,rh=67;
  const week=['일','월','화','수','목','금','토'];
  week.forEach((w,i)=>{rect(x0+i*cw,y0,cw,42,'#f5f8f5',line);text(w,x0+i*cw+cw/2,y0+21,17,i===0?'#b46966':muted,'650','center');});
  const [year,month]=payload.month.split('-').map(Number);
  const offset=new Date(year,month-1,1).getDay(), count=new Date(year,month,0).getDate();
  const worked=new Set(payload.records.map(r=>r.work_date));
  for(let row=0;row<6;row++)for(let col=0;col<7;col++){
    const x=x0+col*cw,y=y0+42+row*rh,day=row*7+col-offset+1;
    rect(x,y,cw,rh,'#fff',line);
    if(day>0&&day<=count){
      const isWorked=worked.has(`${payload.month}-${pad(day)}`);
      if(isWorked){ctx.fillStyle='#e5f2eb';ctx.beginPath();ctx.arc(x+cw/2,y+rh/2,23,0,Math.PI*2);ctx.fill();}
      text(day,x+cw/2,y+rh/2,20,isWorked?teal:ink,isWorked?'800':'450','center');
      if(isWorked){ctx.fillStyle=teal;ctx.beginPath();ctx.arc(x+cw/2+25,y+17,4.5,0,Math.PI*2);ctx.fill();}
    }
  }
  const sy=825;
  rect(75,sy,750,54,light);
  text('급여 정산 내역',96,sy+28,22,teal,'750');
  const s=payload.salary;
  const pairs=[
    [`${PAY_LABEL[s.pay_type]}제 계약`,s.needs_review?'정산 확인 필요':s.contract_amount_label || KRW(s.amount)],
    ['총 근무시간 / 출근일수',`${HOURS(s.total_minutes)} / ${s.work_days}일`],
    ['세전 급여',KRW(s.gross)],
    ['원천징수세액 (3.3%)',s.withholding===null?'정산 확인 필요':`− ${KRW(s.withholding)}`],
    ['예상 지급액',KRW(s.net)],
  ];
  pairs.forEach(([a,b],i)=>{
    const y=sy+54+i*54;
    rect(75,y,750,54,i===4?'#eaf4ed':'#fff',line);
    stroke(325,y,325,y+54,line);
    text(a,95,y+27,19,i===4?teal:ink,i===4?'750':'500');
    text(b,804,y+27,22,i===4?teal:ink,i===4?'800':'550','right');
  });
  text(`발행: ${settings.academy_name}${settings.principal_name?' · '+settings.principal_name:''}`,450,1184,21,ink,'700','center');
  text(s.needs_review?'정산 확인 필요 · 자동 산정 보류':'근무기록 및 계약 기준 예상 급여 · 3.3% 공제 적용 가정',450,1218,14,muted,'400','center');
}

app.addEventListener('click', async ev=>{
  const target=ev.target.closest('[data-action]');
  if(!target)return;
  const action=target.dataset.action;
  const id=Number(target.dataset.id);
  try {
    if(action==='home') await goto('home');
    else if(action==='choose') await goto('choose');
    else if(action==='director') await goto('director');
    else if(action==='manage') await goto('manage',{manageTab:'register',editTeacherId:null});
    else if(action==='open-teacher') await goto('teacher',{teacherId:id,readOnly:false});
    else if(action==='open-director-teacher') await goto('teacher',{teacherId:id,readOnly:true});
    else if(action==='month-prev') changeMonth(-1);
    else if(action==='month-next') changeMonth(1);
    else if(action==='pick-day'){state.selectedDate=target.dataset.date;render();}
    else if(action==='edit-day'){
      state.selectedDate=target.dataset.date;render();
      document.getElementById('work-form')?.scrollIntoView({behavior:'smooth',block:'center'});
    }
    else if(action==='delete-record'){
      if(!confirm(`${state.selectedDate} 근무기록을 삭제할까요?`))return;
      const res=await api(`/api/teachers/${state.teacherId}/records/${state.selectedDate}`,{method:'DELETE'});
      await refresh();backupToast(res);
    }
    else if(action==='payslip') await goto('payslip');
    else if(action==='back-teacher') await goto('teacher');
    else if(action==='download-png'){
      const canvas=document.getElementById('payslip-canvas');
      if(!canvas)throw new Error('급여명세서가 준비되지 않았습니다.');
      canvas.toBlob(blob=>{
        if(!blob){toast('이미지를 생성하지 못했습니다.',true);return;}
        const safeName=state.pageData.teacher.name.replace(/[\\/:*?"<>|]/g,'_');
        const href=URL.createObjectURL(blob);
        const a=document.createElement('a');a.href=href;a.download=`${state.month}_${safeName}_급여명세서.png`;
        document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(href),1500);
        toast('PNG 급여명세서를 다운로드했습니다.');
      },'image/png');
    }
    else if(action.startsWith('tab-')) await goto('manage',{manageTab:action.slice(4),editTeacherId:null});
    else if(action==='manage-edit-teacher') await goto('manage',{manageTab:'edit',editTeacherId:id});
    else if(action==='bulk-deactivate'){
      const checked=[...document.querySelectorAll('.deactivate-check:checked')].map(x=>Number(x.value));
      if(!checked.length){toast('비활성화할 선생님을 먼저 선택해 주세요.',true);return;}
      if(!confirm(`선택한 ${checked.length}명의 선생님을 정말로 비활성화할까요? 과거 기록은 보존됩니다.`))return;
      const results=[];
      for(const id of checked)results.push(await api(`/api/teachers/${id}/status?active=false`,{method:'PATCH'}));
      await refresh();
      const failed=results.find(x=>x.backup&&!x.backup.ok);
      if(failed)toast(failed.backup.message,true);
      else toast(`${checked.length}명의 선생님을 비활성화했습니다.`);
    }
    else if(action==='toggle-teacher'){
      const t=state.boot.teachers.find(t=>t.id===id);
      if(!t)return;
      if(!confirm(t.is_active?`${t.name} 선생님을 비활성화할까요? 과거 근무기록은 보존됩니다.`:`${t.name} 선생님을 다시 활성화할까요?`))return;
      const res=await api(`/api/teachers/${id}/status?active=${t.is_active?'false':'true'}`,{method:'PATCH'});
      await refresh();backupToast(res);
    }
    else if(action==='restore-pick')document.getElementById('restore-file')?.click();
  }catch(e){errorReport(e);}
});
// Reject an invalid insertion as a whole; never turn a pasted password into another PIN.
function rejectPinInsertion(input, text){
  const start=input.selectionStart ?? input.value.length;
  const end=input.selectionEnd ?? start;
  return !/^[0-9]*$/.test(text) || input.value.length-(end-start)+text.length>4;
}
app.addEventListener('beforeinput',ev=>{
  if(ev.target.name==='password' && ev.inputType?.startsWith('insert') && ev.data!==null && rejectPinInsertion(ev.target,ev.data)){
    ev.preventDefault();toast('비밀번호는 숫자 4자리만 입력할 수 있습니다.',true);
  }
});
app.addEventListener('paste',ev=>{
  if(ev.target.name==='password' && rejectPinInsertion(ev.target,ev.clipboardData?.getData('text') || '')){
    ev.preventDefault();toast('비밀번호는 숫자 4자리만 붙여넣을 수 있습니다.',true);
  }
});
app.addEventListener('invalid',ev=>{
  if(ev.target.name==='password')ev.target.setCustomValidity('비밀번호는 숫자 4자리로 입력해 주세요.');
},true);
app.addEventListener('input',ev=>{
  if(ev.target.name==='password'){
    // Fallback for autofill, composition and browsers without cancellable beforeinput.
    if(!/^[0-9]{0,4}$/.test(ev.target.value)){
      ev.target.value='';toast('비밀번호는 숫자 4자리만 입력할 수 있습니다.',true);
    }
    ev.target.setCustomValidity(ev.target.value && !/^[0-9]{4}$/.test(ev.target.value)?'비밀번호는 숫자 4자리로 입력해 주세요.':'');
    return;
  }
  if(!['start-time','end-time'].includes(ev.target.id))return;
  const a=document.getElementById('start-time')?.value,b=document.getElementById('end-time')?.value;
  const view=document.getElementById('work-preview');if(!view)return;
  if(!/^\d\d:\d\d$/.test(a||'')||!/^\d\d:\d\d$/.test(b||'')){view.textContent='-';return;}
  const [ah,am]=a.split(':').map(Number),[bh,bm]=b.split(':').map(Number);
  const minutes=(bh*60+bm)-(ah*60+am);
  view.textContent=minutes>0?HOURS(minutes):'퇴근 시각 확인 필요';
});
app.addEventListener('submit',async ev=>{
  ev.preventDefault();const form=ev.target;const fd=new FormData(form);
  const submit=form.querySelector('[type="submit"]');if(submit)submit.disabled=true;
  try {
    if(form.id==='login-form'){
      const payload={password:fd.get('password'),role:fd.get('teacher_id')?'teacher':'director'};
      if(fd.get('teacher_id'))payload.teacher_id=Number(fd.get('teacher_id'));
      else payload.login_id=fd.get('login_id');
      await api('/api/auth/login',json('POST',payload));
      await refresh();
    } else if(form.id==='setup-form'){
      await api('/api/auth/setup',json('POST',{name:fd.get('name'),login_id:fd.get('login_id'),password:fd.get('password'),role:'director'}));
      await refresh();
    } else if(form.id==='work-form'){
      const body={start_time:fd.get('start_time'),end_time:fd.get('end_time')};
      const res=await api(`/api/teachers/${state.teacherId}/records/${state.selectedDate}`,json('PUT',body));
      await refresh();backupToast(res);
    } else if(form.id==='create-teacher-form'||form.id==='edit-teacher-form'){
      const payload={name:fd.get('name'),phone:fd.get('phone') || '',role:fd.get('role'),login_id:fd.get('login_id'),password:fd.get('password') || null,pay_type:fd.get('role')==='director'?'hourly':fd.get('pay_type'),amount:fd.get('role')==='director'?1:Number(fd.get('amount')),effective_date:fd.get('effective_date') || defaultDay};
      const editing=form.id==='edit-teacher-form';
      const res=await api(editing?`/api/teachers/${state.editTeacherId}`:'/api/teachers',json(editing?'PUT':'POST',payload));
      await refresh();backupToast(res);
    } else if(form.id==='review-form'){
      const res=await api(`/api/teachers/${state.editTeacherId}/review/${fd.get('month')}`,json('PUT',{reason:fd.get('reason')}));
      await refresh();backupToast(res);
    } else if(form.id==='settings-form'){
      const res=await api('/api/settings',json('PUT',{academy_name:fd.get('academy_name'),principal_name:fd.get('principal_name')}));
      await refresh();backupToast(res);
    }
  }catch(e){errorReport(e);}finally{if(submit && submit.isConnected)submit.disabled=false;}
});
app.addEventListener('change',async ev=>{
  if(ev.target.name==='month' && ev.target.form?.id==='review-form'){
    const teacher=state.boot.teachers.find(t=>t.id===state.editTeacherId);
    ev.target.form.elements.reason.value=teacher?.review_flags?.[ev.target.value] || '';return;
  }
  if(ev.target.id==='teacher-role'){
    const director=ev.target.value==='director';
    document.getElementById('teacher-pay-fields').hidden=director;
    document.getElementById('teacher-amount').required=!director;
    return;
  }
  if(ev.target.id==='teacher-pay-type'){
    const type=ev.target.value;
    document.getElementById('teacher-amount-label').textContent=`${PAY_LABEL[type]} (원/${type==='daily'?'근무일':type==='monthly'?'월':'시간'}) *`;
    return;
  }
  if(ev.target.id!=='restore-file')return;
  const file=ev.target.files?.[0];if(!file)return;
  ev.target.value='';
  if(!confirm(`'${file.name}'로 현재 전체 데이터를 교체할까요? 복원 직전 SQLite 안전 백업을 생성합니다.`))return;
  try {
    const formData=new FormData();formData.append('file',file);
    const result=await api('/api/backup/restore',{method:'POST',body:formData});
    await refresh();
    if(result.backup && !result.backup.ok)toast(result.backup.message,true);
    else toast(`복원 완료 · 선생님 ${result.restored_teachers}명 / 근무기록 ${result.restored_records}건`);
  }catch(e){errorReport(e);}
});
document.querySelector('.topbar .brand').addEventListener('click',()=>goto('home'));
window.addEventListener('hashchange',applyRoute);
window.addEventListener('pageshow',ev=>{if(ev.persisted)refresh();});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refresh();});
applyRoute();
