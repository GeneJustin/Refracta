(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const MAX_FILES = 5;
  const MAX_BYTES = 15 * 1024 * 1024;
  const ALLOWED = ['.csv', '.tsv', '.txt', '.xlsx', '.xls', '.xlsm'];
  const PALETTE = ['#7c8cff', '#2dd4bf', '#f59e0b', '#f472b6', '#60a5fa',
                   '#a78bfa', '#34d399', '#fb7185', '#fbbf24', '#22d3ee'];

  const state = {
    localFiles: [],
    selectedIds: new Set(),
    historyItems: [],
    dashboard: null,
    savedId: null,
    charts: [],
    busy: false,
  };

  /* ---------- util ---------- */
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const trunc = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const nf = new Intl.NumberFormat('id-ID', { maximumFractionDigits: 2 });
  const nfc = new Intl.NumberFormat('id-ID', { notation: 'compact', maximumFractionDigits: 1 });
  const fmt = (v) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return '–';
    v = Number(v);
    return Math.abs(v) >= 10000 ? nfc.format(v) : nf.format(v);
  };
  const fmtDate = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '' :
      d.toLocaleDateString('id-ID', { day: 'numeric', month: 'short', year: 'numeric' });
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function toast(msg, type = '') {
    const t = document.createElement('div');
    t.className = 'toast ' + type;
    t.textContent = msg;
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), 4500);
  }

  async function api(url, opts) {
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* bukan JSON */ }
    if (res.status === 401) { window.location.href = '/login'; throw new Error('Sesi berakhir.'); }
    if (!res.ok || !data || data.success === false) {
      throw new Error((data && data.error) || `Permintaan gagal (${res.status}).`);
    }
    return data;
  }

  /* ---------- tema ---------- */
  const theme = {};
  function readTheme() {
    const s = getComputedStyle(document.documentElement);
    const g = (n) => s.getPropertyValue(n).trim();
    Object.assign(theme, {
      text: g('--text'), muted: g('--muted'), axis: g('--chart-axis'),
      grid: g('--chart-grid'), card: g('--card'), tooltip: g('--tooltip-bg'),
      heatLo: g('--heat-lo'), heatMid: g('--heat-mid'),
    });
  }
  function applyTheme(name) {
    document.documentElement.setAttribute('data-theme', name);
    try { localStorage.setItem('refracta-theme', name); } catch (_) { /* abaikan */ }
    readTheme();
    if (state.dashboard && !$('#viewDashboard').hidden) renderDashboard(state.dashboard);
  }
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem('refracta-theme'); } catch (_) { /* abaikan */ }
    document.documentElement.setAttribute('data-theme', saved === 'light' ? 'light' : 'dark');
    readTheme();
    $('#themeToggle').addEventListener('click', () => {
      applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
    });
  }

  /* ---------- view ---------- */
  function showView(name) {
    ['Start', 'Loading', 'Dashboard'].forEach((v) => { $('#view' + v).hidden = v !== name; });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /* =========================================================
     1. PILIH DATA
     ========================================================= */
  const totalSelected = () => state.localFiles.length + state.selectedIds.size;

  function updateGenerateBar() {
    const n = totalSelected();
    $('#selectedCount').textContent = n;
    $('#generateBtn').disabled = n === 0 || n > MAX_FILES || state.busy;
    $('#selectedHint').textContent = n > MAX_FILES ? `· melebihi batas ${MAX_FILES} file` : `· maksimal ${MAX_FILES}`;
  }

  function addLocalFiles(fileList) {
    for (const f of Array.from(fileList)) {
      const ext = '.' + f.name.split('.').pop().toLowerCase();
      if (!ALLOWED.includes(ext)) { toast(`Format '${f.name}' tidak didukung.`, 'error'); continue; }
      if (f.size > MAX_BYTES) { toast(`'${f.name}' lebih dari 15 MB.`, 'error'); continue; }
      if (state.localFiles.some((x) => x.name === f.name && x.size === f.size)) continue;
      if (totalSelected() >= MAX_FILES) { toast(`Maksimal ${MAX_FILES} file.`, 'error'); break; }
      state.localFiles.push(f);
    }
    renderLocalList();
    updateGenerateBar();
  }

  function renderLocalList() {
    const ul = $('#localList');
    ul.innerHTML = '';
    state.localFiles.forEach((f, i) => {
      const li = document.createElement('li');
      li.innerHTML = `<span title="${esc(f.name)}">${esc(f.name)}</span><small>${(f.size / 1024).toFixed(0)} KB</small>`;
      const b = document.createElement('button');
      b.type = 'button'; b.setAttribute('aria-label', 'Hapus file'); b.textContent = '×';
      b.addEventListener('click', () => { state.localFiles.splice(i, 1); renderLocalList(); updateGenerateBar(); });
      li.appendChild(b);
      ul.appendChild(li);
    });
  }

  function initDropzone() {
    const dz = $('#dropzone'), input = $('#fileInput');
    input.addEventListener('change', () => { addLocalFiles(input.files); input.value = ''; });
    ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('drag'); }));
    dz.addEventListener('drop', (e) => { if (e.dataTransfer && e.dataTransfer.files) addLocalFiles(e.dataTransfer.files); });
  }

  async function loadHistory() {
    try {
      const data = await api('/api/dashboard/sources');
      state.historyItems = data.items || [];
    } catch (e) {
      state.historyItems = [];
      $('#historyList').innerHTML = `<li class="empty">${esc(e.message)}</li>`;
      return;
    }
    renderHistory();
  }

  function renderHistory() {
    const q = $('#historySearch').value.trim().toLowerCase();
    const items = state.historyItems.filter((it) =>
      !q || (it.csv_name || '').toLowerCase().includes(q) || (it.name || '').toLowerCase().includes(q));
    const ul = $('#historyList');
    $('#historyCount').textContent = `${state.historyItems.length} file CSV`;

    if (!items.length) {
      ul.innerHTML = state.historyItems.length
        ? '<li class="empty">Tidak ada file yang cocok.</li>'
        : '<li class="empty">Belum ada CSV. Scan dokumen dulu lewat tombol di bawah.</li>';
      return;
    }

    ul.innerHTML = '';
    items.forEach((it) => {
      const li = document.createElement('li');
      li.className = 'history-item' + (state.selectedIds.has(it.file_id) ? ' selected' : '');
      li.innerHTML = `
        <span class="check"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg></span>
        <div class="meta"><b title="${esc(it.csv_name)}">${esc(it.csv_name)}</b>
        <small>${it.row_count ?? '?'} baris · ${esc(fmtDate(it.created_at))}</small></div>`;
      li.addEventListener('click', () => {
        if (state.selectedIds.has(it.file_id)) state.selectedIds.delete(it.file_id);
        else if (totalSelected() >= MAX_FILES) { toast(`Maksimal ${MAX_FILES} file.`, 'error'); return; }
        else state.selectedIds.add(it.file_id);
        li.classList.toggle('selected');
        updateGenerateBar();
      });
      ul.appendChild(li);
    });
  }

  /* ---------- dashboard tersimpan ---------- */
  async function loadSaved() {
    try {
      const data = await api('/api/dashboards');
      const items = data.items || [];
      $('#savedSection').hidden = !items.length;
      const grid = $('#savedGrid');
      grid.innerHTML = '';
      items.forEach((it) => {
        const card = document.createElement('article');
        card.className = 'saved-card';
        card.innerHTML = `<h3>${esc(it.name)}</h3><p>${esc(it.description || 'Tanpa deskripsi')}</p>
          <footer><span>${esc(fmtDate(it.created_at))}</span><button class="del" type="button">Hapus</button></footer>`;
        card.addEventListener('click', () => openSaved(it.id));
        card.querySelector('.del').addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm(`Hapus dashboard "${it.name}"?`)) return;
          try { await api(`/api/dashboards/${it.id}`, { method: 'DELETE' }); toast('Dashboard dihapus.', 'ok'); loadSaved(); }
          catch (err) { toast(err.message, 'error'); }
        });
        grid.appendChild(card);
      });
    } catch (_) { /* daftar tersimpan opsional */ }
  }

  async function openSaved(id) {
    try {
      const data = await api(`/api/dashboards/${id}`);
      state.dashboard = data.dashboard;
      state.savedId = id;
      state.localFiles = [];
      showDashboard();
    } catch (e) { toast(e.message, 'error'); }
  }

  /* =========================================================
     2. GENERATE
     ========================================================= */
  let stepTimer = null;
  function runLoadingSteps() {
    const items = Array.from(document.querySelectorAll('#loadingSteps li'));
    let i = 0;
    const paint = () => items.forEach((li, idx) => {
      li.className = idx < i ? 'done' : idx === i ? 'active' : '';
    });
    paint();
    clearInterval(stepTimer);
    stepTimer = setInterval(() => { if (i < items.length - 1) { i++; paint(); } }, 2600);
  }

  async function generate() {
    if (state.busy || !totalSelected()) return;
    state.busy = true;
    updateGenerateBar();

    const fd = new FormData();
    state.localFiles.forEach((f) => fd.append('files', f, f.name));
    state.selectedIds.forEach((id) => fd.append('file_ids', id));

    showView('Loading');
    runLoadingSteps();
    try {
      const data = await api('/api/dashboard/generate', { method: 'POST', body: fd });
      state.dashboard = data.dashboard;
      state.savedId = null;
      showDashboard();
    } catch (e) {
      toast(e.message, 'error');
      showView('Start');
    } finally {
      clearInterval(stepTimer);
      state.busy = false;
      updateGenerateBar();
    }
  }

  /* =========================================================
     3. RENDER DASHBOARD
     ========================================================= */
  function showDashboard() {
    showView('Dashboard');
    $('#nameInput').value = state.dashboard.title || '';
    updateSaveButton();
    renderDashboard(state.dashboard);
  }

  function updateSaveButton() {
    const b = $('#saveDbBtn');
    b.disabled = !!state.savedId;
    b.textContent = state.savedId ? 'Tersimpan ✓' : 'Simpan';
  }

  function disposeCharts() {
    state.charts.forEach((c) => { try { c.ro && c.ro.disconnect(); c.chart.dispose(); } catch (_) { /* abaikan */ } });
    state.charts = [];
  }

  function renderDashboard(d) {
    disposeCharts();
    readTheme();
    const root = $('#dashboardCanvas');
    const provider = { gemini: 'Dirancang oleh AI · Gemini', openrouter: 'Dirancang oleh AI', heuristic: 'Dibuat otomatis' }[d.provider];

    root.innerHTML = `
      <div class="dash-header"><div>
        <h1>${esc(d.title)}</h1>
        <p>${esc(d.summary)}</p>
        <div class="dash-badges">
          ${provider ? `<span class="badge accent">${esc(provider)}</span>` : ''}
          ${(d.datasets || []).map((s) => `<span class="badge">${esc(trunc(s.name, 40))} · ${nf.format(s.rows)} baris</span>`).join('')}
          ${d.generated_at ? `<span class="badge">${esc(fmtDate(d.generated_at))}</span>` : ''}
        </div></div></div>
      <div class="kpis" id="kpiRow"></div>
      <div class="insights" id="insightBox" hidden></div>
      <div class="chart-grid" id="chartGrid"></div>
      <div class="quality" id="qualityBox" hidden></div>
      <div class="dash-footer">Dibuat dengan Refracta</div>`;

    // KPI
    const kpiRow = $('#kpiRow');
    (d.kpis || []).forEach((k, i) => {
      const el = document.createElement('div');
      el.className = 'kpi';
      el.style.setProperty('--kpi-color', PALETTE[i % PALETTE.length]);
      el.style.animationDelay = `${i * 60}ms`;
      const val = typeof k.value === 'number' ? `${esc(k.prefix || '')}${fmt(k.value)}${esc(k.suffix || '')}` : esc(k.value);
      el.innerHTML = `<small>${esc(k.label)}</small><div class="val">${val}</div>${k.hint ? `<div class="hint">${esc(k.hint)}</div>` : ''}`;
      kpiRow.appendChild(el);
    });
    if (!(d.kpis || []).length) kpiRow.remove();

    // Insight
    if ((d.insights || []).length) {
      const box = $('#insightBox');
      box.hidden = false;
      box.innerHTML = '<h2>Temuan utama</h2><ul>' + d.insights.map((it) =>
        `<li class="${it.source === 'ai' ? 'ai' : ''}"><i></i><span>${esc(it.text)}${it.source === 'ai' ? '<em>AI</em>' : ''}</span></li>`).join('') + '</ul>';
    }

    // Chart
    const grid = $('#chartGrid');
    (d.charts || []).forEach((ch, i) => {
      try { grid.appendChild(buildCard(ch, i)); }
      catch (e) { console.warn('Chart gagal dirender:', ch.type, e); }
    });

    // Kualitas data
    if ((d.quality || []).length) {
      const q = $('#qualityBox');
      q.hidden = false;
      q.innerHTML = '<h2>Catatan pembersihan data</h2><ul>' + d.quality.map((t) => `<li>${esc(t)}</li>`).join('') + '</ul>';
    }
  }

  const CHART_HEIGHT = {
    line: 360, area: 360, donut: 340, treemap: 340, scatter: 340,
    histogram: 320, box: 340, heatmap: 380, radar: 360, forecast: 400,
  };
  function heightFor(ch) {
    if (ch.type === 'bar') return ch.horizontal ? Math.max(300, 70 + ch.categories.length * 30) : 340;
    if (ch.type === 'correlation') return Math.max(340, 120 + ch.labels.length * 38);
    return CHART_HEIGHT[ch.type] || 340;
  }

  function buildCard(ch, index) {
    const card = document.createElement('article');
    card.className = `chart-card span-${ch.span === 12 ? 12 : 6}`;
    card.style.animationDelay = `${Math.min(index, 8) * 50}ms`;
    card.innerHTML = `<header><h3>${esc(ch.title)}</h3>${ch.description ? `<p>${esc(ch.description)}</p>` : ''}</header>`;

    if (ch.type === 'table') {
      card.insertAdjacentHTML('beforeend', tableHtml(ch));
      return card;
    }

    const body = document.createElement('div');
    body.className = 'chart-body';
    body.style.height = heightFor(ch) + 'px';
    card.appendChild(body);

    const option = OPTIONS[ch.type] && OPTIONS[ch.type](ch);
    if (option) {
      const chart = echarts.init(body, null, { renderer: 'canvas', devicePixelRatio: Math.max(2, window.devicePixelRatio || 1) });
      chart.setOption(option);
      let ro = null;
      if (window.ResizeObserver) { ro = new ResizeObserver(() => chart.resize()); ro.observe(body); }
      state.charts.push({ chart, ro });
    }

    const foot = footerHtml(ch);
    if (foot) card.insertAdjacentHTML('beforeend', foot);
    return card;
  }

  function tableHtml(ch) {
    const isNum = (v) => typeof v === 'number';
    const head = ch.columns.map((c) => `<th>${esc(c)}</th>`).join('');
    const rows = ch.rows.map((r) => '<tr>' + r.map((v) =>
      `<td class="${isNum(v) ? 'num' : ''}" title="${esc(v)}">${isNum(v) ? esc(fmt(v)) : esc(v)}</td>`).join('') + '</tr>').join('');
    return `<div class="tbl-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function footerHtml(ch) {
    if (ch.type === 'scatter' && ch.r != null) {
      const s = Math.abs(ch.r) >= .7 ? 'kuat' : Math.abs(ch.r) >= .4 ? 'sedang' : 'lemah';
      return `<div class="chart-foot"><span>Korelasi (r): <b>${ch.r.toFixed(2)}</b></span><span>Hubungan <b>${s}</b></span></div>`;
    }
    if (ch.type === 'histogram' && ch.stats) {
      const s = ch.stats;
      return `<div class="chart-foot"><span>Rata-rata <b>${fmt(s.mean)}</b></span><span>Median <b>${fmt(s.median)}</b></span><span>Std. deviasi <b>${fmt(s.std)}</b></span><span>Rentang <b>${fmt(s.min)} – ${fmt(s.max)}</b></span></div>`;
    }
    if (ch.type === 'forecast' && ch.stats) {
      const s = ch.stats;
      const arrow = s.direction === 'naik' ? '↗' : s.direction === 'turun' ? '↘' : '→';
      return `<div class="stat-row">
        <div class="stat"><small>Arah tren</small><b>${arrow} ${esc(s.direction)} (${s.pct_per_period >= 0 ? '+' : ''}${s.pct_per_period.toFixed(1)}% / ${esc(s.unit)})</b></div>
        <div class="stat"><small>Prediksi ${esc(s.next_label)}</small><b>${fmt(s.next_value)}</b></div>
        <div class="stat"><small>Keandalan model</small><b>${esc(s.confidence)} · R² ${s.r2.toFixed(2)}</b></div>
        <div class="stat"><small>Model</small><b>${esc(s.model)} · ${s.n} periode</b></div></div>`;
    }
    if (ch.type === 'correlation') {
      return '<div class="chart-foot"><span>Nilai mendekati <b>+1</b> = naik bersama, <b>−1</b> = berlawanan arah, <b>0</b> = tidak berhubungan.</span></div>';
    }
    return '';
  }

  /* ---------- opsi ECharts ---------- */
  const alpha = (hex, a) => hex + a;
  const gradV = (c) => new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: c }, { offset: 1, color: alpha(c, '88') }]);
  const gradH = (c) => new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: alpha(c, '88') }, { offset: 1, color: c }]);
  const areaGrad = (c) => new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: alpha(c, '55') }, { offset: 1, color: alpha(c, '05') }]);

  const base = (extra = {}) => ({
    color: PALETTE,
    textStyle: { fontFamily: 'Inter, system-ui, sans-serif', color: theme.text },
    animationDuration: 700,
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    ...extra,
  });
  const tip = (trigger = 'axis', extra = {}) => ({
    trigger, confine: true, backgroundColor: theme.tooltip, borderColor: theme.axis,
    textStyle: { color: theme.text, fontSize: 12 },
    extraCssText: 'border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.25);',
    valueFormatter: (v) => fmt(v), ...extra,
  });
  const legend = (extra = {}) => ({ type: 'scroll', top: 0, textStyle: { color: theme.muted, fontSize: 12 }, pageTextStyle: { color: theme.muted }, ...extra });
  const catAxis = (data, o = {}) => ({
    type: 'category', data, inverse: !!o.inverse,
    axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false },
    axisLabel: { color: theme.muted, fontSize: 11, hideOverlap: true, rotate: o.rotate || 0, formatter: (v) => trunc(v, o.maxLen || 14) },
  });
  const valAxis = (o = {}) => ({
    type: 'value', scale: !!o.scale, name: o.name,
    nameTextStyle: { color: theme.muted, fontSize: 11, align: 'left' },
    axisLabel: { color: theme.muted, fontSize: 11, formatter: (v) => fmt(v) },
    splitLine: { lineStyle: { color: theme.grid, type: 'dashed' } },
    axisLine: { show: false }, axisTick: { show: false },
  });

  const OPTIONS = {
    bar(ch) {
      const multi = ch.series.length > 1;
      const h = ch.horizontal;
      const o = base({
        tooltip: tip('axis', { axisPointer: { type: 'shadow' } }),
        legend: multi ? legend() : { show: false },
        series: ch.series.map((s, i) => ({
          name: s.name, type: 'bar', data: s.data, barMaxWidth: 34,
          stack: ch.stacked && multi ? 'total' : undefined,
          itemStyle: {
            borderRadius: ch.stacked && multi ? 0 : (h ? [0, 6, 6, 0] : [6, 6, 0, 0]),
            color: multi ? PALETTE[i % PALETTE.length] : (h ? gradH(PALETTE[0]) : gradV(PALETTE[0])),
          },
          emphasis: { focus: 'series' },
        })),
      });
      o.grid.top = multi ? 44 : 16;
      if (h) { o.xAxis = valAxis(); o.yAxis = catAxis(ch.categories, { inverse: true, maxLen: 22 }); }
      else { o.xAxis = catAxis(ch.categories, { rotate: ch.categories.length > 7 ? 30 : 0 }); o.yAxis = valAxis(); }
      return o;
    },

    line(ch, forceArea) {
      const multi = ch.series.length > 1;
      const area = forceArea || ch.type === 'area' || !multi;
      const o = base({
        tooltip: tip('axis'),
        legend: multi ? legend() : { show: false },
        xAxis: catAxis(ch.categories, { rotate: ch.categories.length > 12 ? 30 : 0 }),
        yAxis: valAxis(),
        series: ch.series.map((s, i) => {
          const c = PALETTE[i % PALETTE.length];
          return {
            name: s.name, type: 'line', data: s.data, smooth: .3, connectNulls: true,
            showSymbol: ch.categories.length <= 40, symbolSize: 6,
            lineStyle: { width: 2.5, color: c }, itemStyle: { color: c },
            areaStyle: area ? { color: areaGrad(c) } : undefined,
            emphasis: { focus: 'series' },
          };
        }),
      });
      o.grid.top = multi ? 44 : 20;
      if (ch.categories.length > 60) o.dataZoom = [{ type: 'inside' }];
      return o;
    },
    area(ch) { return OPTIONS.line(ch, true); },

    donut(ch) {
      const o = base({
        tooltip: { ...tip('item'), formatter: (p) => `${esc(p.name)}<br/><b>${fmt(p.value)}</b> (${p.percent}%)` },
        legend: legend({ top: undefined, bottom: 0 }),
        series: [{
          type: 'pie', radius: ['50%', '74%'], center: ['50%', '44%'],
          itemStyle: { borderRadius: 8, borderColor: theme.card, borderWidth: 3 },
          label: { show: false }, data: ch.data,
          emphasis: { scale: true, scaleSize: 6 },
        }],
      });
      delete o.grid;
      if (ch.total != null) {
        o.title = {
          text: fmt(ch.total), subtext: ch.measure, left: 'center', top: '36%',
          textStyle: { color: theme.text, fontSize: 24, fontWeight: 800 },
          subtextStyle: { color: theme.muted, fontSize: 12 },
        };
      }
      return o;
    },

    treemap(ch) {
      const o = base({
        tooltip: { ...tip('item'), formatter: (p) => `${esc(p.name)}: <b>${fmt(p.value)}</b>` },
        series: [{
          type: 'treemap', data: ch.data, roam: false, nodeClick: false,
          breadcrumb: { show: false }, left: 0, right: 0, top: 0, bottom: 0,
          label: { show: true, formatter: '{b}', color: '#fff', fontSize: 12, overflow: 'truncate' },
          itemStyle: { borderColor: theme.card, borderWidth: 3, borderRadius: 8, gapWidth: 3 },
          color: PALETTE,
        }],
      });
      delete o.grid;
      return o;
    },

    scatter(ch) {
      const multi = ch.series.length > 1;
      const series = ch.series.map((s, i) => ({
        name: s.name, type: 'scatter', data: s.data, symbolSize: 8,
        itemStyle: { color: PALETTE[i % PALETTE.length], opacity: .7 },
      }));
      series.push({
        name: 'Tren linear', type: 'line', data: ch.trend, symbol: 'none', silent: true,
        lineStyle: { type: 'dashed', width: 2, color: PALETTE[2] }, tooltip: { show: false },
      });
      const o = base({
        tooltip: tip('item', { formatter: (p) => p.seriesType === 'scatter'
          ? `${esc(ch.xLabel)}: <b>${fmt(p.value[0])}</b><br/>${esc(ch.yLabel)}: <b>${fmt(p.value[1])}</b>` : '' }),
        legend: multi ? legend() : { show: false },
        xAxis: { ...valAxis({ scale: true }), name: trunc(ch.xLabel, 24), nameLocation: 'middle', nameGap: 28 },
        yAxis: valAxis({ scale: true, name: trunc(ch.yLabel, 24) }),
        series,
      });
      o.grid.top = multi ? 44 : 30; o.grid.bottom = 28;
      return o;
    },

    histogram(ch) {
      const o = base({
        tooltip: tip('axis', { axisPointer: { type: 'shadow' }, valueFormatter: (v) => `${fmt(v)} data` }),
        xAxis: catAxis(ch.categories, { rotate: ch.categories.length > 8 ? 40 : 0, maxLen: 18 }),
        yAxis: valAxis({ name: 'Frekuensi' }),
        series: [{ type: 'bar', data: ch.data, barCategoryGap: '8%',
          itemStyle: { borderRadius: [4, 4, 0, 0], color: gradV(PALETTE[1]) } }],
      });
      o.grid.top = 30;
      return o;
    },

    box(ch) {
      const o = base({
        tooltip: tip('item', { formatter: (p) => {
          if (p.seriesType === 'scatter') return `Pencilan: <b>${fmt(p.value[1])}</b>`;
          const v = (Array.isArray(p.value) ? p.value : p.data).slice(-5);
          return `<b>${esc(p.name)}</b><br/>Maks: ${fmt(v[4])}<br/>Q3: ${fmt(v[3])}<br/>Median: <b>${fmt(v[2])}</b><br/>Q1: ${fmt(v[1])}<br/>Min: ${fmt(v[0])}`;
        } }),
        xAxis: catAxis(ch.categories, { rotate: ch.categories.length > 5 ? 25 : 0 }),
        yAxis: valAxis({ scale: true, name: trunc(ch.yLabel, 24) }),
        series: [
          { type: 'boxplot', data: ch.boxes, itemStyle: { color: alpha(PALETTE[0], '33'), borderColor: PALETTE[0], borderWidth: 2 } },
          { type: 'scatter', data: ch.outliers, symbolSize: 6, itemStyle: { color: PALETTE[3], opacity: .8 } },
        ],
      });
      o.grid.top = 30;
      return o;
    },

    correlation(ch) {
      const o = base({
        tooltip: tip('item', { formatter: (p) => `${esc(ch.labels[p.value[1]])} × ${esc(ch.labels[p.value[0]])}<br/>r = <b>${p.value[2].toFixed(2)}</b>` }),
        xAxis: { ...catAxis(ch.labels, { rotate: 30, maxLen: 12 }), splitArea: { show: false } },
        yAxis: { ...catAxis(ch.labels, { inverse: true, maxLen: 14 }) },
        visualMap: { min: -1, max: 1, calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemWidth: 12, itemHeight: 150,
          textStyle: { color: theme.muted }, inRange: { color: ['#f472b6', theme.heatMid, '#2dd4bf'] } },
        series: [{ type: 'heatmap', data: ch.data,
          label: { show: true, color: theme.text, fontSize: 11, formatter: (p) => p.value[2].toFixed(2) },
          itemStyle: { borderColor: theme.card, borderWidth: 2, borderRadius: 4 } }],
      });
      o.grid = { left: 8, right: 16, top: 10, bottom: 56, containLabel: true };
      return o;
    },

    heatmap(ch) {
      const show = ch.xLabels.length * ch.yLabels.length <= 80;
      const o = base({
        tooltip: tip('item', { formatter: (p) => `${esc(ch.yLabels[p.value[1]])} × ${esc(ch.xLabels[p.value[0]])}<br/>${esc(ch.measure)}: <b>${fmt(p.value[2])}</b>` }),
        xAxis: catAxis(ch.xLabels, { rotate: 30, maxLen: 12 }),
        yAxis: catAxis(ch.yLabels, { inverse: true, maxLen: 14 }),
        visualMap: { min: 0, max: ch.max || 1, calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemWidth: 12, itemHeight: 150,
          textStyle: { color: theme.muted }, inRange: { color: [theme.heatLo, PALETTE[0]] } },
        series: [{ type: 'heatmap', data: ch.data,
          label: { show, color: theme.text, fontSize: 11, formatter: (p) => fmt(p.value[2]) },
          itemStyle: { borderColor: theme.card, borderWidth: 2, borderRadius: 4 } }],
      });
      o.grid = { left: 8, right: 16, top: 10, bottom: 56, containLabel: true };
      return o;
    },

    radar(ch) {
      const o = base({
        tooltip: tip('item', { valueFormatter: undefined }),
        legend: legend({ top: undefined, bottom: 0 }),
        radar: { indicator: ch.indicators, radius: '62%', center: ['50%', '46%'],
          axisName: { color: theme.muted, fontSize: 11 },
          splitLine: { lineStyle: { color: theme.grid } }, axisLine: { lineStyle: { color: theme.grid } },
          splitArea: { show: false } },
        series: [{ type: 'radar',
          data: ch.series.map((s, i) => ({ name: s.name, value: s.value,
            lineStyle: { width: 2, color: PALETTE[i % PALETTE.length] },
            itemStyle: { color: PALETTE[i % PALETTE.length] },
            areaStyle: { color: alpha(PALETTE[i % PALETTE.length], '22') } })) }],
      });
      delete o.grid;
      return o;
    },

    forecast(ch) {
      const diff = ch.upper.map((u, i) => (u == null || ch.lower[i] == null) ? null : Math.max(u - ch.lower[i], 0));
      const o = base({
        tooltip: tip('axis', { formatter: (ps) => {
          if (!ps.length) return '';
          const i = ps[0].dataIndex;
          let html = `<b>${esc(ch.labels[i])}</b>`;
          ps.forEach((p) => {
            if (p.seriesName === '_lower') return;
            if (p.seriesName === 'Rentang 95%') {
              if (ch.lower[i] != null) html += `<br/>${p.marker} Rentang 95%: ${fmt(ch.lower[i])} – ${fmt(ch.upper[i])}`;
            } else if (p.value != null) html += `<br/>${p.marker} ${esc(p.seriesName)}: <b>${fmt(p.value)}</b>`;
          });
          return html;
        } }),
        legend: legend({ data: ['Aktual', 'Garis tren', 'Prakiraan', 'Rentang 95%'] }),
        xAxis: catAxis(ch.labels, { rotate: ch.labels.length > 14 ? 30 : 0 }),
        yAxis: valAxis({ scale: true }),
        series: [
          { name: '_lower', type: 'line', data: ch.lower, stack: 'band', symbol: 'none', lineStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
          { name: 'Rentang 95%', type: 'line', data: diff, stack: 'band', symbol: 'none', lineStyle: { opacity: 0 }, areaStyle: { color: alpha(PALETTE[2], '2e') }, silent: true },
          { name: 'Aktual', type: 'line', data: ch.actual, smooth: .25, showSymbol: ch.labels.length <= 40, symbolSize: 6, lineStyle: { width: 2.5, color: PALETTE[0] }, itemStyle: { color: PALETTE[0] } },
          { name: 'Garis tren', type: 'line', data: ch.fitted, symbol: 'none', lineStyle: { width: 1.5, type: 'dotted', color: PALETTE[1] }, itemStyle: { color: PALETTE[1] } },
          { name: 'Prakiraan', type: 'line', data: ch.forecast, symbol: 'circle', symbolSize: 6, lineStyle: { width: 2.5, type: 'dashed', color: PALETTE[2] }, itemStyle: { color: PALETTE[2] } },
        ],
      });
      o.grid.top = 44;
      return o;
    },
  };

  /* =========================================================
     SIMPAN KE DB
     ========================================================= */
  async function saveDashboard() {
    if (!state.dashboard || state.savedId) return;
    const btn = $('#saveDbBtn');
    btn.disabled = true; btn.textContent = 'Menyimpan…';

    const name = $('#nameInput').value.trim() || state.dashboard.title || 'Dashboard';
    const fd = new FormData();
    fd.append('payload', JSON.stringify({
      name,
      dashboard: { ...state.dashboard, title: name },
      file_ids: Array.from(state.selectedIds),
    }));
    state.localFiles.forEach((f) => fd.append('files', f, f.name));

    try {
      const data = await api('/api/dashboards', { method: 'POST', body: fd });
      state.savedId = data.dashboard_id;
      state.dashboard.title = name;
      toast('Dashboard tersimpan.', 'ok');
      loadSaved();
    } catch (e) {
      toast(e.message, 'error');
    }
    updateSaveButton();
  }

  async function renameIfSaved() {
    const name = $('#nameInput').value.trim();
    if (!state.savedId || !name || name === state.dashboard.title) return;
    try {
      await api(`/api/dashboards/${state.savedId}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
      });
      state.dashboard.title = name;
      const h1 = document.querySelector('#dashboardCanvas h1');
      if (h1) h1.textContent = name;
      loadSaved();
    } catch (e) { toast(e.message, 'error'); }
  }

  /* =========================================================
     EXPORT PNG / PDF (hanya #dashboardCanvas)
     ========================================================= */
  async function captureCanvas() {
    if (typeof html2canvas === 'undefined') throw new Error('Library export belum termuat. Periksa koneksi internet.');
    const node = $('#dashboardCanvas');
    document.body.classList.add('exporting');
    state.charts.forEach((c) => c.chart.resize());
    await sleep(500);
    try {
      if (document.fonts && document.fonts.ready) await document.fonts.ready;
      const h = node.scrollHeight;
      const scale = Math.max(1, Math.min(2, 15000 / h));
      const canvas = await html2canvas(node, {
        scale, useCORS: true, logging: false,
        backgroundColor: getComputedStyle(node).backgroundColor,
        windowWidth: Math.max(1320, document.documentElement.clientWidth),
      });
      return { canvas, scale };
    } finally {
      document.body.classList.remove('exporting');
      state.charts.forEach((c) => c.chart.resize());
    }
  }

  const fileBase = () => {
    const t = ($('#nameInput').value || 'dashboard').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'dashboard';
    return `${t}-${new Date().toISOString().slice(0, 10)}`;
  };

  function download(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  async function exportAs(kind) {
    const btn = kind === 'png' ? $('#savePngBtn') : $('#savePdfBtn');
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = 'Memproses…';
    try {
      const { canvas, scale } = await captureCanvas();
      if (kind === 'png') {
        await new Promise((resolve, reject) => canvas.toBlob((b) => {
          if (!b) return reject(new Error('Gagal membuat gambar.'));
          download(b, fileBase() + '.png'); resolve();
        }, 'image/png'));
      } else {
        const jsPDF = window.jspdf && window.jspdf.jsPDF;
        if (!jsPDF) throw new Error('Library PDF belum termuat. Periksa koneksi internet.');
        const w = Math.round(canvas.width / scale), h = Math.round(canvas.height / scale);
        const pdf = new jsPDF({ orientation: w > h ? 'l' : 'p', unit: 'px', format: [w, h], hotfixes: ['px_scaling'] });
        pdf.addImage(canvas.toDataURL('image/jpeg', 0.92), 'JPEG', 0, 0, w, h);
        pdf.save(fileBase() + '.pdf');
      }
      toast('Dashboard berhasil diunduh.', 'ok');
    } catch (e) {
      console.error(e);
      toast(e.message || 'Gagal mengunduh dashboard.', 'error');
    } finally {
      btn.disabled = false; btn.textContent = label;
    }
  }

  /* =========================================================
     INIT
     ========================================================= */
  function init() {
    initTheme();
    initDropzone();
    $('#historySearch').addEventListener('input', renderHistory);
    $('#generateBtn').addEventListener('click', generate);
    $('#backBtn').addEventListener('click', () => {
      disposeCharts();
      state.dashboard = null; state.savedId = null;
      showView('Start');
      loadSaved();
    });
    $('#saveDbBtn').addEventListener('click', saveDashboard);
    $('#savePngBtn').addEventListener('click', () => exportAs('png'));
    $('#savePdfBtn').addEventListener('click', () => exportAs('pdf'));
    $('#nameInput').addEventListener('change', renameIfSaved);
    updateGenerateBar();
    loadHistory();
    loadSaved();
  }

  document.addEventListener('DOMContentLoaded', init);
})();

document
    .getElementById("logoutButton")
    .addEventListener(
        "click",
        async function() {

            await fetch(
                "/api/logout",
                {
                    method: "POST"
                }
            );

            window.location.href =
                "/login";

        }
    );
