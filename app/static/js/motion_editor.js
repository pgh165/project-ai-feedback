/* 움직임 지정: 첫 프레임에 관절·물체를 차례로 찍고 동작을 고르면, 뼈대 미리 보기로 확인한 뒤 경로를 저장한다.
   경로 계산은 서버(motion_tracks.build_preset)가 한다 — 화면은 점을 받고 돌려받은 경로를 그리기만 한다. */
(function () {
  var panels = document.querySelectorAll('.motion-panel');
  if (!panels.length) return;
  var presetsReady = fetch('/api/motion/presets').then(function (r) { return r.json(); });
  var COLORS = {done: '#22C58B', moving: '#FFB020', fixed: 'rgba(255,255,255,.75)', link: '#7C5CFF', path: 'rgba(255,176,32,.55)'};
  var HIT = 16;  // 이 픽셀(화면 기준) 안을 누르면 찍은 점을 옮긴다

  async function postJson(url, body) {
    var res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    var data = {};
    try { data = await res.json(); } catch (e) {}
    return {ok: res.ok, data: data};
  }

  function setup(panel, presets) {
    var img = panel.querySelector('.motion-image'), canvas = panel.querySelector('.motion-canvas');
    var ctx = canvas.getContext('2d');
    var select = panel.querySelector('.motion-preset'), paramsBox = panel.querySelector('.motion-params');
    var takesSel = panel.querySelector('.motion-takes'), guide = panel.querySelector('.motion-guide');
    var message = panel.querySelector('.motion-message');
    var previewBtn = panel.querySelector('.motion-preview'), saveBtn = panel.querySelector('.motion-save');
    var saved = JSON.parse(panel.dataset.motion || 'null');
    var state = {points: {}, params: {}, tracks: null, frame: 0, timer: null, drag: null};

    Object.keys(presets).forEach(function (key) {
      var option = document.createElement('option');
      option.value = key;
      option.textContent = presets[key].label;
      select.appendChild(option);
    });
    if (saved && saved.preset && presets[saved.preset.name]) {
      select.value = saved.preset.name;
      state.points = saved.preset.points;
      state.params = saved.preset.params;
    }
    if (saved && saved.takes) takesSel.value = String(saved.takes);

    function preset() { return presets[select.value]; }
    function nextPoint() { return preset().points.find(function (p) { return !state.points[p[0]]; }); }
    function scale() { return canvas.getBoundingClientRect().width / canvas.width; }

    function stopPreview() {
      if (state.timer) clearInterval(state.timer);
      state.timer = null;
      state.tracks = null;
      previewBtn.textContent = '뼈대 미리 보기';
    }

    function renderKeyframes() {
      var JOINTS = [['shoulder', '어깨'], ['elbow', '팔꿈치'], ['wrist', '손목']];
      var frames = state.params.keyframes;
      var table = document.createElement('table');
      table.className = 'motion-frames';
      var head = table.insertRow();
      ['시점(0~1)'].concat(JOINTS.map(function (j) { return j[1] + '°'; })).concat(['']).forEach(function (text) {
        var th = document.createElement('th');
        th.textContent = text;
        head.appendChild(th);
      });
      frames.forEach(function (frame, index) {
        var row = table.insertRow();
        [['t', 0.01]].concat(JOINTS.map(function (j) { return [j[0], 1]; })).forEach(function (cell) {
          var input = document.createElement('input');
          input.type = 'number';
          input.className = 'form-control form-control-sm';
          input.step = cell[1];
          input.value = frame[cell[0]];
          input.setAttribute('aria-label', (index + 1) + '단계 ' + cell[0]);
          input.addEventListener('input', function () { frame[cell[0]] = Number(input.value); stopPreview(); refresh(); });
          row.insertCell().appendChild(input);
        });
        var remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-quiet btn-sm';
        remove.textContent = '×';
        remove.setAttribute('aria-label', (index + 1) + '단계 지우기');
        remove.disabled = frames.length === 1;
        remove.addEventListener('click', function () { frames.splice(index, 1); stopPreview(); renderParams(); refresh(); });
        row.insertCell().appendChild(remove);
      });
      paramsBox.appendChild(table);
      var add = document.createElement('button');
      add.type = 'button';
      add.className = 'btn btn-quiet btn-sm';
      add.textContent = '단계 추가';
      add.disabled = frames.length >= 8;
      add.addEventListener('click', function () {
        var last = frames[frames.length - 1];
        frames.forEach(function (f, i) { f.t = Math.round((i + 1) / (frames.length + 1) * 100) / 100; });
        frames.push({t: 1, shoulder: last.shoulder, elbow: last.elbow, wrist: last.wrist});
        stopPreview();
        renderParams();
        refresh();
      });
      paramsBox.appendChild(add);
    }

    function renderParams() {
      paramsBox.textContent = '';
      if (preset().keyframes) {
        if (!state.params.keyframes) state.params.keyframes = [{t: 1, shoulder: 0, elbow: 30, wrist: 0}];
        renderKeyframes();
        return;
      }
      Object.keys(preset().params).forEach(function (key) {
        var spec = preset().params[key];
        if (state.params[key] === undefined) state.params[key] = spec.default;
        var label = document.createElement('label');
        label.className = 'motion-param';
        var text = document.createElement('span');
        var range = document.createElement('input');
        range.type = 'range';
        range.className = 'form-range';
        range.min = spec.min; range.max = spec.max; range.step = 1;
        range.value = state.params[key];
        function show() { text.textContent = spec.label + ': ' + range.value; }
        range.addEventListener('input', function () { state.params[key] = Number(range.value); show(); stopPreview(); refresh(); });
        show();
        label.appendChild(text);
        label.appendChild(range);
        paramsBox.appendChild(label);
      });
    }

    function drawDot(x, y, color, text) {
      var r = 6 / scale();
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.lineWidth = 2 / scale();
      ctx.strokeStyle = '#10131C';
      ctx.stroke();
      if (text) {
        ctx.font = (13 / scale()) + 'px sans-serif';
        ctx.lineWidth = 3 / scale();
        ctx.strokeText(text, x + r + 2 / scale(), y + r);
        ctx.fillStyle = '#fff';
        ctx.fillText(text, x + r + 2 / scale(), y + r);
      }
    }

    function draw() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (state.tracks) {
        ctx.lineWidth = 2 / scale();
        ctx.strokeStyle = COLORS.path;
        state.tracks.forEach(function (track) {
          if (track.length < 2) return;
          ctx.beginPath();
          track.forEach(function (p, i) { if (i) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]); });
          ctx.stroke();
        });
        var at = function (i) { var t = state.tracks[i]; return t[Math.min(state.frame, t.length - 1)]; };
        ctx.lineWidth = 4 / scale();
        ctx.strokeStyle = COLORS.link;
        preset().links.forEach(function (l) {
          ctx.beginPath();
          ctx.moveTo(at(l[0])[0], at(l[0])[1]);
          ctx.lineTo(at(l[1])[0], at(l[1])[1]);
          ctx.stroke();
        });
        state.tracks.forEach(function (track, i) {
          var p = at(i);
          drawDot(p[0], p[1], track.length > 1 ? COLORS.moving : COLORS.fixed);
        });
        return;
      }
      preset().points.forEach(function (p, i) {
        var q = state.points[p[0]];
        if (q) drawDot(q[0], q[1], COLORS.done, (i + 1) + ' ' + p[1]);
      });
    }

    function refresh() {
      var next = nextPoint();
      var total = preset().points.length;
      var done = preset().points.filter(function (p) { return state.points[p[0]]; }).length;
      guide.textContent = next ? (done + 1) + '/' + total + ' — 첫 프레임에서 "' + next[1] + '"을(를) 누르세요.'
                               : '점을 모두 찍었습니다. 점을 끌어 옮길 수 있습니다. 뼈대 미리 보기로 확인하세요.';
      previewBtn.disabled = saveBtn.disabled = !!next;
      draw();
    }

    function toCanvas(e) {
      var rect = canvas.getBoundingClientRect();
      return [(e.clientX - rect.left) / rect.width * canvas.width, (e.clientY - rect.top) / rect.height * canvas.height];
    }

    canvas.addEventListener('pointerdown', function (e) {
      if (state.tracks) stopPreview();
      message.textContent = '';
      var p = toCanvas(e), limit = HIT / scale();
      var hit = preset().points.find(function (d) {
        var q = state.points[d[0]];
        return q && Math.hypot(q[0] - p[0], q[1] - p[1]) < limit;
      });
      var next = hit || nextPoint();
      if (!next) { refresh(); return; }
      state.points[next[0]] = p;
      state.drag = next[0];
      canvas.setPointerCapture(e.pointerId);
      refresh();
    });
    canvas.addEventListener('pointermove', function (e) {
      if (!state.drag) return;
      state.points[state.drag] = toCanvas(e);
      draw();
    });
    canvas.addEventListener('pointerup', function () { state.drag = null; refresh(); });

    function body() {
      return {preset: select.value, points: state.points, params: state.params,
              width: canvas.width, height: canvas.height};
    }

    previewBtn.addEventListener('click', async function () {
      if (state.timer) { stopPreview(); refresh(); return; }
      message.textContent = '';
      var res = await postJson('/api/motion/preview', body());
      if (!res.ok) { message.textContent = res.data.error || '미리 보기 실패'; return; }
      state.tracks = res.data.tracks;
      state.frame = 0;
      previewBtn.textContent = '미리 보기 멈춤';
      var longest = Math.max.apply(null, state.tracks.map(function (t) { return t.length; }));
      state.timer = setInterval(function () {
        state.frame = (state.frame + 1) % (longest + 15);  // 끝에서 잠깐 멈춘다
        draw();
      }, 5000 / longest);
      draw();
    });

    saveBtn.addEventListener('click', async function () {
      var takes = Number(takesSel.value);
      var imageStep = panel.dataset.imageStep === '1';
      var text = imageStep ? '이 경로를 저장합니다. 이미지를 승인하면 컷 ' + takes + '개를 경로대로 만듭니다. 계속할까요?'
                           : '이 경로대로 컷 ' + takes + '개를 새 버전으로 만듭니다 (컷마다 약 3분). 계속할까요?';
      if (!confirm(text)) return;
      saveBtn.disabled = true;
      var res = await postJson(panel.dataset.url, Object.assign(body(), {takes: takes}));
      if (!res.ok) { message.textContent = res.data.error || '저장 실패'; saveBtn.disabled = false; return; }
      location.reload();
    });

    var aiBtn = panel.querySelector('.motion-ai'), summary = panel.querySelector('.motion-summary');
    aiBtn.addEventListener('click', async function () {
      var text = panel.querySelector('.motion-text').value.trim();
      message.textContent = '';
      summary.textContent = '';
      if (!text) { message.textContent = '어떤 동작을 원하는지 적어 주세요.'; return; }
      aiBtn.disabled = true;
      aiBtn.textContent = 'AI가 분석하는 중… (처음엔 30초쯤)';
      var res = await postJson('/api/motion/plan', {text: text});
      aiBtn.disabled = false;
      aiBtn.textContent = 'AI로 동작 만들기';
      if (!res.ok) { message.textContent = res.data.error || '동작을 만들지 못했습니다'; return; }
      var plan = res.data.plan;
      if (plan.action === 'unsupported') { message.textContent = plan.reason; return; }
      stopPreview();
      select.value = plan.action;
      state.params = plan.action === 'joints' ? {keyframes: plan.keyframes} : {lift: plan.lift};
      takesSel.value = String(preset().takes);
      renderParams();
      refresh();
      summary.textContent = plan.summary + (res.data.fallback_reason
        ? ' (' + res.data.fallback_reason + ' 작은 모델로 만들어 틀릴 수 있습니다)' : '') + ' 미리 보기로 확인하세요.';
    });

    panel.querySelector('.motion-undo').addEventListener('click', function () {
      stopPreview();
      var last = preset().points.filter(function (p) { return state.points[p[0]]; }).pop();
      if (last) delete state.points[last[0]];
      refresh();
    });
    panel.querySelector('.motion-reset').addEventListener('click', function () {
      stopPreview();
      state.points = {};
      refresh();
    });
    var clearBtn = panel.querySelector('.motion-clear');
    if (clearBtn) clearBtn.addEventListener('click', async function () {
      var imageStep = panel.dataset.imageStep === '1';
      if (!confirm(imageStep ? '이 장면의 움직임 경로를 지웁니다. 계속할까요?'
                             : '움직임 경로를 지우고 이 컷을 경로 없이 새 버전으로 만듭니다. 계속할까요?')) return;
      clearBtn.disabled = true;
      var res = await postJson(panel.dataset.url, {});
      if (!res.ok) { message.textContent = res.data.error || '지우지 못했습니다'; clearBtn.disabled = false; return; }
      location.reload();
    });
    select.addEventListener('change', function () {
      stopPreview();
      state.params = {};
      takesSel.value = String(preset().takes);
      message.textContent = '';
      renderParams();
      refresh();
    });

    function ready() {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      if (!saved) takesSel.value = String(preset().takes);
      renderParams();
      refresh();
    }
    if (img.complete && img.naturalWidth) ready();
    else img.addEventListener('load', ready, {once: true});
    window.addEventListener('resize', draw);
  }

  panels.forEach(function (panel) {
    panel.addEventListener('toggle', async function once() {
      if (!panel.open) return;
      panel.removeEventListener('toggle', once);
      var img = panel.querySelector('.motion-image');
      img.src = img.dataset.src;
      setup(panel, await presetsReady);
    });
  });
})();
