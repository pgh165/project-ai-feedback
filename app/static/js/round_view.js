// 프로젝트 상세 화면 상단 상태 줄 — /api/rounds/<id>/status 폴링.
// 진행 중이면 2초마다 진행률·설명을 고치고, 상태가 바뀌면(장면 완성·러프컷 끝 등) 화면을 새로 고친다.
// 생성 중지도 여기서 한다.
(function () {
  const root = document.getElementById('round-bar');
  if (!root) return;
  const base = root.dataset.apiBase;
  const isCurrent = root.dataset.current === '1';
  const isDev = root.dataset.dev === '1';
  const detailEl = document.getElementById('round-detail');
  const progressEl = document.getElementById('round-progress');
  const badgeEl = document.getElementById('round-badges');
  const cancelBtn = document.getElementById('round-cancel');
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
    else if (data.auto && data.auto.planning && data.auto.message) detailEl.textContent = data.auto.message;  // AI 기획 중
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
  }

  async function refresh() {
    try {
      data = await (await fetch(root.dataset.statusUrl)).json();
    } catch (e) { schedule(10000); return; }
    const planning = !!(data.auto && data.auto.planning);
    if (isCurrent && (data.state !== root.dataset.state ||
                      (data.rough_running ? '1' : '0') !== root.dataset.rough ||
                      (root.dataset.planning === '1' && !planning))) {  // AI 기획이 끝나거나 실패하면 새로 고침
      // 단계가 바뀌었으면 주소의 탭 표시(#produce 등)를 지워 지금 단계 탭이 열리게 한다 — 제작이 끝나면 ③ 확인으로 (2026-11-07)
      if (data.state !== root.dataset.state) {
        try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
      }
      location.reload();
      return;
    }
    renderBar();
    const running = planning || data.steps.some(s => s.status === 'active');
    if (!isCurrent || data.state === 'prompt_updated') return;
    schedule(running ? 2000 : 10000);
  }
  function schedule(ms) { clearTimeout(timer); timer = setTimeout(refresh, ms); }
  refresh();

  // 공개 뒤 댓글 자동 수집 — 1분마다 새 댓글을 모으고, 묶음이 나오면 반영할 묶음을 고르는 검토 화면으로 (2026-11-08)
  const auto = document.getElementById('auto-collect');
  if (auto && isCurrent) {
    const tick = async () => {
      try {
        const res = await fetch(auto.dataset.url, {method: 'POST'});
        const j = await res.json();
        if (j.state === 'pending_review' && j.review_url) { location.href = j.review_url; return; }
        if (j.state !== root.dataset.state) { location.reload(); return; }
      } catch (e) { /* 다음 차례에 다시 */ }
      setTimeout(tick, 60000);
    };
    tick();
  }
})();
