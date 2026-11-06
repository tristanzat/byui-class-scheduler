'use strict';

const START_HOUR = 7, END_HOUR = 22, HOUR_PX = 44;
const DAYS = ['M', 'T', 'W', 'R', 'F', 'S'];
const DAY_NAMES = { M: 'Mon', T: 'Tue', W: 'Wed', R: 'Thu', F: 'Fri', S: 'Sat' };
const COLORS = ['#1f5fae', '#2e8b57', '#b5651d', '#8a3ab9', '#c2185b', '#00838f', '#6d6d00', '#5d4037', '#37474f', '#d84315'];
const STORE_KEY = 'byui-scheduler-selected';
const PANEL_KEY = 'byui-scheduler-panel-collapsed';
const RESULT_LIMIT = 60;

const $ = id => document.getElementById(id);
let sections = [], byCode = new Map(), courses = [], professors = {};
let selected = new Set(JSON.parse(localStorage.getItem(STORE_KEY) || '[]'));
let hovered = null;

const fmtTime = m => {
  const h = Math.floor(m / 60), mm = String(m % 60).padStart(2, '0');
  return `${((h + 11) % 12) + 1}:${mm}${h < 12 ? 'a' : 'p'}`;
};
const fmtHour = h => `${((h + 11) % 12) + 1}${h < 12 ? ' AM' : ' PM'}`;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const timed = m => m.days && m.start_min != null && m.end_min != null;

function meetingText(m) {
  if (!timed(m)) return 'No set meeting time';
  return `${m.days} ${fmtTime(m.start_min)}–${fmtTime(m.end_min)}` + (m.room ? ` · ${esc(m.room)}` : '');
}

function meetingsConflict(a, b) {
  if (!timed(a) || !timed(b)) return false;
  if (a.start_date && b.start_date && (a.end_date < b.start_date || b.end_date < a.start_date)) return false;
  if (a.start_min >= b.end_min || b.start_min >= a.end_min) return false;
  return [...a.days].some(d => b.days.includes(d));
}
function sectionsConflict(a, b) {
  return a.meetings.some(m => b.meetings.some(n => meetingsConflict(m, n)));
}
const conflictsWithSelection = s =>
  [...selected].some(c => c !== s.code && byCode.has(c) && sectionsConflict(s, byCode.get(c)));

function colorFor(code) {
  const key = byCode.get(code).subject + byCode.get(code).number;
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return COLORS[h % COLORS.length];
}

function save() { localStorage.setItem(STORE_KEY, JSON.stringify([...selected])); }
function toggle(code) {
  selected.has(code) ? selected.delete(code) : selected.add(code);
  save();
  renderResults();
  renderCalendar();
}

function buildIndex(data) {
  sections = data.sections;
  professors = data.professors || {};
  const map = new Map();
  for (const s of sections) {
    byCode.set(s.code, s);
    const key = s.subject + ' ' + s.number;
    if (!map.has(key)) map.set(key, { key, subject: s.subject, number: s.number, title: s.title, sections: [] });
    map.get(key).sections.push(s);
  }
  courses = [...map.values()].sort((a, b) =>
    a.subject.localeCompare(b.subject) || a.number.localeCompare(b.number, undefined, { numeric: true }));
  for (const c of courses) {
    const names = c.sections.map(s => s.instructor || '').join(' ');
    c.search = `${c.subject} ${c.number} ${c.subject}${c.number} ${c.title} ${names}`.toLowerCase();
  }

  const subjects = [...new Set(courses.map(c => c.subject))].sort();
  $('subject').insertAdjacentHTML('beforeend', subjects.map(s => `<option>${esc(s)}</option>`).join(''));
  const deliveries = new Map(sections.map(s => [s.delivery, s.delivery_label]));
  $('delivery').insertAdjacentHTML('beforeend',
    [...deliveries].map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join(''));
  $('termLabel').textContent = `Term ${sections[0]?.term.replace(';', ' ')} · ${sections.length} sections · ${courses.length} courses`;
  selected = new Set([...selected].filter(c => byCode.has(c)));
}

function instructorHtml(s) {
  if (!s.instructor) return '';
  return s.instructor.split('; ').map(n => {
    const [last, first = ''] = n.split(', ');
    const p = professors[n];
    const rating = p && p.num_ratings > 0
      ? ` <span class="tag"><a href="https://www.ratemyprofessors.com/professor/${p.rmp_id}" target="_blank" rel="noopener"
           title="${esc(p.rmp_name)} · difficulty ${p.difficulty ?? '?'} · ${p.num_ratings} ratings${p.match === 'exact' || p.match === 'manual' ? '' : ' · name match is approximate'}">★ ${p.rating.toFixed(1)}${p.match === 'exact' || p.match === 'manual' ? '' : '?'}</a></span>` : '';
    return `${esc((first.split(' ')[0] + ' ' + last).trim())}${rating}`;
  }).join(', ');
}

function statusHtml(s) {
  if (!s.status) return '';
  const cls = s.status === 'Open' || s.status === 'Reopened' ? 'ok' : s.status === 'Closed' ? 'warn' : 'wait';
  const seats = s.seats_total != null ? ` ${s.seats_open}/${s.seats_total}` : '';
  return `<span class="tag ${cls}">${esc(s.status)}${seats}</span>`;
}

function renderResults() {
  const terms = $('q').value.toLowerCase().split(/\s+/).filter(Boolean);
  const subj = $('subject').value, del = $('delivery').value, hide = $('hideConflicts').checked;
  const hideClosed = $('hideClosed').checked;
  const openKeys = new Set([...$('results').querySelectorAll('details[open]')].map(d => d.dataset.key));
  const scroll = $('results').scrollTop;

  const matches = [];
  for (const c of courses) {
    if (subj && c.subject !== subj) continue;
    if (!terms.every(t => c.search.includes(t))) continue;
    let secs = c.sections;
    if (del) secs = secs.filter(s => s.delivery === del);
    if (hideClosed) secs = secs.filter(s => s.status !== 'Closed' || selected.has(s.code));
    if (hide) secs = secs.filter(s => selected.has(s.code) || !conflictsWithSelection(s));
    if (secs.length) matches.push({ c, secs });
  }

  $('count').textContent = `${matches.length} course${matches.length === 1 ? '' : 's'}` +
    (matches.length > RESULT_LIMIT ? ` (showing first ${RESULT_LIMIT} — refine your search)` : '');
  const autoOpen = matches.length <= 5;

  $('results').innerHTML = matches.slice(0, RESULT_LIMIT).map(({ c, secs }) => `
<details class="course" data-key="${esc(c.key)}" ${autoOpen || openKeys.has(c.key) ? 'open' : ''}>
  <summary><span><span class="cn">${esc(c.key)}</span> ${esc(c.title)}</span>
    <span class="meta">${secs.length} sec</span></summary>
  ${secs.map(s => {
    const sel = selected.has(s.code), bad = !sel && conflictsWithSelection(s);
    return `<div class="sec ${sel ? 'selected' : ''}" data-code="${esc(s.code)}">
      <span class="code">${esc(s.section)}</span>
      <span class="info">
        ${s.instructor ? `<div class="instr">${instructorHtml(s)}</div>` : ''}
        ${s.meetings.map(m => `<div>${meetingText(m)}</div>`).join('') || '<div>No meetings</div>'}
        <div>${esc(s.delivery_label)} · ${s.credits} cr ${statusHtml(s)}
          ${s.meetings.some(m => m.start_date && m.end_date && (m.start_date !== '2027-01-06' || m.end_date !== '2027-04-08'))
        ? `<span class="tag">${esc(s.meetings[0].start_date)} → ${esc(s.meetings[0].end_date)}</span>` : ''}
          ${bad ? '<span class="tag warn">conflict</span>' : ''}</div>
      </span>
      <button type="button" class="add ${sel ? 'rm' : ''}"
        aria-label="${sel ? 'Remove' : 'Add'} ${esc(s.code)}">${sel ? 'Remove' : 'Add'}</button>
    </div>`;
  }).join('')}
</details>`).join('') || '<div class="empty">No matching classes.</div>';
  $('results').scrollTop = scroll;
}

function layoutDay(events) {
  // Greedy column packing for overlapping events within one day.
  events.sort((a, b) => a.start - b.start || b.end - a.end);
  const groups = [];
  let cur = null;
  for (const e of events) {
    if (!cur || e.start >= cur.end) { cur = { end: e.end, cols: [], items: [] }; groups.push(cur); }
    let col = cur.cols.findIndex(end => end <= e.start);
    if (col < 0) { col = cur.cols.length; cur.cols.push(0); }
    cur.cols[col] = e.end;
    e.col = col; cur.items.push(e);
    cur.end = Math.max(cur.end, e.end);
  }
  for (const g of groups) for (const e of g.items) e.ncols = g.cols.length;
}

function renderCalendar() {
  const chosen = [...selected].map(c => byCode.get(c)).filter(Boolean);
  const events = Object.fromEntries(DAYS.map(d => [d, []]));
  const untimed = [];

  const add = (s, ghost) => {
    let any = false;
    for (const m of s.meetings) {
      if (!timed(m)) continue;
      any = true;
      for (const d of m.days) if (events[d]) events[d].push({ s, m, start: m.start_min, end: m.end_min, ghost });
    }
    return any;
  };
  for (const s of chosen) if (!add(s, false)) untimed.push(s);
  if (hovered && !selected.has(hovered.code)) add(hovered, true);

  let minH = START_HOUR, maxH = END_HOUR;
  for (const list of Object.values(events)) for (const e of list) {
    minH = Math.min(minH, Math.floor(e.start / 60));
    maxH = Math.max(maxH, Math.ceil(e.end / 60));
  }
  const total = (maxH - minH) * HOUR_PX;

  // Mark real (non-ghost) events that overlap another selected event.
  for (const list of Object.values(events)) {
    const real = list.filter(e => !e.ghost);
    for (const e of real) e.conflict = real.some(o => o !== e && o.s !== e.s && meetingsConflict(e.m, o.m));
  }

  let html = '<div class="dayhead" style="border-left:0"></div>' +
    DAYS.map(d => `<div class="dayhead">${DAY_NAMES[d]}</div>`).join('');
  html += `<div class="timecol" style="height:${total}px">` +
    Array.from({ length: maxH - minH }, (_, i) =>
      `<span class="lbl" style="top:${i * HOUR_PX}px;${i === 0 ? 'transform:none' : ''}">${fmtHour(minH + i)}</span>`).join('') + '</div>';
  for (const d of DAYS) {
    layoutDay(events[d]);
    html += `<div class="daycol" style="height:${total}px">` + events[d].map(e => {
      const top = (e.start / 60 - minH) * HOUR_PX, h = (e.end - e.start) / 60 * HOUR_PX;
      const w = 100 / e.ncols;
      return `<div class="ev ${e.conflict ? 'conflict' : ''} ${e.ghost ? 'ghost' : ''}" data-code="${esc(e.s.code)}"
    ${e.ghost ? 'aria-hidden="true"' : 'role="button" tabindex="0"'}
    style="top:${top}px;height:${h - 1}px;left:calc(${e.col * w}% + 1px);width:calc(${w}% - 2px);background:${colorFor(e.s.code)}"
    title="${esc(e.s.code + ' ' + e.s.title)}&#10;${fmtTime(e.start)}–${fmtTime(e.end)} ${esc(e.m.room || '')}">
    <b>${esc(e.s.subject + ' ' + e.s.number + '-' + e.s.section)}</b>
    ${h > 34 ? esc(e.s.title) : ''}${h > 62 ? `<br>${fmtTime(e.start)}–${fmtTime(e.end)}` : ''}${h > 48 && e.m.room ? `<br>${esc(e.m.room)}` : ''}
  </div>`;
    }).join('') + '</div>';
  }
  $('grid').innerHTML = html;

  const credits = chosen.reduce((n, s) => n + (s.credits || 0), 0);
  $('summary').innerHTML = chosen.length
    ? chosen.map(s => `<span class="chip" style="background:${colorFor(s.code)}">${esc(s.code)}
    <button type="button" data-code="${esc(s.code)}" aria-label="Remove ${esc(s.code)}">×</button></span>`).join('') +
    `<span class="total">${credits} credits</span><button type="button" class="clear" id="clearAll">Clear all</button>`
    : '<span class="hint">No classes selected — search on the left and click a section to add it.</span>';

  $('noTime').innerHTML = untimed.length
    ? '<b>No set meeting time:</b> ' + untimed.map(s => `${esc(s.code)} (${esc(s.title)}, ${esc(s.delivery_label)})`).join('; ')
    : '';
}

function setupPanelToggle() {
  const apply = collapsed => {
    $('layout').classList.toggle('collapsed', collapsed);
    $('togglePanel').textContent = collapsed ? 'Show classes' : 'Hide classes';
    $('togglePanel').setAttribute('aria-expanded', String(!collapsed));
    localStorage.setItem(PANEL_KEY, collapsed ? '1' : '');
  };
  apply(!!localStorage.getItem(PANEL_KEY));
  $('togglePanel').addEventListener('click', () => apply(!$('layout').classList.contains('collapsed')));
}

function init() {
  $('results').addEventListener('click', e => {
    if (e.target.closest('a')) return;
    const row = e.target.closest('.sec');
    if (row) toggle(row.dataset.code);
  });
  $('results').addEventListener('mouseover', e => {
    // Touch browsers emulate mouseover on tap, which would leave a stuck ghost preview.
    if (!matchMedia('(hover: hover)').matches) return;
    const row = e.target.closest('.sec');
    const s = row ? byCode.get(row.dataset.code) : null;
    if (s !== hovered) { hovered = s; renderCalendar(); }
  });
  $('results').addEventListener('mouseleave', () => { if (hovered) { hovered = null; renderCalendar(); } });
  $('summary').addEventListener('click', e => {
    if (e.target.id === 'clearAll') { selected.clear(); save(); renderResults(); renderCalendar(); }
    else if (e.target.dataset.code) toggle(e.target.dataset.code);
  });
  $('grid').addEventListener('click', e => {
    const ev = e.target.closest('.ev:not(.ghost)');
    if (ev) toggle(ev.dataset.code);
  });
  $('grid').addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const ev = e.target.closest('.ev:not(.ghost)');
    if (!ev) return;
    e.preventDefault();
    toggle(ev.dataset.code);
  });
  // Autofocus would pop the on-screen keyboard on touch devices.
  if (matchMedia('(hover: hover)').matches) $('q').focus();
  let timer;
  $('q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(renderResults, 120); });
  for (const id of ['subject', 'delivery', 'hideConflicts', 'hideClosed']) $(id).addEventListener('change', renderResults);
}

setupPanelToggle();
fetch('sections.json')
  .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(data => { buildIndex(data); init(); renderResults(); renderCalendar(); })
  .catch(err => {
    $('results').innerHTML = `<div class="empty">Could not load sections.json (${esc(err.message)}).<br>
  Open this page through a web server: <code>python serve.py</code></div>`;
  });
