(() => {
  const table = document.querySelector('#top30-table');
  const rows = table ? Array.from(table.tBodies[0].rows) : [];
  const search = document.querySelector('#stock-search');
  const minScore = document.querySelector('#min-score');
  const source = document.querySelector('#source-filter');
  const direction = document.querySelector('#direction-filter');
  const count = document.querySelector('#result-count');

  const applyFilters = () => {
    const term = (search?.value || '').trim().toLowerCase();
    const threshold = Number(minScore?.value || 0);
    let visible = 0;
    rows.forEach((row) => {
      const matchesTerm = !term || `${row.dataset.code} ${row.dataset.name}`.toLowerCase().includes(term);
      const matchesScore = Number(row.dataset.score || 0) >= threshold;
      const matchesSource = !source?.value || source.value === 'all' || (row.dataset.source || '').includes(source.value);
      const pct = Number(row.dataset.pct || 0);
      const matchesDirection = !direction?.value || direction.value === 'all' ||
        (direction.value === 'up' && pct > 0) || (direction.value === 'down' && pct < 0) ||
        (direction.value === 'flat' && pct === 0);
      row.hidden = !(matchesTerm && matchesScore && matchesSource && matchesDirection);
      if (!row.hidden) visible += 1;
    });
    if (count) count.textContent = `显示 ${visible} / ${rows.length}`;
  };

  [search, minScore, source, direction].forEach((control) => {
    control?.addEventListener(control.tagName === 'INPUT' ? 'input' : 'change', applyFilters);
  });

  document.querySelector('#toggle-columns')?.addEventListener('click', (event) => {
    const expanded = table?.classList.toggle('show-optional');
    event.currentTarget.textContent = expanded ? '收起更多字段' : '展开更多字段';
    event.currentTarget.setAttribute('aria-expanded', String(Boolean(expanded)));
  });

  document.querySelectorAll('[data-sort]').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sort;
      const nextDirection = button.dataset.direction === 'asc' ? 'desc' : 'asc';
      document.querySelectorAll('[data-sort]').forEach((item) => delete item.dataset.direction);
      button.dataset.direction = nextDirection;
      const multiplier = nextDirection === 'asc' ? 1 : -1;
      rows.sort((a, b) => {
        const left = a.dataset[key] || '';
        const right = b.dataset[key] || '';
        const numeric = ['rank', 'price', 'pct', 'amount', 'turnover', 'ratio', 'score'].includes(key);
        return multiplier * (numeric ? Number(left) - Number(right) : left.localeCompare(right, 'zh-CN'));
      });
      rows.forEach((row) => table.tBodies[0].appendChild(row));
    });
  });

  const topButton = document.querySelector('.back-to-top');
  window.addEventListener('scroll', () => topButton?.classList.toggle('visible', window.scrollY > 500), { passive: true });
  topButton?.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
  applyFilters();
})();
