(function () {
  'use strict';
  // All filtering and sorting happens locally; scanner settings stay in the URL.
  const res = document.getElementById('res');
  const filter = document.getElementById('results-filter');
  const sort = document.getElementById('results-sort');
  if (res && filter && sort) {
    const body = res.querySelector('tbody');
    const rows = body ? Array.from(body.rows) : [];
    const count = document.getElementById('results-count');
    const empty = document.getElementById('results-empty');
    const update = () => {
      const q = filter.value.trim().toLocaleLowerCase();
      const theme = res.querySelector('#thbar button[aria-pressed="true"]');
      let n = 0;
      rows.forEach(row => {
        const matches = (row.dataset.search || '').toLocaleLowerCase().includes(q);
        const inTheme = !theme || (row.dataset.th || '').includes(' ' + theme.dataset.no + ' ');
        row.hidden = !(matches && inTheme);
        if (!row.hidden) n++;
      });
      count.textContent = n + ' / ' + rows.length + '종목';
      empty.hidden = !body || n !== 0;
    };
    filter.addEventListener('input', update);
    // Runs after the existing theme handler, so text and theme conditions combine.
    res.addEventListener('click', e => {
      if (e.target.closest('#thbar button')) update();
      const row = e.target.closest('tbody tr');
      if (row && !e.target.closest('a,button,input') && !getSelection().toString()) {
        const a = row.querySelector('a[href]'); if (a) location.href = a.href;
      }
    });
    sort.addEventListener('change', () => {
      if (!body) return;
      const key = sort.value;
      rows.sort((a,b) => {
        if (key === 'name') return a.dataset.search.localeCompare(b.dataset.search, 'ko');
        const av = Number(a.dataset[key]), bv = Number(b.dataset[key]);
        const validA = a.dataset[key] !== '' && Number.isFinite(av);
        const validB = b.dataset[key] !== '' && Number.isFinite(bv);
        return validA !== validB ? (validA ? -1 : 1) : (validA ? bv - av : 0);
      });
      const frag = document.createDocumentFragment(); rows.forEach(r => frag.appendChild(r)); body.appendChild(frag);
    });
    document.getElementById('results-detail').addEventListener('change', e => {
      res.querySelector('.wrap.wide').classList.toggle('show-forward', e.target.checked);
    });
    update();
  }
  // Four compact period groups, without changing the scanner's input contract.
  function compactPeriods() {
    const span = document.querySelector('#cfg .span');
    if (!span || span.querySelector('.span-switch')) return;
    const switcher = document.createElement('div'); switcher.className = 'span-switch';
    switcher.setAttribute('role','group'); switcher.setAttribute('aria-label','검색 기간 단위');
    const checked = span.querySelector('input[name=span]:checked');
    let current = span.querySelector('[name=start]').value ? 'range' : (checked ? checked.value.slice(-1) : 'd');
    function show(unit) {
      current = unit;
      span.querySelectorAll('.span-row').forEach(row => { row.hidden = row.dataset.unit !== unit; });
      switcher.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.unit === unit)));
    }
    for (const [unit,label] of [['d','일'],['w','주'],['m','월'],['range','직접 입력']]) {
      const b = document.createElement('button'); b.type='button'; b.dataset.unit=unit; b.textContent=label;
      b.addEventListener('click', () => show(unit)); switcher.appendChild(b);
    }
    span.insertBefore(switcher, span.querySelector('.span-row')); show(current);
  }
  compactPeriods();
  const cfg = document.querySelector('.sig');
  if (cfg) new MutationObserver(compactPeriods).observe(cfg, {childList:true});
  // Search shortcut avoids intercepting typing in any control.
  document.addEventListener('keydown', e => {
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.target.closest('input,select,textarea,[contenteditable=true]')) {
      const q = document.getElementById('q'); if(q){e.preventDefault();q.focus();}
    }
  });
  const toggleThemes = document.getElementById('theme-expand');
  if (toggleThemes) toggleThemes.addEventListener('click', () => {
    const expanded = document.querySelector('.stock-meta .themes').classList.toggle('expanded');
    toggleThemes.textContent = expanded ? '접기' : '테마 더 보기'; toggleThemes.setAttribute('aria-expanded',String(expanded));
  });
  try {
    const heading = document.querySelector('[data-stock-code]');
    let recents = JSON.parse(localStorage.getItem('flowapp.recents') || '[]');
    if (!Array.isArray(recents)) recents = [];
    if (heading) {
      recents = [{code:heading.dataset.stockCode,name:heading.dataset.stockName},...recents.filter(x=>x.code!==heading.dataset.stockCode)].slice(0,6);
      localStorage.setItem('flowapp.recents',JSON.stringify(recents));
    }
    const host = document.getElementById('recent-stocks');
    if (host && recents.length) {
      const label = document.createElement('span'); label.textContent='최근 본 종목'; host.appendChild(label);
      for (const r of recents) {
        if (!/^[0-9A-Z]{6}$/.test(r.code)) continue;
        const a = document.createElement('a');a.textContent=String(r.name);a.href='/stock/'+r.code+location.search;host.appendChild(a);
      }
      host.hidden=false;
    }
  } catch(e) { /* Private browsing and blocked storage retain the normal UI. */ }
})();
