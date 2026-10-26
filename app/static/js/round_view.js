// 프로젝트 상세 화면 상단 상태 줄 — /api/rounds/<id>/status 폴링.
// 진행 중이면 2초마다 진행률·설명을 고치고, 상태가 바뀌면(장면 완성·러프컷 끝 등) 화면을 새로 고친다.
// 생성 중지, 품질 부족 장면을 유료 API로 다시 만들기(사람이 비용을 확인하고 누를 때만)도 여기서 한다.
(function () {
  const root = document.getElementById('round-bar');
  if (!root) return;
  const base = root.dataset.apiBase;
  const isCurrent = root.dataset.current === '1';
  const isDev = root.dataset.dev === '1';
  const detailEl = document.getElementById('round-detail');
  const progressEl = document.getElementById('round-progress');
  const badgeEl = document.getElementById('round-badges');
  const escalateEl = document.getElementById('escalate-box');
  const cancelBtn = document.getElementById('round-cancel');
  const picked = new Set();
  const flags = {};
  let data = null;
  let timer = null;

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') node.className = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c == null) continue;
      node.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  async function post(path, body) {
    const res = await fetch(base + path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify(body || {})});
    let json = {};
    try { json = await res.json(); } catch (e) { /* 본문 없음 */ }
    return {ok: res.ok, json};
  }
  const money = v => '$' + Number(v).toFixed(2);

  if (cancelBtn) {
    cancelBtn.addEventListener('click', async () => {
      if (!confirm('생성을 중지할까요? 지금 만드는 장면은 버려지고, 이미 만들어진 장면은 남습니다.')) return;
      cancelBtn.disabled = true;
      cancelBtn.textContent = '중지하는 중…';
      await post('/cancel');
      refresh();
    });
  }

  function renderBar() {
    const active = data.steps.find(s => s.status === 'active');
    if (active && active.detail) detailEl.textContent = active.detail;
    if (active && active.progress != null) {
      progressEl.hidden = false;
      progressEl.firstElementChild.style.width = Math.round(active.progress * 100) + '%';
    } else {
      progressEl.hidden = true;
    }
    badgeEl.replaceChildren();
    if (data.fallback_to_mock && isDev) {
      badgeEl.append(el('span', {class: 'badge text-bg-warning', title: data.fallback_detail}, 'mock으로 대체됨'));
    }
    if (data.commercial_spent_usd > 0) {
      badgeEl.append(el('span', {class: 'badge text-bg-info'},
        `상용 API ${money(data.commercial_spent_usd)} / ${money(data.commercial_budget_usd)}`));
    }
  }

  // 품질이 부족한 장면을 유료 API로 다시 만들기 — 키가 있고, 올릴 영상 확인 단계일 때만
  function renderEscalate() {
    if (!escalateEl) return;
    escalateEl.replaceChildren();
    if (!(isCurrent && data.state === 'video_ready' && data.commercial.available)) return;
    const box = el('details', {class: 'stage-card escalate'}, el('summary', {}, '품질이 부족한 장면을 유료 API로 다시 만들기'));
    data.shots.forEach(s => {
      const row = el('label', {class: 'escalate-row'},
        el('input', {type: 'checkbox', class: 'form-check-input', checked: picked.has(s.id),
          onchange: e => { e.target.checked ? picked.add(s.id) : picked.delete(s.id); renderEscalate(); }}),
        el('strong', {}, s.id), el('span', {class: 'text-muted'}, `${s.start.toFixed(0)}–${s.end.toFixed(0)}초`),
        s.recommend_api ? el('span', {class: 'badge text-bg-warning'}, 'API 권장 (재발)') : null,
        el('span', {class: 'shot-prompt'}, s.prompt_ko || s.prompt));
      if (picked.has(s.id)) {
        row.append(el('select', {class: 'form-select form-select-sm', onchange: e => { flags[s.id] = e.target.value ? [e.target.value] : []; }},
          el('option', {value: ''}, '품질 부족 사유…'),
          Object.entries(data.commercial.reasons).map(([k, label]) =>
            el('option', {value: k, selected: (flags[s.id] || [])[0] === k}, label))));
      }
      box.append(row);
    });
    const cost = data.shots.filter(s => picked.has(s.id)).reduce((a, s) => a + s.estimate_api_cost_usd, 0);
    box.append(el('button', {class: 'btn btn-outline-primary btn-sm mt-2', disabled: picked.size === 0,
      onclick: async () => {
        const ids = [...picked];
        if (!confirm(`선택한 ${ids.length}개 장면을 상용 API로 재생성합니다.\n예상 비용 ${money(cost)} (남은 예산 ${money(data.commercial_remaining_usd)})`)) return;
        const r = await post('/escalate', {shot_ids: ids, flags, confirm_cost_usd: Number(cost.toFixed(4))});
        if (!r.ok) alert(r.json.error || '재생성 요청 실패');
        picked.clear();
        refresh();
      }}, `고른 장면 다시 만들기 (예상 ${money(cost)}, 남은 예산 ${money(data.commercial_remaining_usd)})`));
    box.open = picked.size > 0;
    escalateEl.append(box);
  }

  async function refresh() {
    try {
      data = await (await fetch(root.dataset.statusUrl)).json();
    } catch (e) { schedule(10000); return; }
    if (isCurrent && (data.state !== root.dataset.state ||
                      (data.rough_running ? '1' : '0') !== root.dataset.rough)) {
      location.reload();
      return;
    }
    renderBar();
    renderEscalate();
    const running = data.steps.some(s => s.status === 'active');
    if (!isCurrent || data.state === 'prompt_updated') return;
    schedule(running ? 2000 : 10000);
  }
  function schedule(ms) { clearTimeout(timer); timer = setTimeout(refresh, ms); }
  refresh();
})();
