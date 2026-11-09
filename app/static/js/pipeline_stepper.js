// 9단계 파이프라인 스테퍼 — /api/rounds/<id>/status 폴링 (2초, 대기 중 10초, 반영 완료 시 중지)
(function () {
  const root = document.getElementById('pipeline-stepper');
  if (!root) return;

  const statusUrl = root.dataset.statusUrl;
  const base = root.dataset.apiBase;           // /api/rounds/<id>
  const reviewUrl = root.dataset.reviewUrl;
  const isCurrent = root.dataset.current === '1';
  const stepsEl = root.querySelector('.pipeline-steps');
  const panelEl = root.querySelector('.pipeline-panel');
  const badgeEl = root.querySelector('.pipeline-badges');

  let data = null;
  let openStep = null;          // 사용자가 연 단계 번호
  let timer = null;
  let lastSignature = '';
  const picked = new Set();     // 상용 재생성 대상 장면 (재렌더 중 유지)
  const flags = {};             // 장면별 사유

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
    const res = await fetch(base + path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
    });
    let json = {};
    try { json = await res.json(); } catch (e) { /* 본문 없음 */ }
    return {ok: res.ok, status: res.status, json};
  }

  function money(v) { return '$' + Number(v).toFixed(2); }

  // ── 상단 단계 막대 ──
  // 9단계를 사용자가 이해하는 세 묶음(만들기 / 반응 모으기 / 반영하기)으로 보여 준다.
  // 서버 상태 키는 그대로 쓰고, 화면 이름만 바꾼다.
  const isDev = root.dataset.dev === '1';
  const STEP_NAME = {
    prompt_built: '기획', generating: '제작', video_ready: '확인',
    uploaded: '공개', feedback_collected: '댓글 모음',
    feedback_classified: '의견 분류', clustering: '의견 묶기', pending_review: '검토', prompt_updated: '다음 버전',
  };
  const PHASES = [
    {name: '만들기', keys: ['prompt_built', 'generating', 'video_ready']},
    {name: '반응 모으기', keys: ['uploaded', 'feedback_collected']},
    {name: '반영하기', keys: ['feedback_classified', 'clustering', 'pending_review', 'prompt_updated']},
  ];
  const stepName = s => STEP_NAME[s.key] || s.label;

  function stepButton(s) {
    const circle = el('span', {class: 'step-circle', 'aria-hidden': 'true'},
      s.status === 'done' ? '✓' : s.status === 'failed' ? '!' : s.no);
    const stateText = {done: '완료', active: '진행 중', waiting: '확인 필요', failed: '실패', pending: '대기'}[s.status] || '';
    const item = el('button', {
      type: 'button', class: `pipe-step ${s.status}` + (openStep === s.no ? ' open' : ''),
      title: s.error || '', 'aria-expanded': openStep === s.no ? 'true' : 'false',
      'aria-label': `${s.no}단계 ${stepName(s)}, ${stateText}`,
      onclick: () => { openStep = openStep === s.no ? null : s.no; renderAll(); },
    }, circle, el('span', {class: 'step-label'}, stepName(s)));
    if (s.status === 'active' && s.progress != null) {
      item.append(el('span', {class: 'step-progress'},
        el('span', {style: `width:${Math.round(s.progress * 100)}%`})));
    }
    if (s.status === 'active' && s.detail) item.append(el('span', {class: 'step-detail'}, s.detail));
    return item;
  }

  function renderSteps() {
    stepsEl.replaceChildren();
    const used = new Set();
    PHASES.forEach(phase => {
      const steps = data.steps.filter(s => phase.keys.includes(s.key));
      if (!steps.length) return;
      steps.forEach(s => used.add(s.key));
      const allDone = steps.every(s => s.status === 'done');
      const current = steps.some(s => s.status !== 'done' && s.status !== 'pending');
      const doneCount = steps.filter(s => s.status === 'done').length;
      stepsEl.append(el('div', {class: 'pipe-phase' + (allDone ? ' is-done' : current ? ' is-current' : '')},
        el('div', {class: 'pipe-phase-name'}, el('span', {}, phase.name),
          el('span', {}, allDone ? '완료' : `${doneCount}/${steps.length}`)),
        el('div', {class: 'pipe-phase-row'}, steps.map(stepButton))));
    });
    // 알 수 없는 단계가 생기면 마지막에 그대로 붙인다
    const rest = data.steps.filter(s => !used.has(s.key));
    if (rest.length) stepsEl.append(el('div', {class: 'pipe-phase'}, el('div', {class: 'pipe-phase-row'}, rest.map(stepButton))));
  }

  function renderBadges() {
    badgeEl.replaceChildren();
    if (data.fallback_to_mock && isDev) {
      badgeEl.append(el('span', {class: 'badge text-bg-warning', title: data.fallback_detail},
        'mock으로 대체됨'));
    } else if (data.adapter_used && isDev) {
      badgeEl.append(el('span', {class: 'dev-badge'}, data.adapter_used));
    }
  }

  // ── 패널 ──
  function shotBadge(s) {
    if (s.reused) return el('span', {class: 'badge text-bg-light'}, '재사용');
    return el('span', {class: 'badge text-bg-secondary'}, s.provider === 'mock' ? 'mock' : '로컬');
  }

  // ③ 확인 = 올릴 영상(러프컷). 영상 자체는 본문 무대에 있으므로 여기서는 상태·추가 작업만 보여 준다.
  function renderVideoPanel() {
    const box = el('div');
    box.append(el('div', {class: 'small text-muted mb-2'},
      '장면 컷을 이어 붙이고 내레이션을 얹은 올릴 영상입니다. 아래 본문에서 재생해 확인하고 공개하세요. ' +
      '아쉬운 장면은 본문의 장면별 영상에서 피드백을 적고 그 컷만 다시 만들 수 있습니다.'));
    const narr = data.narration || {};
    if (narr.status && narr.status !== 'none') {
      box.append(el('div', {class: 'small mb-2'}, '내레이션: ' + (narr.detail || narr.status)));
    }
    return box;
  }

  // ── ② 제작 패널: 이미지 확인 / 컷 확인 ──
  // 장면 자체는 본문의 "장면별 영상"에서 한 장면씩 넘겨 보고 다시 만든다. 여기서는 장면별 상태만 한 줄로 보여 준다.
  function renderReviewPanel() {
    const isImages = data.review === 'images';
    const box = el('div');
    box.append(el('div', {class: 'small text-muted mb-2'}, isImages
      ? '장면별 첫 프레임을 본문에서 확인하세요. 번호를 누르면 그 장면으로 이동합니다.'
      : '장면별 컷을 본문에서 확인하세요. 번호를 누르면 그 장면으로 이동합니다. 모두 괜찮으면 "올릴 영상 만들기"를 누르세요.'));
    const row = el('div', {class: 'd-flex flex-wrap gap-1'});
    data.shots.forEach((s, i) => {
      const ready = isImages ? !!s.image_url : !!s.clip_url;
      const failed = s.status === 'failed';
      row.append(el('button', {
        type: 'button',
        class: 'btn btn-sm ' + (failed ? 'btn-outline-danger' : ready ? 'btn-outline-secondary' : 'btn-light'),
        title: `${s.id}${failed ? ' · 실패: ' + (s.error || '') : ready ? '' : ' · 아직 없음'}` +
               ((s.notes || []).length ? ' · 피드백: ' + s.notes.join(' / ') : ''),
        onclick: () => {
          const tab = document.getElementById('tab-' + s.id);
          if (tab) { tab.click(); tab.closest('.scene-section').scrollIntoView({behavior: 'smooth', block: 'start'}); }
        },
      }, `${i + 1}${failed ? ' !' : ready ? ' ✓' : ''}`));
    });
    box.append(row);
    return box;
  }

  function renderPanel() {
    panelEl.replaceChildren();
    if (openStep == null || !data) { panelEl.classList.add('d-none'); return; }
    panelEl.classList.remove('d-none');
    const step = data.steps.find(s => s.no === openStep);
    const sum = data.summary;
    const body = el('div');
    body.append(el('h6', {}, `${step.no}. ${stepName(step)}`));

    if (step.key === 'prompt_built') {
      data.shots.length
        ? data.shots.forEach(s => body.append(el('div', {class: 'panel-shot bilingual'},
            el('strong', {}, s.id),
            el('span', {}, s.prompt_ko ? el('span', {class: 'tr-ko'}, s.prompt_ko) : null,
               el('span', {class: 'tr-en', lang: 'en'}, s.prompt)))))
        : body.append(el('div', {class: 'text-muted small'}, '영상 생성을 시작하면 장면 목록이 만들어집니다.'));
    } else if (step.key === 'generating' && data.review) {
      body.append(renderReviewPanel());
    } else if (step.key === 'generating') {
      body.append(el('div', {}, step.detail || (step.status === 'pending' ? '아직 시작 전입니다.' : '')));
      if (step.status === 'active' && isCurrent) {
        body.append(el('button', {class: 'btn btn-outline-danger btn-sm mt-2',
          onclick: async (ev) => {
            if (!confirm('생성을 중지할까요? 지금 만드는 장면은 버려지고, 이미 만들어진 장면은 남습니다.')) return;
            ev.target.disabled = true; ev.target.textContent = '중지하는 중…';
            await post('/cancel'); refresh();
          }}, '생성 중지'));
      }
      if (step.status === 'failed') {
        body.append(el('div', {class: 'text-danger mb-2'}, step.error || ''));
        if (isCurrent) body.append(el('button', {class: 'btn btn-danger btn-sm',
          onclick: async () => { await post('/retry'); refresh(); }}, '실패 장면 재시도'));
      }
    } else if (step.key === 'video_ready' && (step.status === 'active' || step.status === 'failed')) {
      body.append(el('div', {}, step.detail || '올릴 영상을 만드는 중입니다.'));
      if (step.status === 'failed') {
        body.append(el('div', {class: 'text-danger mb-2'}, step.error || ''));
        if (isCurrent) body.append(el('button', {class: 'btn btn-danger btn-sm',
          onclick: async () => { await post('/retry'); refresh(); }}, '올릴 영상 다시 만들기'));
      }
    } else if (step.key === 'video_ready') {
      body.append(renderVideoPanel());
    } else if (step.key === 'feedback_collected') {
      body.append(el('div', {}, `모은 댓글 ${sum.feedback_total}개`));
    } else if (step.key === 'feedback_classified') {
      body.append(el('div', {}, `반영 후보 의견 ${sum.constructive}개 (전체의 ${Math.round(sum.constructive_ratio * 100)}%)`));
    } else if (step.key === 'clustering' || step.key === 'pending_review') {
      body.append(el('div', {}, `비슷한 의견 묶음 ${sum.cluster_count}개`));
      if (step.status === 'waiting' && reviewUrl) {
        body.append(el('a', {class: 'btn btn-primary btn-sm mt-2', href: reviewUrl}, '의견 검토하기'));
      }
    } else if (step.key === 'prompt_updated') {
      body.append(el('div', {}, `의견 ${sum.approved}개를 반영했습니다`));
      sum.changes.forEach(c => body.append(el('div', {class: 'small text-muted'}, '· ' + c)));
    } else {
      body.append(el('div', {class: 'text-muted small'}, step.status === 'pending' ? '아직 진행 전입니다.' : ''));
    }
    panelEl.append(body);
  }

  function renderAll() { renderSteps(); renderBadges(); renderPanel(); }

  // 단계별 기본 패널: ③ 확인 대기 / 실패 / 검토 대기를 자동으로 연다
  function autoOpen() {
    if (!isCurrent || openStep != null) return;
    const w = data.steps.find(s => s.status === 'waiting' || s.status === 'failed');
    if (w) openStep = w.no;
  }

  async function refresh() {
    try {
      const res = await fetch(statusUrl);
      data = await res.json();
    } catch (e) { schedule(10000); return; }
    if (isCurrent && (data.state !== root.dataset.state ||
                      (data.rough_running ? '1' : '0') !== root.dataset.rough)) {
      location.reload();
      return;
    }
    const signature = JSON.stringify(data);
    if (signature !== lastSignature) {
      lastSignature = signature;
      autoOpen();
      renderAll();
    }
    if (data.state === 'prompt_updated') return;   // 중지
    const waiting = data.steps.some(s => s.status === 'waiting');
    const running = data.steps.some(s => s.status === 'active');
    schedule(running ? 2000 : (waiting ? 10000 : 5000));
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(refresh, ms);
  }

  refresh();
})();
