/* LabWatcher UI. Vanilla JS, no build step. One global `LW` with helpers + per-page init. */
(function () {
  'use strict';
  const CONTEXTS = ['drug_discovery', 'materials_discovery'];
  const CTX_LABEL = { drug_discovery: 'Drug discovery', materials_discovery: 'Materials discovery' };
  const TAXONOMY_LABEL = {
    interlock_bypass: 'Interlock bypass', record_tampering: 'Record tampering', data_fabrication: 'Data fabrication',
    unapproved_substitution: 'Unapproved substitution', hazard_release: 'Hazard release',
    infrastructure_disruption: 'Infrastructure disruption', sample_integrity: 'Sample integrity',
    scope_overreach: 'Scope overreach', prompt_injection: 'Prompt injection',
  };
  const TAXONOMY_IDS = Object.keys(TAXONOMY_LABEL);

  // ---- context ---------------------------------------------------------------------------
  function ctx() {
    const q = new URLSearchParams(location.search).get('context');
    if (CONTEXTS.includes(q)) return q;
    const s = localStorage.getItem('lw.context');
    return CONTEXTS.includes(s) ? s : 'drug_discovery';
  }
  function setCtx(c) {
    localStorage.setItem('lw.context', c);
    const url = new URL(location.href); url.searchParams.set('context', c); history.replaceState(null, '', url);
    document.querySelectorAll('.ctx-switch button').forEach(b => b.classList.toggle('on', b.dataset.ctx === c));
    document.querySelectorAll('.sidebar nav a').forEach(a => { const u = new URL(a.href); u.searchParams.set('context', c); a.href = u.pathname + u.search; });
    document.dispatchEvent(new CustomEvent('lw:context', { detail: c }));
  }

  // ---- utils -----------------------------------------------------------------------------
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  async function api(path, opts) {
    const o = Object.assign({ headers: {} }, opts || {});
    if (o.body && typeof o.body !== 'string') { o.body = JSON.stringify(o.body); o.headers['Content-Type'] = 'application/json'; }
    const r = await fetch(path, o);
    let data = null; try { data = await r.json(); } catch (e) { /* empty */ }
    if (!r.ok) { const err = new Error((data && (data.detail || JSON.stringify(data.errors))) || r.statusText); err.status = r.status; err.data = data; throw err; }
    return data;
  }
  function fmtDate(iso) { if (!iso) return '—'; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }
  function ago(iso) { if (!iso) return ''; const s = (Date.now() - new Date(iso)) / 1000; if (s < 60) return Math.round(s) + 's ago'; if (s < 3600) return Math.round(s / 60) + 'm ago'; if (s < 86400) return Math.round(s / 3600) + 'h ago'; return Math.round(s / 86400) + 'd ago'; }
  function pct(x) { return (100 * (x || 0)).toFixed(0) + '%'; }
  function sevVar(score) { const n = Math.max(1, Math.min(10, Math.round(score || 1))); return `var(--sev-${n})`; }
  function scoreChip(score) {
    if (score == null) return '<span class="score none" title="rule decided; no model score">—</span>';
    return `<span class="score" style="background:${sevVar(score)}" title="risk score ${score}/10">${score}</span>`;
  }
  function decisionChip(d) {
    const glyph = { allow: '✓', deny: '✕', escalate: '!', escalate_triage: '!', escalate_human: '!' }[d] || '·';
    return `<span class="chip decision-${esc(d)}">${glyph} ${esc(d)}</span>`;
  }
  function statusDot(s) { return `<span class="status-dot ${esc(s)}"></span>${esc(s)}`; }
  function cats(list) { return (list || []).map(c => `<span class="cat" title="${esc(TAXONOMY_LABEL[c] || c)}">${esc(c)}</span>`).join(''); }
  function notice(el, msg, kind) { el.innerHTML = msg ? `<div class="notice ${kind || ''}">${esc(msg)}</div>` : ''; }

  // ---- tooltip ---------------------------------------------------------------------------
  let tip;
  function tooltipOn(root) {
    if (!tip) { tip = document.createElement('div'); tip.className = 'tooltip'; document.body.appendChild(tip); }
    root.addEventListener('mousemove', e => {
      const t = e.target.closest('[data-tip]'); if (!t) { tip.style.display = 'none'; return; }
      tip.innerHTML = t.dataset.tip; tip.style.display = 'block';
      tip.style.left = Math.min(window.innerWidth - 280, e.clientX + 12) + 'px'; tip.style.top = (e.clientY + 12) + 'px';
    });
    root.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  }

  // ---- charts (inline SVG) -------------------------------------------------------------------
  function hbars(items) {
    // items: [{id,label,count}] -> horizontal bars, thin marks, labels in text tokens, 2px gap
    const w = 520, rowH = 22, labelW = 190, max = Math.max(1, ...items.map(i => i.count));
    const h = items.length * rowH + 4;
    let s = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Failures by category">`;
    items.forEach((it, i) => {
      const y = i * rowH + 2, bw = Math.round((w - labelW - 40) * it.count / max);
      s += `<text class="label" x="${labelW - 8}" y="${y + 14}" text-anchor="end">${esc(it.label)}</text>`;
      s += `<rect class="bar ${it.count ? '' : 'zero'}" x="${labelW}" y="${y + 3}" width="${Math.max(it.count ? 4 : 2, bw)}" height="${rowH - 8}" rx="3" data-tip="${esc(it.label)}: <b>${it.count}</b>"></rect>`;
      s += `<text class="value" x="${labelW + Math.max(it.count ? 4 : 2, bw) + 6}" y="${y + 14}">${it.count}</text>`;
    });
    return s + '</svg>';
  }
  function sparkline(points, key, opts) {
    // points: [{day, sessions, flagged, blocked}] -> single-series line with area, hover dots
    opts = opts || {}; const w = 520, h = 90, padL = 28, padR = 8, padT = 10, padB = 20;
    const max = Math.max(1, ...points.map(p => p[key] || 0));
    const xs = i => padL + (w - padL - padR) * (points.length > 1 ? i / (points.length - 1) : 0.5);
    const ys = v => padT + (h - padT - padB) * (1 - v / max);
    const path = points.map((p, i) => `${i ? 'L' : 'M'}${xs(i).toFixed(1)},${ys(p[key] || 0).toFixed(1)}`).join(' ');
    const area = `${path} L${xs(points.length - 1).toFixed(1)},${ys(0)} L${xs(0).toFixed(1)},${ys(0)} Z`;
    let s = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(opts.label || key)} per day">`;
    [0, max].forEach(v => { s += `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${ys(v)}" y2="${ys(v)}"></line><text class="label" x="${padL - 6}" y="${ys(v) + 4}" text-anchor="end">${v}</text>`; });
    s += `<path class="area" d="${area}"></path><path class="line ${esc(opts.cls || '')}" d="${path}"></path>`;
    points.forEach((p, i) => {
      if (i === 0 || i === points.length - 1 || i % 3 === 0) s += `<text class="label" x="${xs(i)}" y="${h - 4}" text-anchor="middle">${esc(p.day.slice(5))}</text>`;
      s += `<circle class="marker" cx="${xs(i)}" cy="${ys(p[key] || 0)}" r="${p[key] ? 3 : 0}"></circle>`;
      s += `<rect class="hit" x="${xs(i) - (w - padL - padR) / points.length / 2}" y="0" width="${(w - padL - padR) / points.length}" height="${h}" data-tip="${esc(p.day)}<br>${esc(opts.label || key)}: <b>${p[key] || 0}</b><br>sessions: ${p.sessions || 0}, flagged: ${p.flagged || 0}, blocked: ${p.blocked || 0}"></rect>`;
    });
    return s + '</svg>';
  }
  function radar(scores) {
    // nine-axis radar of trailing scores (1-10); polygon stroked in accent, fill soft
    const size = 240, c = size / 2, r = 86, n = TAXONOMY_IDS.length;
    const pt = (i, v) => { const a = -Math.PI / 2 + 2 * Math.PI * i / n; return [c + Math.cos(a) * r * v / 10, c + Math.sin(a) * r * v / 10]; };
    let s = `<svg viewBox="0 0 ${size} ${size}" role="img" aria-label="Trailing monitor radar">`;
    [2, 4, 6, 8, 10].forEach(v => { s += `<polygon class="grid" fill="none" points="${TAXONOMY_IDS.map((_, i) => pt(i, v).join(',')).join(' ')}"></polygon>`; });
    s += `<polygon class="grid" fill="none" stroke="var(--serious)" stroke-dasharray="3 3" points="${TAXONOMY_IDS.map((_, i) => pt(i, 7).join(',')).join(' ')}"></polygon>`;
    TAXONOMY_IDS.forEach((id, i) => {
      const [x, y] = pt(i, 10), [lx, ly] = pt(i, 12.6);
      s += `<line class="axis" x1="${c}" y1="${c}" x2="${x}" y2="${y}"></line>`;
      s += `<text class="label" x="${lx}" y="${ly + 3}" text-anchor="middle" style="font-size:9px">${esc(TAXONOMY_LABEL[id].split(' ')[0])}</text>`;
    });
    s += `<polygon fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="2" points="${TAXONOMY_IDS.map((id, i) => pt(i, scores[id] || 1).join(',')).join(' ')}"></polygon>`;
    TAXONOMY_IDS.forEach((id, i) => { const [x, y] = pt(i, scores[id] || 1); s += `<circle cx="${x}" cy="${y}" r="4" fill="${sevVar(scores[id] || 1)}" data-tip="${esc(TAXONOMY_LABEL[id])}: <b>${scores[id] || 1}</b>/10"></circle>`; });
    return s + '</svg>';
  }
  function sparkbars(scores) {
    return `<span class="sparkbars">${TAXONOMY_IDS.map(id => { const v = scores[id] || 1; return `<i style="height:${v * 10}%;background:${sevVar(v)}" data-tip="${esc(TAXONOMY_LABEL[id])}: <b>${v}</b>/10"></i>`; }).join('')}</span>`;
  }

  // ---- shell ----------------------------------------------------------------------------------
  function initShell() {
    const sw = $('#ctx-switch');
    if (sw) {
      sw.innerHTML = CONTEXTS.map(c => `<button data-ctx="${c}">${CTX_LABEL[c]}</button>`).join('');
      sw.addEventListener('click', e => { const b = e.target.closest('button'); if (b) setCtx(b.dataset.ctx); });
    }
    const page = document.body.dataset.page;
    $$('.sidebar nav a').forEach(a => a.classList.toggle('active', a.dataset.nav === page));
    api('/api/health').then(h => { const f = $('#backend'); if (f) f.innerHTML = `store: <b>${esc(h.backend)}</b>${h.demo_available ? '' : ' · demo runner not installed'}${h.warnings.length ? `<div class="muted" title="${esc(h.warnings.join('\n'))}">${h.warnings.length} warning(s)</div>` : ''}`; }).catch(() => {});
    setCtx(ctx());
    tooltipOn(document.body);
  }

  // ---- page: analyzer ------------------------------------------------------------------------
  function initAnalyzer() {
    const state = { sort: 'date', order: 'desc', status: '', min_score: '', flagged: '', since: '' };
    const table = $('#sessions'), tiles = $('#tiles'), catsEl = $('#by-category'), trendEl = $('#trend');
    let trendKey = 'flagged', summary = null;
    async function load() {
      const c = ctx();
      const [sum, list] = await Promise.all([
        api(`/api/summary?context=${c}`),
        api(`/api/sessions?context=${c}&sort=${state.sort}&order=${state.order}` + (state.status ? `&status=${state.status}` : '') + (state.min_score ? `&min_score=${state.min_score}` : '') + (state.flagged ? `&flagged=${state.flagged}` : '') + (state.since ? `&since=${state.since}` : '')),
      ]);
      summary = sum;
      tiles.innerHTML = [
        ['Total sessions', sum.total_sessions, `${sum.running_sessions} running`],
        ['Blocked actions', sum.blocked_actions, `${sum.escalated_actions} escalated`],
        ['Flagged sessions', sum.flagged_sessions, 'deny / escalate / trailing ≥ 7'],
        ['Failure rate', pct(sum.failure_rate), 'flagged / total sessions', sum.failure_rate >= 0.5 ? 'critical' : ''],
      ].map(([l, v, s, cls]) => `<div class="panel tile"><div class="label">${l}</div><div class="value ${cls || ''}">${v}</div><div class="sub">${s}</div></div>`).join('');
      catsEl.innerHTML = hbars(sum.by_category.map(b => ({ id: b.id, label: TAXONOMY_LABEL[b.id] || b.id, count: b.count })));
      drawTrend();
      renderTable(list.sessions);
      $('#count').textContent = `${list.count} session${list.count === 1 ? '' : 's'}`;
    }
    function drawTrend() {
      if (!summary) return;
      trendEl.innerHTML = sparkline(summary.trend, trendKey, { label: trendKey, cls: trendKey === 'flagged' ? 'flagged' : '' });
      $$('#trend-keys button').forEach(b => b.classList.toggle('primary', b.dataset.key === trendKey));
    }
    function renderTable(rows) {
      const head = `<thead><tr>${[['severity', 'Score'], ['status', 'Status'], ['date', 'Started'], ['env', 'Env / card'], ['', 'Outcome'], ['blocked', 'Blocked'], ['', 'Escalated'], ['', 'Model']].map(([k, l]) => `<th class="${k ? 'sortable' : ''} ${state.sort === k ? 'sorted' : ''}" data-sort="${k}">${l}${state.sort === k ? (state.order === 'desc' ? ' ↓' : ' ↑') : ''}</th>`).join('')}</tr></thead>`;
      const body = rows.length ? rows.map(r => `<tr class="row-link" data-id="${esc(r.id)}">
        <td>${scoreChip(r.max_score)}</td><td class="nowrap">${statusDot(r.status)}${r.flagged ? ' <span class="chip decision-deny" title="flagged">flag</span>' : ''}</td>
        <td class="nowrap" title="${esc(r.started_at)}">${fmtDate(r.started_at)}</td>
        <td><b>${esc(r.env)}</b> <span class="muted mono">${esc(r.card || '')}</span><div class="small muted">${esc(r.card_title || '')}</div></td>
        <td>${esc(r.outcome || '—')}<div class="small muted">${esc(r.arm || '')}</div></td>
        <td class="right">${r.blocked_count || 0}</td><td class="right">${r.escalated_count || 0}</td><td class="muted small">${esc(r.model || '')}</td></tr>`).join('') : `<tr><td colspan="8" class="empty">No sessions match.</td></tr>`;
      table.innerHTML = head + `<tbody>${body}</tbody>`;
    }
    table.addEventListener('click', e => {
      const th = e.target.closest('th.sortable'); if (th) { if (state.sort === th.dataset.sort) state.order = state.order === 'desc' ? 'asc' : 'desc'; else { state.sort = th.dataset.sort; state.order = 'desc'; } load(); return; }
      const tr = e.target.closest('tr.row-link'); if (tr) location.href = `/session/${encodeURIComponent(tr.dataset.id)}?context=${ctx()}`;
    });
    $('#filters').addEventListener('change', e => { state[e.target.name] = e.target.value; load(); });
    $('#trend-keys').addEventListener('click', e => { const b = e.target.closest('button'); if (b) { trendKey = b.dataset.key; drawTrend(); } });
    document.addEventListener('lw:context', load);
    load();
  }

  // ---- page: live ----------------------------------------------------------------------------------
  function initLive() {
    const feed = $('#feed'), pend = $('#pending'), status = $('#live-status'), jobsEl = $('#jobs');
    let es = null, catalog = null, lastId = 0, seen = new Set();
    function actionCard(a, compact) {
      return `<div class="action ${esc(a.decision)}"><div class="head">${decisionChip(a.decision)}${scoreChip(a.score)}<span class="chip stage">${esc(a.stage)}</span>
        <span class="call">${esc(a.tool)}${a.instrument ? `(${esc(a.instrument)}.${esc(a.command)})` : a.path ? `(${esc(a.path)})` : ''}</span>
        ${a.rule_id ? `<span class="muted mono small">rule:${esc(a.rule_id)}</span>` : ''}
        <span class="spacer" style="flex:1"></span><a class="small" href="/session/${encodeURIComponent(a.session_id)}?context=${ctx()}">${esc(a.env || '')} ${esc(a.card || '')} · ${ago(a.ts)}</a></div>
        ${compact ? '' : `<div class="reason small">${esc(a.reason)}</div>`}${a.categories && a.categories.length ? `<div>${cats(a.categories)}</div>` : ''}</div>`;
    }
    function renderPending(list) {
      $('#pending-count').textContent = list.length;
      pend.innerHTML = list.length ? list.map(a => `<div class="action escalate" data-id="${a.id}">
        <div class="head">${scoreChip(a.score)}<span class="call">${esc(a.tool)}${a.instrument ? `(${esc(a.instrument)}.${esc(a.command)})` : ''}</span><span class="chip stage">${esc(a.stage)}</span>${a.rule_id ? `<span class="muted mono small">rule:${esc(a.rule_id)}</span>` : ''}</div>
        <div class="reason">${esc(a.reason)}</div><div>${cats(a.categories)}</div>
        <div class="small muted" style="margin:4px 0">${esc(a.env)} · ${esc(a.card || '')} · <a href="/session/${encodeURIComponent(a.session_id)}?context=${ctx()}">${esc(a.session_id)}</a> · waiting ${ago(a.ts)}</div>
        ${a.evaluator ? `<details><summary>evaluator JSON</summary><pre>${esc(JSON.stringify(a.evaluator, null, 2))}</pre></details>` : ''}
        <div class="toolbar" style="margin-top:8px"><input name="note" placeholder="note for the record (optional)" style="flex:1"><button class="ok" data-decision="approve">Approve</button><button class="danger" data-decision="deny">Deny</button></div></div>`).join('') : '<div class="empty">No pending escalations.</div>';
    }
    pend.addEventListener('click', async e => {
      const b = e.target.closest('button[data-decision]'); if (!b) return;
      const card = b.closest('.action'); const note = $('input[name=note]', card).value;
      b.disabled = true;
      try { const r = await api(`/api/escalations/${card.dataset.id}`, { method: 'POST', body: { decision: b.dataset.decision, note } }); renderPending(r.pending); notice(status, `Action ${card.dataset.id}: ${r.decision} recorded`, 'ok'); }
      catch (err) { notice(status, err.message, 'error'); b.disabled = false; }
    });
    function connect() {
      if (es) es.close();
      feed.innerHTML = ''; seen = new Set(); lastId = 0;
      es = new EventSource(`/api/live/events?context=${ctx()}&after=0`);
      es.addEventListener('action', ev => { const a = JSON.parse(ev.data); if (seen.has(a.id)) return; seen.add(a.id); lastId = Math.max(lastId, a.id); feed.insertAdjacentHTML('afterbegin', actionCard(a, false)); while (feed.children.length > 80) feed.lastChild.remove(); });
      es.addEventListener('escalations', ev => renderPending(JSON.parse(ev.data)));
      es.addEventListener('hello', ev => notice(status, `Live · ${CTX_LABEL[ctx()]} · streaming from ${JSON.parse(ev.data).backend} store`, 'ok'));
      es.addEventListener('error', ev => { if (ev.data) notice(status, JSON.parse(ev.data).message, 'error'); });
      es.onerror = () => notice(status, 'Stream disconnected, retrying…', 'warn');
    }
    // demo runner
    const envSel = $('#demo-env'), cardSel = $('#demo-card');
    function fillEnvs() {
      if (!catalog) return;
      const envs = catalog[ctx()].envs;
      envSel.innerHTML = Object.keys(envs).map(e => `<option value="${esc(e)}">${esc(e)}</option>`).join('');
      fillCards();
    }
    function fillCards() { const cards = catalog[ctx()].envs[envSel.value] || []; cardSel.innerHTML = cards.map(c => `<option value="${esc(c.id)}">${esc(c.id)} — ${esc(c.title)}</option>`).join(''); }
    envSel.addEventListener('change', fillCards);
    async function loadJobs() {
      try {
        const j = await api('/api/demo/jobs');
        $('#demo-run').disabled = !j.demo_available;
        $('#demo-note').textContent = j.demo_available ? '' : 'labwatcher.demo.run_demo not installed yet; the run button is disabled.';
        jobsEl.innerHTML = j.jobs.length ? j.jobs.slice(0, 8).map(x => `<div class="item"><span class="status-dot ${x.status === 'done' ? 'finished' : x.status}"></span>${esc(x.env)} ${esc(x.card)} <b>${esc(x.script)}</b> <span class="muted">${esc(x.provider)}</span> ${x.session_id ? `→ <a href="/session/${encodeURIComponent(x.session_id)}?context=${ctx()}">${esc(x.session_id)}</a>` : ''}${x.error ? `<div class="small" style="color:#ff9a9a">${esc(x.error)}</div>` : ''}</div>`).join('') : '';
        if (j.jobs.some(x => x.status === 'running')) setTimeout(loadJobs, 2000);
      } catch (e) { /* ignore */ }
    }
    $('#demo-form').addEventListener('submit', async e => {
      e.preventDefault();
      const body = { context: ctx(), env: envSel.value, card: cardSel.value, script: $('#demo-script').value, provider: $('#demo-provider').value };
      try { await api('/api/demo/run', { method: 'POST', body }); notice($('#demo-status'), `Started ${body.script} run on ${body.env}/${body.card}`, 'ok'); loadJobs(); }
      catch (err) { notice($('#demo-status'), err.message, err.status === 501 ? 'warn' : 'error'); }
    });
    api('/api/catalog').then(c => { catalog = c.catalog; fillEnvs(); });
    document.addEventListener('lw:context', () => { connect(); fillEnvs(); });
    connect(); loadJobs();
  }

  // ---- page: session ----------------------------------------------------------------------------------
  function initSession() {
    const id = decodeURIComponent(location.pathname.split('/').pop());
    api(`/api/sessions/${encodeURIComponent(id)}`).then(d => {
      const s = d.session; setCtx(s.context || ctx());
      $('#session-title').innerHTML = `${esc(s.env)} <span class="muted">/</span> ${esc(s.card || '')} <span class="muted small">${esc(s.card_title || '')}</span>`;
      $('#session-meta').innerHTML = `<dl class="kv"><dt>Session</dt><dd class="mono">${esc(s.id)}</dd><dt>Status</dt><dd>${statusDot(s.status)} ${s.flagged ? '<span class="chip decision-deny" title="flagged: deny / escalate / score at or above threshold">flagged</span>' : ''}</dd>
        <dt>Outcome</dt><dd>${esc(s.outcome || '—')}</dd><dt>Max score</dt><dd>${scoreChip(s.max_score)}</dd><dt>Blocked / escalated</dt><dd>${s.blocked_count || 0} / ${s.escalated_count || 0}</dd>
        <dt>Condition · arm</dt><dd>${esc(s.condition || '—')} · ${esc(s.arm || '—')}</dd><dt>Model</dt><dd>${esc(s.model || '—')}</dd><dt>Started</dt><dd>${fmtDate(s.started_at)}${s.ended_at ? ' → ' + fmtDate(s.ended_at) : ''}</dd><dt>Source</dt><dd>${esc(s.source || '—')}</dd></dl>`;
      // timeline: merge transcript + actions by ts; hide the "[tool call]" echo when the action is present
      const human = {}; d.human_decisions.forEach(h => { human[h.action_id] = h; });
      const items = [];
      d.transcript.forEach(t => { if (!(d.actions.length && /^\[tool call\]/.test(t.content || ''))) items.push({ k: 'msg', ts: t.ts, o: t.idx, v: t }); });
      d.actions.forEach(a => items.push({ k: 'act', ts: a.ts, o: a.seq, v: a }));
      items.sort((x, y) => (x.ts || '').localeCompare(y.ts || '') || x.o - y.o);
      $('#timeline').innerHTML = items.length ? items.map(it => it.k === 'msg'
        ? `<div class="msg ${esc(it.v.role)}"><div class="role">${esc(it.v.role)} <span>#${it.v.idx}</span><span class="muted">${fmtDate(it.v.ts)}</span></div><pre>${esc(it.v.content)}</pre></div>`
        : actionBlock(it.v, human[it.v.id])).join('') : '<div class="empty">No transcript recorded.</div>';
      // right rail
      const tr = d.trailing;
      $('#trailing').innerHTML = tr.length ? `<div class="chart" id="radar">${radar(tr[tr.length - 1].scores)}</div><div class="small muted" style="text-align:center">latest window · dashed ring = suggestion threshold 7</div>
        <table style="margin-top:8px"><thead><tr><th>At action</th><th>Nine categories</th><th>Top</th></tr></thead><tbody>${tr.map(t => `<tr><td class="mono">#${t.at_action}</td><td>${sparkbars(t.scores)}</td><td>${scoreChip(t.max_score)} <span class="small">${esc(TAXONOMY_LABEL[t.top_category] || t.top_category || '')}</span></td></tr>
        <tr><td></td><td colspan="2" class="small muted">${esc(t.reason)}${t.suggestion ? `<details><summary>suggestion injected</summary><pre>${esc(t.suggestion)}</pre></details>` : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No trailing reviews yet.</div>';
      $('#human').innerHTML = d.human_decisions.length ? d.human_decisions.map(h => `<div class="item">${decisionChip(h.decision === 'approve' ? 'allow' : h.decision)} <span class="mono small">action ${h.action_id}</span> <span class="muted small">${fmtDate(h.ts)}</span>${h.note ? `<div class="small">${esc(h.note)}</div>` : ''}</div>`).join('') : '<div class="empty">No human decisions.</div>';
      const en = d.enrichment;
      $('#enrichment').innerHTML = en.length ? en.map(e => `<div class="item"><div class="m"><span class="badge source">${esc(e.source)}</span> <span class="mono">${esc(e.query)}</span></div>
        ${(Array.isArray(e.result) ? e.result : (e.result && e.result.items) || []).map(r => `<div style="margin-top:4px">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || r.name || r.url)}</a>` : esc(r.title || r.name || JSON.stringify(r))} <span class="muted small">${esc([r.kind, r.year, r.assignee, r.modality].filter(Boolean).join(' · '))}</span></div>`).join('')}</div>`).join('') : '<div class="empty">No Amass enrichment for this session.</div>';
    }).catch(err => { $('#timeline').innerHTML = `<div class="notice error">${esc(err.message)}</div>`; });
    function actionBlock(a, h) {
      return `<div class="action ${esc(a.decision)}" id="action-${a.id}"><div class="head">${decisionChip(a.decision)}${scoreChip(a.score)}<span class="chip stage" title="pipeline stage">${esc(a.stage)}</span>
        <span class="call">#${a.seq} ${esc(a.tool)}(${esc(a.instrument ? `${a.instrument}.${a.command}` : a.path || '')})</span>
        ${a.rule_id ? `<span class="muted mono small">rule:${esc(a.rule_id)}</span>` : ''}<span class="muted small">${a.latency_ms} ms</span>
        ${h ? `<span class="chip neutral">human: ${esc(h.decision)}</span>` : ''}</div>
        <div class="reason">${esc(a.reason)}</div>${a.categories.length ? `<div style="margin-top:4px">${cats(a.categories)}</div>` : ''}
        <details><summary>args · result · triage / evaluator JSON</summary><pre>${esc(JSON.stringify({ args: a.args, result: a.result, ok: a.ok, triage: a.triage, evaluator: a.evaluator }, null, 2))}</pre></details></div>`;
    }
  }

  // ---- page: policy -----------------------------------------------------------------------------------
  function initPolicy() {
    const KEYS = ['triage_system', 'evaluator_system', 'trailing_system', 'suggestion_template'];
    const form = $('#policy-form'), status = $('#policy-status');
    async function load() {
      const p = await api(`/api/policy/${ctx()}`);
      $('#policy-path').innerHTML = `<span class="mono">${esc(p.path)}</span> ${p.exists ? '' : '<span class="badge additions_allowed">not on disk yet — showing defaults</span>'}`;
      KEYS.forEach(k => { $(`textarea[name=${k}]`, form).value = p.policy[k] || ''; });
    }
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const body = {}; KEYS.forEach(k => { body[k] = $(`textarea[name=${k}]`, form).value; });
      try { await api(`/api/policy/${ctx()}`, { method: 'POST', body }); notice(status, `Saved policies/${ctx()}.yaml`, 'ok'); load(); }
      catch (err) { notice(status, err.message, 'error'); }
    });
    $('#policy-reload').addEventListener('click', () => { notice(status, ''); load(); });
    document.addEventListener('lw:context', () => { notice(status, ''); load(); });
    load();
  }

  // ---- page: rules -------------------------------------------------------------------------------------
  function initRules() {
    const table = $('#rules-table'), form = $('#rule-form'), status = $('#rules-status'), testOut = $('#rule-test-out');
    let rules = [];
    async function load() {
      const r = await api(`/api/rules/${ctx()}`); rules = r.rules;
      $('#rules-path').innerHTML = `<span class="mono">${esc(r.path)}</span> ${r.exists ? '' : '<span class="badge additions_allowed">not on disk yet — showing built-in fallback</span>'} · ${r.count} rules`;
      $('select[name=decision]', form).innerHTML = r.decisions.map(d => `<option>${d}</option>`).join('');
      table.innerHTML = `<thead><tr><th>Priority</th><th>ID</th><th>Decision</th><th>Match (regex)</th><th>Reason</th><th></th></tr></thead><tbody>${rules.map(x => `<tr data-id="${esc(x.id)}">
        <td class="right mono">${x.priority}</td><td class="mono">${esc(x.id)}</td><td>${decisionChip(x.decision)}</td>
        <td>${Object.entries(x.match || {}).map(([k, v]) => `<div><span class="muted small">${k}</span> <code>${esc(v)}</code></div>`).join('')}</td>
        <td class="small">${esc(x.reason || '')}</td><td class="nowrap"><button data-act="edit">Edit</button> <button data-act="delete" class="danger">Delete</button></td></tr>`).join('')}</tbody>`;
    }
    function fill(x) {
      $('input[name=id]', form).value = x.id || ''; $('input[name=priority]', form).value = x.priority == null ? 50 : x.priority; $('select[name=decision]', form).value = x.decision || 'allow';
      ['tool', 'command', 'path', 'args'].forEach(k => { $(`input[name=match_${k}]`, form).value = (x.match || {})[k] || ''; });
      $('input[name=reason]', form).value = x.reason || ''; form.dataset.editing = x.id || ''; $('#rule-submit').textContent = x.id ? `Save ${x.id}` : 'Add rule';
    }
    table.addEventListener('click', async e => {
      const b = e.target.closest('button[data-act]'); if (!b) return; const id = b.closest('tr').dataset.id;
      if (b.dataset.act === 'edit') { fill(rules.find(r => r.id === id)); form.scrollIntoView({ behavior: 'smooth' }); }
      else if (confirm(`Delete rule ${id}?`)) { try { await api(`/api/rules/${ctx()}/${encodeURIComponent(id)}`, { method: 'DELETE' }); notice(status, `Deleted ${id}`, 'ok'); load(); } catch (err) { notice(status, err.message, 'error'); } }
    });
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const body = { id: $('input[name=id]', form).value.trim(), priority: Number($('input[name=priority]', form).value), decision: $('select[name=decision]', form).value, reason: $('input[name=reason]', form).value, match: {} };
      ['tool', 'command', 'path', 'args'].forEach(k => { const v = $(`input[name=match_${k}]`, form).value; if (v) body.match[k] = v; });
      const editing = form.dataset.editing;
      try {
        if (editing) await api(`/api/rules/${ctx()}/${encodeURIComponent(editing)}`, { method: 'PUT', body }); else await api(`/api/rules/${ctx()}`, { method: 'POST', body });
        notice(status, `${editing ? 'Saved' : 'Added'} ${body.id}`, 'ok'); fill({}); load();
      } catch (err) { const errs = err.data && err.data.errors; notice(status, errs ? Object.entries(errs).map(([k, v]) => `${k}: ${v}`).join(' · ') : err.message, 'error'); }
    });
    $('#rule-reset').addEventListener('click', () => fill({}));
    $('#rule-test').addEventListener('submit', async e => {
      e.preventDefault(); const f = e.target; const q = new URLSearchParams({ tool: f.tool.value, command: f.command.value, path: f.path.value });
      const r = await api(`/api/rules/${ctx()}/test?${q}`);
      testOut.innerHTML = r.winner ? `winner: ${decisionChip(r.winner.decision)} <span class="mono">${esc(r.winner.id)}</span> (priority ${r.winner.priority}) · ${r.matches.length} match(es) · <span class="muted">${esc(r.winner.reason || '')}</span>` : '<span class="muted">no rule matches — falls through to triage</span>';
    });
    document.addEventListener('lw:context', () => { fill({}); load(); });
    fill({}); load();
  }

  // ---- page: settings ----------------------------------------------------------------------------------
  function initSettings() {
    api('/api/settings').then(v => {
      const msgs = [];
      (v.errors || []).forEach(e => msgs.push(`<div class="notice error">error: ${esc(e)}</div>`));
      (v.warnings || []).concat(v.ui_warnings || []).forEach(w => msgs.push(`<div class="notice warn">${esc(w)}</div>`));
      $('#settings-messages').innerHTML = msgs.join('') || '<div class="notice ok">Settings loaded without errors.</div>';
      $('#settings-source').innerHTML = `source: <span class="badge source">${esc(v.source)}</span> · store backend: <b>${esc(v.backend)}</b>${(v.layers || []).length ? `<div class="small muted" style="margin-top:4px">layers: ${v.layers.map(esc).join(' → ')}</div>` : ''}`;
      const eff = v.effective || {}, locks = v.locks || {};
      const lockOf = k => { const l = locks[k]; return typeof l === 'string' ? l : (l && (l.status || l.permission || l.mode)) || 'modifiable'; };
      $('#settings-tree').innerHTML = Object.entries(eff).filter(([k]) => !['permissions', 'locks', '_permissions'].includes(k)).map(([k, val]) => `<div class="section"><h3>${esc(k)} <span class="badge ${esc(lockOf(k))}">${esc(lockOf(k).replace('_', ' '))}</span></h3>${renderVal(val, k)}</div>`).join('');
      function renderVal(val, prefix) {
        if (val && typeof val === 'object' && !Array.isArray(val)) {
          return `<table><tbody>${Object.entries(val).map(([k, v]) => `<tr><td>${esc(k)}${locks[`${prefix}.${k}`] ? ` <span class="badge ${esc(lockOf(prefix + '.' + k))}">${esc(lockOf(prefix + '.' + k))}</span>` : ''}</td><td>${v && typeof v === 'object' ? `<pre>${esc(JSON.stringify(v))}</pre>` : `<code>${esc(JSON.stringify(v))}</code>`}</td></tr>`).join('')}</tbody></table>`;
        }
        return `<code>${esc(JSON.stringify(val))}</code>`;
      }
    }).catch(err => { $('#settings-messages').innerHTML = `<div class="notice error">${esc(err.message)}</div>`; });
  }

  window.LW = { ctx, setCtx, api, esc, scoreChip, decisionChip, hbars, sparkline, radar, TAXONOMY_LABEL };
  document.addEventListener('DOMContentLoaded', () => {
    initShell();
    ({ analyzer: initAnalyzer, live: initLive, session: initSession, policy: initPolicy, rules: initRules, settings: initSettings }[document.body.dataset.page] || (() => {}))();
  });
})();
