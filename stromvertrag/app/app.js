const $ = (selector) => document.querySelector(selector);
const box = $('#contracts');
const form = $('#form');
let editing = null;
let items = [];
let locale = 'de';
let today = new Date().toISOString().slice(0, 10);
let readingDraft = {};
let readingChanges = {};
let previewTimer;
let previewSequence = 0;

const euro = (value) => value == null ? '–' : new Intl.NumberFormat(locale, { style: 'currency', currency: 'EUR' }).format(value);
const euroPerKwh = (value) => value == null ? '–' : new Intl.NumberFormat(locale, { style: 'currency', currency: 'EUR', minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(value) + '/kWh';
const kwh = (value) => value == null ? '–' : new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value) + ' kWh';

function displayDate(iso) {
  if (!iso) return '';
  const [year, month, day] = iso.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return new Intl.DateTimeFormat(locale, { day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'UTC' }).format(date);
}

function dayAfter(iso) {
  return new Date(Date.parse(iso + 'T00:00:00Z') + 86400000).toISOString().slice(0, 10);
}

function node(tag, content = '', className = '') {
  const element = document.createElement(tag);
  element.textContent = content;
  element.className = className;
  return element;
}

function notice(message) {
  $('#notice').textContent = message;
  $('#notice').classList.toggle('hidden', !message);
}

async function api(path, method = 'GET', body) {
  const response = await fetch('./api/' + path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Stromvertrag' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'Anfrage fehlgeschlagen');
  return data;
}

const ICON_PATHS = {
  edit: 'M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zm17.71-10.04a1 1 0 0 0 0-1.41l-2.5-2.5a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.99-1.67z',
  delete: 'M16 9v10H8V9h8m-1.5-6h-5l-1 1H5v2h14V4h-3.5l-1-1zM18 7H6v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7z',
  check: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1 14.2-4-4 1.4-1.4 2.6 2.6 4.6-4.6 1.4 1.4-6 6z',
  warning: 'M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z',
  estimate: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-5 9.5c1.5-2.5 3.2-2.5 5 0s3.5 2.5 5 0v2c-1.5 2.5-3.2 2.5-5 0s-3.5-2.5-5 0v-2z',
};

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICON_PATHS[name]);
  svg.append(path);
  return svg;
}

function button(label, handler, kind = 'secondary') {
  const element = node('button', label, kind);
  element.type = 'button';
  element.onclick = handler;
  return element;
}

function iconButton(name, label, handler, kind = 'secondary') {
  const element = button('', handler, kind + ' icon-button');
  element.setAttribute('aria-label', label);
  element.title = label;
  element.append(icon(name));
  return element;
}

function statusIcon(status) {
  const kind = status === 'Zählerstände' ? 'manual'
    : status === 'Statistik' ? 'statistics'
    : status.includes('geschätzt') ? 'estimated'
    : status.includes('unvollständig') ? 'partial' : 'missing';
  const element = node('button', '', 'status-indicator ' + kind);
  element.type = 'button';
  element.setAttribute('aria-label', status);
  element.title = status;
  element.onclick = () => notice(status);
  element.append(icon(kind === 'manual' || kind === 'statistics' ? 'check' : kind === 'estimated' ? 'estimate' : 'warning'));
  return element;
}

function fact(label, value) {
  const wrapper = node('div', '', 'fact');
  wrapper.append(node('dt', label), node('dd', value));
  return wrapper;
}

function renderReadings() {
  const list = $('#reading-list');
  const start = form.elements.namedItem('start').value;
  const end = form.elements.namedItem('end').value;
  list.replaceChildren();
  for (const [day, value] of Object.entries(readingDraft).sort(([a], [b]) => a.localeCompare(b))) {
    if (day === start || (end && day === dayAfter(end))) continue;
    const row = node('li');
    row.append(node('span', displayDate(day) + ' · ' + kwh(Number(value))));
    row.append(button('Entfernen', () => {
      delete readingDraft[day];
      readingChanges[day] = null;
      renderReadings();
    }, 'danger'));
    list.append(row);
  }
}

function openEditor(item) {
  editing = item?.id || null;
  readingDraft = { ...(item?.readings || {}) };
  readingChanges = {};
  $('#editor-title').textContent = item ? 'Vertrag bearbeiten' : 'Vertrag anlegen';
  form.reset();
  $('#reading-editor').classList.remove('hidden');
  $('#reading-date').value = '';
  $('#reading-value').value = '';
  if (item) {
    const select = form.elements.namedItem('entity_id');
    if (item.entity_id && ![...select.options].some((option) => option.value === item.entity_id)) {
      const option = node('option', item.entity_id);
      option.value = item.entity_id;
      select.append(option);
    }
    for (const [key, value] of Object.entries(item)) {
      const field = form.elements.namedItem(key);
      if (field) field.value = value;
    }
    form.elements.namedItem('start_reading').value = item.readings?.[item.start] ?? '';
    form.elements.namedItem('end_reading').value = item.readings?.[dayAfter(item.end)] ?? '';
  }
  renderReadings();
  form.elements.namedItem('start').onchange = () => {
    if (item && form.elements.namedItem('start').value !== item.start) form.elements.namedItem('start_reading').value = '';
    renderReadings();
  };
  form.elements.namedItem('end').onchange = () => {
    if (item && form.elements.namedItem('end').value !== item.end) form.elements.namedItem('end_reading').value = '';
    renderReadings();
  };
  $('#editor').classList.remove('hidden');
  schedulePreview();
  $('#editor').scrollIntoView({ behavior: 'smooth' });
}

function renderPreview(data) {
  const content = $('#preview-content');
  content.replaceChildren();
  const grid = node('dl', '', 'preview-grid');
  for (const [label, value, className] of [
    ['Grundgebühr', euro(data.base)],
    ['Verbrauchskosten', euro(data.energy)],
    ['Brutto', euro(data.gross)],
    ['Boni', '− ' + euro(data.bonus)],
    ['Effektiv', euro(data.effective), 'effective'],
    ['Effektiv je kWh', euroPerKwh(data.effective_per_kwh)],
  ]) {
    const cell = node('div', '', className || '');
    cell.append(node('dt', label), node('dd', value));
    grid.append(cell);
  }
  content.append(grid);
}

function schedulePreview() {
  clearTimeout(previewTimer);
  const sequence = ++previewSequence;
  const raw = Object.fromEntries(new FormData(form));
  if (!raw.start || !raw.end || !raw.base_monthly || !raw.price_kwh || !raw.contract_kwh) {
    $('#preview-content').textContent = 'Laufzeit, Grundgebühr, Arbeitspreis und kWh-Menge eingeben.';
    return;
  }
  if (raw.end < raw.start) {
    $('#preview-content').textContent = 'Das Vertragsende muss nach dem Beginn liegen.';
    return;
  }
  previewTimer = setTimeout(async () => {
    try {
      const data = await api('preview', 'POST', raw);
      if (sequence === previewSequence) renderPreview(data);
    } catch (error) {
      if (sequence === previewSequence) $('#preview-content').textContent = error.message;
    }
  }, 180);
}

function render() {
  box.replaceChildren();
  if (!items.length) {
    box.append(node('div', 'Noch keine Verträge. Lege den ersten Vertrag an.', 'card empty'));
    return;
  }
  for (const contract of items) {
    const card = node('section', '', 'card');
    const head = node('div', '', 'contract-head');
    const heading = node('div');
    const title = node('div', '', 'contract-title');
    title.append(node('h2', contract.provider + ' · ' + contract.tariff));
    const finished = contract.end < today;
    title.append(node('span', contract.start > today ? 'Geplant' : finished ? 'Abgeschlossen' : 'Aktiv', 'contract-state'));
    heading.append(title, node('p', contract.entity_id || 'Manuelle Ablesungen', 'muted'));
    const actions = node('div', '', 'contract-actions');
    actions.append(iconButton('edit', 'Vertrag bearbeiten', () => openEditor(contract)));
    actions.append(iconButton('delete', 'Vertrag löschen', async () => {
      if (!confirm('Vertrag und Ablesungen löschen?')) return;
      try { await api('contracts/' + contract.id, 'DELETE'); await load(); }
      catch (error) { notice(error.message); }
    }, 'danger'));
    head.append(heading, actions);
    card.append(head);

    const facts = node('dl', '', 'contract-facts');
    facts.append(
      fact('Laufzeit', displayDate(contract.start) + ' – ' + displayDate(contract.end)),
      fact('Vereinbarte Menge', kwh(Number(contract.contract_kwh))),
      fact('Grundgebühr', euro(Number(contract.base_monthly)) + ' / Monat'),
      fact('Arbeitspreis', euroPerKwh(Number(contract.price_kwh))),
    );
    card.append(facts);

    const tableWrap = node('div', '', 'tablewrap');
    const table = node('table');
    const header = node('tr');
    for (const label of ['Jahr / Zeitraum', 'Verbrauch', 'Grundgebühr', 'Arbeitspreis', 'Brutto', 'Bonus', 'Effektiv', 'Datenlage']) header.append(node('th', label));
    const thead = node('thead'); thead.append(header); table.append(thead);
    const tbody = node('tbody');
    let total = 0, totalKwh = 0, hasTotal = false, complete = true, estimated = false;
    for (const year of contract.years) {
      const row = node('tr');
      for (const value of [String(year.year) + ' · ' + displayDate(year.from) + ' – ' + displayDate(year.to), kwh(year.kwh), euro(year.base), euro(year.energy), euro(year.gross), euro(year.bonus), euro(year.net)]) row.append(node('td', value));
      const statusCell = node('td'); statusCell.append(statusIcon(year.status)); row.append(statusCell);
      tbody.append(row);
      if (year.net != null) { total += year.net; totalKwh += year.kwh; hasTotal = true; }
      if (year.kwh == null || year.status.includes('unvollständig')) complete = false;
      if (year.status.includes('geschätzt')) estimated = true;
    }
    table.append(tbody); tableWrap.append(table); card.append(tableWrap);

    const suffix = (complete ? '' : ' (lückenhaft)') + (estimated ? ' (Jahresanteile geschätzt)' : '');
    const summary = node('div', '', 'summary');
    for (const [label, value] of [
      [finished ? 'Verbrauch gesamt' : 'Verbrauch bisher', hasTotal ? kwh(totalKwh) : '–'],
      [finished ? 'Effektive Gesamtkosten' : 'Effektive Kosten bisher', hasTotal ? euro(total) : '–'],
    ]) {
      const paragraph = node('p');
      paragraph.append(node('strong', label), node('span', value + suffix, 'summary-value'));
      summary.append(paragraph);
    }
    card.append(summary);
    if (!contract.years.length) card.append(node('p', 'Dieser Vertrag beginnt in der Zukunft.', 'muted'));
    box.append(card);
  }
}

$('#reading-date').onchange = () => { $('#reading-value').value = readingDraft[$('#reading-date').value] ?? ''; };
$('#reading-add').onclick = () => {
  const day = $('#reading-date').value;
  const value = $('#reading-value').value;
  const start = form.elements.namedItem('start').value;
  const end = form.elements.namedItem('end').value;
  if (!day || !value) { notice('Ablesedatum und Zählerstand eingeben.'); return; }
  if (!start || !end || day <= start || day >= dayAfter(end)) { notice('Weitere Ablesungen müssen innerhalb der Vertragslaufzeit liegen. Die Grenzstände stehen oben im Formular.'); return; }
  if (!Number.isFinite(Number(value)) || Number(value) < 0) { notice('Ungültiger Zählerstand.'); return; }
  readingDraft[day] = value;
  readingChanges[day] = value;
  renderReadings();
  notice('Ablesung vorgemerkt. Bitte den Vertrag speichern.');
};
$('#new').onclick = () => openEditor(null);
$('#cancel').onclick = () => { $('#editor').classList.add('hidden'); ++previewSequence; clearTimeout(previewTimer); };
form.addEventListener('input', schedulePreview);
form.addEventListener('change', schedulePreview);
form.onsubmit = async (event) => {
  event.preventDefault();
  try {
    const body = Object.fromEntries(new FormData(form));
    body.reading_changes = readingChanges;
    await api(editing ? 'contracts/' + editing : 'contracts', editing ? 'PUT' : 'POST', body);
    $('#editor').classList.add('hidden');
    ++previewSequence; clearTimeout(previewTimer);
    notice('Vertrag gespeichert.');
    await load();
  } catch (error) { notice(error.message); }
};

async function load() {
  try { items = await api('report'); render(); }
  catch (error) { notice(error.message); }
}

async function entities() {
  try {
    const list = await api('entities');
    const select = form.elements.namedItem('entity_id');
    for (const sensor of list) {
      const option = node('option', sensor.name + ' (' + sensor.id + ', ' + sensor.unit + ')');
      option.value = sensor.id;
      select.append(option);
    }
  } catch (error) { notice(error.message); }
}

async function settings() {
  try {
    const config = await api('settings');
    locale = String(config.language || 'de').replace('_', '-');
    new Intl.DateTimeFormat(locale);
    today = config.today || today;
    document.documentElement.lang = locale;
  } catch { locale = 'de'; }
}

async function start() {
  await settings();
  await Promise.all([entities(), load()]);
}
start();
