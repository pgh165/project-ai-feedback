// unified diff 텍스트를 줄 단위로 색칠
function renderDiff(el, text) {
  el.textContent = '';
  for (const line of text.split('\n')) {
    const span = document.createElement('span');
    if (line.startsWith('@@')) span.className = 'hunk';
    else if (line.startsWith('+') && !line.startsWith('+++')) span.className = 'add';
    else if (line.startsWith('-') && !line.startsWith('---')) span.className = 'del';
    span.textContent = line + '\n';
    el.appendChild(span);
  }
}

document.querySelectorAll('pre[data-diff]').forEach(el => renderDiff(el, el.textContent));

// 검토 화면: 승인 반영 미리보기
const previewBtn = document.getElementById('preview-btn');
if (previewBtn) {
  previewBtn.addEventListener('click', async () => {
    const res = await fetch(previewBtn.dataset.url);
    const data = await res.json();
    const el = document.getElementById('preview');
    el.classList.remove('d-none');
    renderDiff(el, data.diff || '승인된 클러스터가 없습니다.');
  });
}

// 상세 화면: 영상 생성 진행 상황 폴링
const job = document.getElementById('job');
if (job) {
  const timer = setInterval(async () => {
    const res = await fetch(job.dataset.statusUrl);
    const data = await res.json();
    document.getElementById('job-message').textContent = data.message;
    if (data.job !== 'running') {
      clearInterval(timer);
      location.reload();
    }
  }, 3000);
}
