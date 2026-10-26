// 파이프라인 점검 페이지 — 환경 점검 · 테스트 실행 · 자동 판정 · 작업 로그
(function () {
  const root = document.getElementById('devtest');
  if (!root) return;
  const d = root.dataset;

  const BADGE = {pass: 'text-bg-success', fail: 'text-bg-danger', warn: 'text-bg-warning', skip: 'text-bg-light'};
  const LABEL = {pass: '통과', fail: '실패', warn: '주의', skip: '대기'};

  function el(tag, attrs, ...children) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') n.className = v; else n.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  }
  async function api(url, method, body) {
    const res = await fetch(url, {
      method: method || 'GET', headers: {'Content-Type': 'application/json'},
      body: body ? JSON.stringify(body) : undefined,
    });
    let json = {};
    try { json = await res.json(); } catch (e) { /* 본문 없음 */ }
    return {ok: res.ok, status: res.status, json};
  }
  const $ = id => document.getElementById(id);
  function say(text, ok) {
    const box = $('action-msg');
    if (!box) { alert(text); return; }
    box.className = 'mt-2 small ' + (ok === false ? 'text-danger' : 'text-success');
    box.textContent = text;
  }

  // ── 환경 점검 ──
  async function loadEnv() {
    const r = await api(d.envUrl);
    const list = $('env-list');
    list.replaceChildren();
    r.json.items.forEach(i => list.append(el('li', {class: 'list-group-item d-flex gap-2 align-items-start'},
      el('span', {class: 'badge ' + BADGE[i.status]}, LABEL[i.status]),
      el('div', {}, el('div', {}, i.label), el('div', {class: 'small text-muted'}, i.detail)))));
    $('env-footer').textContent =
      `장면 기본 ${r.json.shot_seconds}초 · ${r.json.size} · 시험 모드 ${r.json.test_mode ? 'ON' : 'OFF'}`;
    $('opt-enhance').checked = !!r.json.prompt_enhance;
  }
  $('opt-enhance').addEventListener('change', async (e) => {
    const r = await api(e.target.dataset.url, 'POST', {enabled: e.target.checked});
    if (r.ok) loadEnv(); else e.target.checked = !e.target.checked;
  });
  $('env-refresh').addEventListener('click', loadEnv);
  loadEnv();

  // ── 예시 프리셋 (임베디드 소프트웨어 홍보) ──
  const STYLE = 'clean modern tech commercial, cinematic lighting, shallow depth of field, smooth camera motion, high detail';
  const PRESETS = [
    {
      key: 'physical-ai', label: '피지컬 AI', title: '센서에서 행동까지, 피지컬 AI', genre: '기술 홍보 영상',
      description: '카메라와 센서로 주변을 인식한 AI가 로봇 팔을 움직여 물건을 스스로 판단해 집어 옮기는 과정을 보여 주는 홍보 영상. 인식, 판단, 행동으로 이어지는 흐름을 차분하고 미래적인 분위기로 담는다.',
      subjects: '흰색과 회색의 6축 로봇 팔(끝에 검은 집게 그리퍼, 손목에 작은 카메라), 작업대 위의 빨간색과 파란색 상자들, 밝은 실험실',
      shots: [
        'A white and gray six-axis robot arm with a black gripper and a small wrist camera stands still above a workbench with red and blue boxes, soft studio lighting',
        'Close-up of the small camera on the wrist of the white robot arm, a faint blue light pulses as it scans the boxes',
        'The white and gray robot arm moves smoothly and picks up a red box with its black gripper, precise and steady motion',
        'The same white robot arm places the red box into a tray beside the blue boxes, then returns to its resting position, slow camera pull back'],
    },
    {
      key: 'autonomous-car', label: '자율주행차', title: '스스로 판단하는 도로', genre: '기술 홍보 영상',
      description: '야간 도시 도로를 달리는 자율주행차가 갑자기 나타난 장애물을 감지하고 부드럽게 감속해 안전하게 멈추는 모습을 보여 주는 홍보 영상. 신뢰감과 안정감이 핵심이다.',
      subjects: '은색 전기 세단(지붕에 작은 라이다 센서, 앞 유리 상단에 카메라), 비 온 뒤 젖은 야간 도로, 네온 불빛이 비치는 도시',
      shots: [
        'A silver electric sedan with a small lidar sensor on the roof drives along a wet city road at night, neon lights reflecting on the asphalt, tracking shot',
        'Close-up of the small lidar sensor on the roof of the silver sedan rotating, thin blue light beams sweep the street',
        'A cyclist suddenly crosses the road ahead, the silver sedan smoothly slows down and stops safely with a gentle motion, headlights glowing',
        'The same silver electric sedan with the lidar sensor on the roof starts moving again and drives calmly into the distance, slow aerial pull back'],
    },
    {
      key: 'circuit', label: '회로 제작', title: '아이디어에서 동작하는 보드까지', genre: '기술 홍보 영상',
      description: '작은 회로 기판이 만들어져 전원을 켜면 불이 들어오고 센서가 반응해 실제로 동작하는 모습까지 보여 주는 홍보 영상. 정교함과 완성도를 강조한다.',
      subjects: '손바닥 크기의 초록색 회로 기판(중앙에 검은 마이크로컨트롤러 칩, 가장자리에 작은 노란색 LED 세 개), 깔끔한 작업대, 따뜻한 조명',
      shots: [
        'Macro shot of a green circuit board with a black microcontroller chip in the center and three small yellow LEDs on its edge, resting on a clean workbench, warm lighting',
        'A precision soldering iron touches a tiny component on the green circuit board, a thin wisp of smoke rises, extreme close-up',
        'The same green circuit board with the black chip is connected by a thin cable, the three yellow LEDs light up one after another',
        'A hand moves close to a small sensor on the green board, the yellow LEDs flicker in response, slow camera push in'],
    },
    {
      key: 'robot-control', label: '로봇 제어', title: '정밀하게 움직이는 로봇', genre: '기술 홍보 영상',
      description: '바퀴형 모바일 로봇이 좁은 창고 통로에서 장애물을 피하며 목표 지점까지 정확하게 이동하고 멈추는 모습을 보여 주는 홍보 영상. 빠른 반응과 정밀한 제어를 강조한다.',
      subjects: '주황색과 검은색의 소형 바퀴 로봇(상단에 회전하는 작은 센서, 전면에 파란 LED 한 줄), 선반이 늘어선 밝은 창고',
      shots: [
        'An orange and black small wheeled robot with a rotating sensor on top and a blue LED strip on the front sits at the start of a narrow warehouse aisle, bright even lighting',
        'The same orange wheeled robot starts moving along the aisle between tall shelves, the blue LED strip glowing steadily, low tracking shot',
        'A cardboard box falls into the aisle ahead, the orange robot instantly turns and steers around it without stopping',
        'The orange and black wheeled robot with the blue LED strip stops precisely on a marked spot on the floor, the LED blinks once, slow camera pull back'],
    },
  ];
  // 해설 음성 문장 — 3초 장면 기준으로 한국어 15자 안팎
  const NARRATION = {
    'physical-ai': ['카메라와 센서가 주변을 살핍니다.', 'AI가 물체의 위치를 인식합니다.',
      '판단이 끝나면 로봇 팔이 움직입니다.', '인식에서 행동까지, 피지컬 AI.'],
    'autonomous-car': ['밤길을 달리는 자율주행차.', '라이다가 도로를 실시간으로 읽습니다.',
      '장애물을 감지하면 즉시 감속합니다.', '임베디드 소프트웨어가 안전을 지킵니다.'],
    'circuit': ['작은 회로 기판에서 시작합니다.', '정밀한 납땜으로 부품을 연결합니다.',
      '전원이 들어오면 LED가 켜집니다.', '센서가 반응하며 보드가 살아납니다.'],
    'robot-control': ['좁은 창고 통로의 모바일 로봇.', '센서로 주변을 읽으며 이동합니다.',
      '장애물이 나타나면 즉시 피합니다.', '정확한 위치에서 멈춥니다.'],
  };
  let currentGenre = '';
  function applyPreset(p, shotCount) {
    $('opt-narration').value = (NARRATION[p.key] || []).slice(0, shotCount || p.shots.length).join('\n');
    $('opt-title').value = p.title;
    $('opt-description').value = p.description;
    $('opt-shots-text').value = p.shots.slice(0, shotCount || p.shots.length).join('\n');
    $('opt-subjects').value = p.subjects;
    $('opt-style').value = STYLE;
    $('opt-aspect').value = '16:9';
    $('opt-seconds').value = '3';
    currentGenre = p.genre;
    $('preset-hint').textContent =
      `'${p.label}' 예시를 불러왔습니다 (${$('opt-shots-text').value.split('\n').length}장면 · 16:9 · 3초). 수정한 뒤 '새 테스트 시작'을 누르세요.`;
  }
  const presetBox = $('preset-buttons');
  PRESETS.forEach(p => {
    presetBox.append(el('button', {class: 'btn btn-sm btn-outline-secondary', type: 'button'}, p.label));
    presetBox.lastChild.addEventListener('click', () => applyPreset(p));
  });
  presetBox.append(el('button', {class: 'btn btn-sm btn-outline-secondary', type: 'button'}, '샘플로 되돌리기'));
  presetBox.lastChild.addEventListener('click', () => {
    ['opt-title', 'opt-description', 'opt-shots-text', 'opt-subjects', 'opt-style', 'opt-narration']
      .forEach(i => { $(i).value = ''; });
    currentGenre = '';
    $('preset-hint').textContent = '입력칸을 비웠습니다. 샘플 장면으로 실행됩니다.';
  });

  // 콘티 엑셀(.xlsx) 불러오기 — 서버가 읽은 값을 입력칸에 채운다 (저장은 '새 테스트 시작'에서)
  const sbFile = $('storyboard-file');
  if (sbFile) {
    $('storyboard-load').addEventListener('click', async () => {
      const msg = $('storyboard-msg');
      if (!sbFile.files.length) { msg.textContent = '엑셀 파일을 먼저 선택해 주세요.'; return; }
      const body = new FormData(); body.append('file', sbFile.files[0]);
      msg.textContent = '읽는 중…';
      try {
        const res = await fetch(sbFile.dataset.url, {method: 'POST', body});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || '읽을 수 없습니다.');
        $('opt-title').value = data.title || '';
        $('opt-description').value = data.description || '';
        $('opt-shots-text').value = data.shots_text || '';
        $('opt-narration').value = data.narration || '';
        $('opt-style').value = data.style || '';
        if (data.aspect) $('opt-aspect').value = data.aspect;
        if (data.seconds) {
          const sec = String(Math.round(data.seconds));
          if (![...$('opt-seconds').options].some(o => o.value === sec)) $('opt-seconds').append(el('option', {value: sec}, sec));
          $('opt-seconds').value = sec;
        }
        msg.textContent = `${data.count}개 장면을 불러왔습니다.` + (data.notes || []).map(n => ' · ' + n).join('') +
          " 확인한 뒤 '새 테스트 시작'을 누르세요.";
      } catch (e) {
        msg.textContent = e.message;
      }
    });
  }

  // ── 테스트 시작 / 정리 ──
  $('btn-start').addEventListener('click', async () => {
    const btn = $('btn-start');
    btn.disabled = true;
    const r = await api(d.startUrl, 'POST', {
      shots: Number($('opt-shots').value), seconds: Number($('opt-seconds').value),
      aspect: $('opt-aspect').value, description: $('opt-description').value,
      shots_text: $('opt-shots-text').value, title: $('opt-title').value,
      subjects: $('opt-subjects').value, style: $('opt-style').value, genre: currentGenre,
      narration: $('opt-narration').value});
    if (r.ok) location.href = r.json.url; else { btn.disabled = false; alert(r.json.error || '시작 실패'); }
  });
  $('btn-cleanup').addEventListener('click', async () => {
    if (!confirm('[TEST] 세션과 생성된 영상 폴더를 모두 삭제합니다. 계속할까요?')) return;
    const r = await api(d.cleanupUrl, 'POST');
    alert(`${r.json.removed}개 세션을 삭제했습니다.`);
    location.href = location.pathname;
  });

  if (!d.roundId) return;

  // ── 단계 실행 / 지적 댓글 ──
  $('btn-step').addEventListener('click', async () => {
    const r = await api(d.stepUrl, 'POST');
    if (!r.ok) { say(r.json.error || '실패', false); return; }
    say(r.json.message, true);
    if (r.json.next_url) { setTimeout(() => { location.href = r.json.next_url; }, 800); return; }
    refreshAll();
  });
  $('btn-targeted').addEventListener('click', async () => {
    const r = await api(d.targetedUrl, 'POST', {shot_id: $('opt-target-shot').value});
    say(r.ok ? r.json.message : (r.json.error || '실패'), r.ok);
    refreshAll();
  });

  // ── 자동 판정 ──
  async function loadVerify() {
    const r = await api(d.verifyUrl);
    if (!r.ok) return;
    const list = $('verify-list');
    list.replaceChildren();
    r.json.checks.forEach(c => list.append(el('li', {class: 'list-group-item'},
      el('div', {class: 'd-flex gap-2 align-items-center'},
        el('span', {class: 'badge ' + BADGE[c.status]}, LABEL[c.status]), el('strong', {}, c.label)),
      el('div', {class: 'small text-muted'}, '기대: ' + c.expect),
      c.detail ? el('div', {class: 'small'}, c.detail) : null)));
    const n = r.json.counts;
    $('verify-counts').textContent = `통과 ${n.pass} · 실패 ${n.fail} · 주의 ${n.warn} · 대기 ${n.skip}`;
    $('verify-counts').className = 'small ' + (n.fail ? 'text-danger fw-bold' : 'text-success');
  }

  // ── 영상 / 작업 로그 / 장면 선택 ──
  async function loadStatus() {
    const r = await api(d.statusUrl);
    if (!r.ok) return;
    const st = r.json;
    const box = $('video-box');
    if (st.video_url && box.dataset.src !== st.video_url) {
      box.dataset.src = st.video_url;
      box.replaceChildren(el('video', {class: 'w-100 rounded episode-video', controls: '', src: st.video_url}));
    }
    const sel = $('opt-target-shot');
    if (sel.options.length !== st.shots.length) {
      sel.replaceChildren(...st.shots.map(s => el('option', {value: s.id},
        `${s.id} (${s.start.toFixed(0)}–${s.end.toFixed(0)}초)`)));
    }
    const hint = {
      prompt_built: '다음: 영상 생성 시작', generating: '생성 중… (자동으로 갱신됩니다)',
      generation_failed: '다음: 실패 장면 재시도', video_ready: '다음: 영상 확인 → 업로드',
      uploaded: '다음: 시뮬레이션 댓글 수집', feedback_collected: '다음: 피드백 분류',
      feedback_classified: '다음: 클러스터링', clustering: '클러스터링 중…',
      pending_review: '다음: 장면을 지정한 클러스터만 승인(없으면 전체) → 다음 버전 생성', prompt_updated: '이 버전은 끝났습니다.',
    };
    $('step-hint').textContent = hint[st.state] || '';
    const busy = st.state === 'generating' || st.state === 'clustering' || st.state === 'prompt_updated';
    $('btn-step').disabled = busy || d.simulation === '0';
    return st.state;
  }
  async function loadJobs() {
    const r = await api(d.jobsUrl);
    const rows = $('job-rows');
    rows.replaceChildren(...r.json.jobs.map(j => el('tr', {},
      el('td', {}, j.id), el('td', {}, j.kind), el('td', {}, j.round_id),
      el('td', {class: j.status === 'failed' ? 'text-danger' : ''}, j.status + (j.error ? ` (${j.error.slice(0, 40)})` : '')),
      el('td', {}, j.seconds != null ? j.seconds + 's' : ''))));
  }

  let timer = null;
  async function refreshAll() {
    clearTimeout(timer);
    let state = null;
    try {
      [state] = await Promise.all([loadStatus(), loadVerify(), loadJobs()]);
    } catch (e) { /* 다음 주기에 재시도 */ }
    const running = state === 'generating' || state === 'clustering';
    timer = setTimeout(refreshAll, running ? 3000 : 10000);
  }
  refreshAll();
})();
