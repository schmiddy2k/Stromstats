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
let previousBasePeriod = 'yearly';
const collapsedContracts = new Set();

function syncThemeFromHomeAssistant() {
  if (window.parent === window) return;
  try {
    const parentDocument = window.parent.document;
    const themeRoot = parentDocument.querySelector('home-assistant') || parentDocument.documentElement;
    const styles = window.parent.getComputedStyle(themeRoot);
    for (const name of [
      '--primary-color', '--accent-color', '--app-header-background-color', '--app-header-text-color',
      '--primary-background-color', '--secondary-background-color', '--card-background-color',
      '--ha-card-background', '--ha-card-border-radius', '--ha-card-box-shadow',
      '--primary-text-color', '--secondary-text-color', '--text-primary-color',
      '--divider-color', '--input-fill-color', '--error-color', '--warning-color', '--success-color',
    ]) {
      const value = styles.getPropertyValue(name).trim();
      if (value) document.documentElement.style.setProperty(name, value);
      else document.documentElement.style.removeProperty(name);
    }
    const colorScheme = styles.getPropertyValue('color-scheme').trim();
    if (colorScheme) document.documentElement.style.colorScheme = colorScheme;
  } catch {
    // Embedded without same-origin access: use the local theme fallbacks.
  }
}

function watchHomeAssistantTheme() {
  syncThemeFromHomeAssistant();
  if (window.parent === window) return;
  try {
    const parentDocument = window.parent.document;
    const observer = new MutationObserver(syncThemeFromHomeAssistant);
    observer.observe(parentDocument.documentElement, { attributes: true, attributeFilter: ['style', 'class'] });
    const themeRoot = parentDocument.querySelector('home-assistant');
    if (themeRoot) observer.observe(themeRoot, { attributes: true, attributeFilter: ['style', 'class'] });
    window.addEventListener('pagehide', () => observer.disconnect(), { once: true });
    window.addEventListener('focus', syncThemeFromHomeAssistant);
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncThemeFromHomeAssistant);
  } catch {
    // A cross-origin parent cannot expose its theme.
  }
}

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
  expand: 'M7 10l5 5 5-5-1.4-1.4L12 12.2 8.4 8.6 7 10z',
  collapse: 'M7 14l5-5 5 5-1.4 1.4L12 11.8l-3.6 3.6L7 14z',
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

function consumptionInfo(status, description) {
  const kind = status === 'Zählerstände' ? 'manual'
    : status === 'Statistik' ? 'statistics'
    : status.includes('geschätzt') ? 'estimated'
    : status.includes('unvollständig') ? 'partial' : 'missing';
  const details = node('details', '', 'consumption-info');
  const trigger = node('summary');
  trigger.setAttribute('aria-label', 'Hinweis zum Verbrauch: ' + status);
  const symbol = node('span', '', 'status-indicator ' + kind);
  symbol.append(icon(kind === 'manual' || kind === 'statistics' ? 'check' : kind === 'estimated' ? 'estimate' : 'warning'));
  trigger.append(symbol);
  details.append(trigger, node('span', description || status, 'consumption-info-text'));
  return details;
}

function consumptionValue(value, status, description) {
  const wrapper = node('span', '', 'consumption-value');
  wrapper.append(node('span', kwh(value)));
  wrapper.append(consumptionInfo(status, description));
  return wrapper;
}

function aggregate(contract) {
  let total = 0, totalKwh = 0, hasTotal = false, complete = true, estimated = false;
  for (const year of contract.years) {
    if (year.net != null && year.kwh != null) { total += year.net; totalKwh += year.kwh; hasTotal = true; }
    if (year.kwh == null || year.status.includes('unvollständig')) complete = false;
    if (year.status.includes('geschätzt')) estimated = true;
  }
  return { total, totalKwh, hasTotal, complete, estimated };
}

function totalConsumptionInfo(hasTotal, complete, estimated) {
  if (!hasTotal) return 'Für diesen Vertrag liegen noch keine Verbrauchsdaten vor.';
  const details = ['Summe der verfügbaren Jahresverbräuche.'];
  if (estimated) details.push('Jahresanteile ohne passende Grenzstände sind geschätzt.');
  if (!complete) details.push('Messdaten sind lückenhaft; der tatsächliche Verbrauch kann höher sein.');
  return details.join(' ');
}

function yearConsumptionInfo(status) {
  if (status === 'Zählerstände') return 'Verbrauch aus den eingetragenen Zählerständen an beiden Zeitraumgrenzen.';
  if (status === 'Statistik') return 'Verbrauch aus stündlichen Werten der Langzeitstatistik. Das laufende Jahr ist ein Zwischenstand.';
  if (status === 'Statistik (unvollständig)') return 'Stündliche Statistikwerte fehlen teilweise. Der angezeigte Verbrauch kann zu niedrig sein.';
  if (status.includes('geschätzt')) return 'Dieser Jahresanteil wurde aus den Endständen und den verfügbaren Statistikwerten geschätzt.';
  return status;
}

function compactMetric(label, value) {
  const wrapper = node('div', '', 'compact-metric');
  const display = typeof value === 'string' ? node('span', value) : value;
  display.classList.add('compact-value');
  wrapper.append(node('span', label, 'compact-label'), display);
  return wrapper;
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
    form.elements.namedItem('base_period').value = item.base_period || 'monthly';
    form.elements.namedItem('base_fee').value = item.base_fee ?? item.base_monthly;
    form.elements.namedItem('start_reading').value = item.readings?.[item.start] ?? '';
    form.elements.namedItem('end_reading').value = item.readings?.[dayAfter(item.end)] ?? '';
  }
  previousBasePeriod = form.elements.namedItem('base_period').value;
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
  if (!raw.start || !raw.end || !raw.base_fee || !raw.price_kwh || !raw.contract_kwh) {
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
    const { total, totalKwh, hasTotal, complete, estimated } = aggregate(contract);
    const isCollapsed = collapsedContracts.has(contract.id);
    const consumptionStatus = estimated ? 'Aus Endständen geschätzt'
      : !complete ? 'Statistik (unvollständig)'
      : contract.years.every((year) => year.status === 'Zählerstände') ? 'Zählerstände' : 'Statistik';
    const consumptionDescription = totalConsumptionInfo(hasTotal, complete, estimated);
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
      try { await api('contracts/' + contract.id, 'DELETE'); collapsedContracts.delete(contract.id); await load(); }
      catch (error) { notice(error.message); }
    }, 'danger'));
    const toggle = iconButton(isCollapsed ? 'expand' : 'collapse', isCollapsed ? 'Vertrag aufklappen' : 'Vertrag einklappen', () => {
      if (isCollapsed) collapsedContracts.delete(contract.id);
      else collapsedContracts.add(contract.id);
      render();
    });
    toggle.setAttribute('aria-expanded', String(!isCollapsed));
    actions.append(toggle);
    head.append(heading, actions);
    card.append(head);

    if (isCollapsed) {
      const details = node('div', '', 'collapsed-details');
      details.append(
        compactMetric('Verbrauch', consumptionValue(hasTotal ? totalKwh : null, consumptionStatus, consumptionDescription)),
        compactMetric('Effektive Kosten', hasTotal ? euro(total) : '–'),
        compactMetric('Effektiv je kWh', hasTotal && totalKwh > 0 ? euroPerKwh(total / totalKwh) : '–'),
      );
      heading.append(details);
      box.append(card);
      continue;
    }

    const facts = node('dl', '', 'contract-facts');
    facts.append(
      fact('Laufzeit', displayDate(contract.start) + ' – ' + displayDate(contract.end)),
      fact('Vereinbarte Menge', kwh(Number(contract.contract_kwh))),
      fact('Grundgebühr', euro(Number(contract.base_fee ?? contract.base_monthly)) + (contract.base_period === 'yearly' ? ' / Jahr' : ' / Monat')),
      fact('Arbeitspreis', euroPerKwh(Number(contract.price_kwh))),
    );
    card.append(facts);

    const tableWrap = node('div', '', 'tablewrap');
    const table = node('table');
    const header = node('tr');
    for (const [label, explanation] of [
      ['Jahr', 'Vertragszeitraum im Kalenderjahr'], ['Verbrauch', 'Gemessener oder ergänzter Verbrauch'],
      ['Grundpreis', 'Anteilig berechnete Grundgebühr'], ['Arbeit', 'Verbrauchskosten zum Arbeitspreis'],
      ['Brutto', 'Grundpreis plus Verbrauchskosten'], ['Bonus', 'Anteilig zugeordnete Boni'],
      ['Effektiv', 'Brutto abzüglich Boni'],
    ]) {
      const cell = node('th', label);
      cell.title = explanation;
      header.append(cell);
    }
    const thead = node('thead'); thead.append(header); table.append(thead);
    const tbody = node('tbody');
    for (const year of contract.years) {
      const row = node('tr');
      const period = node('td', '', 'period-cell');
      period.append(node('strong', String(year.year)), node('small', displayDate(year.from) + ' – ' + displayDate(year.to)));
      row.append(period);
      const consumptionCell = node('td');
      consumptionCell.append(consumptionValue(year.kwh, year.status, yearConsumptionInfo(year.status)));
      row.append(consumptionCell);
      for (const value of [euro(year.base), euro(year.energy), euro(year.gross), euro(year.bonus), euro(year.net)]) row.append(node('td', value));
      tbody.append(row);
    }
    table.append(tbody); tableWrap.append(table);
    if (contract.years.length) {
      const yearDetails = node('details', '', 'year-details');
      const trigger = node('summary', '', 'year-details-toggle');
      trigger.append(node('span', 'Jahresübersicht'), node('span', contract.years.length + (contract.years.length === 1 ? ' Jahr' : ' Jahre'), 'year-count'), icon('expand'));
      yearDetails.append(trigger, tableWrap);
      card.append(yearDetails);
    }

    const summary = node('div', '', 'summary');
    const paid = contract.paid_installments == null || contract.paid_installments === '' ? null : Number(contract.paid_installments);
    for (const [label, value] of [
      [finished ? 'Verbrauch gesamt' : 'Verbrauch bisher', consumptionValue(hasTotal ? totalKwh : null, consumptionStatus, consumptionDescription)],
      [finished ? 'Effektive Gesamtkosten' : 'Effektive Kosten bisher', hasTotal ? euro(total) : '–'],
      ['Effektive Kosten je kWh', hasTotal && totalKwh > 0 ? euroPerKwh(total / totalKwh) : '–'],
      ['Gezahlte Abschläge', paid == null ? '–' : euro(paid)],
    ]) {
      const paragraph = node('p');
      const display = typeof value === 'string' ? node('span', value, 'summary-value') : value;
      if (typeof value !== 'string') display.classList.add('summary-value');
      paragraph.append(node('strong', label), display);
      summary.append(paragraph);
    }
    const settlement = node('p', '', 'settlement');
    if (paid != null && hasTotal) {
      const difference = Math.round((total - paid) * 100) / 100;
      const label = difference > 0 ? 'Nachzahlung' : difference < 0 ? 'Guthaben' : 'Ausgeglichen';
      const prefix = finished && complete && !estimated ? '' : 'Vorläufig: ';
      settlement.append(node('strong', prefix + label), node('span', euro(Math.abs(difference)), 'summary-value'));
    } else {
      settlement.append(node('strong', 'Nachzahlung / Guthaben'), node('span', '–', 'summary-value'));
    }
    summary.append(settlement);
    card.append(summary);
    if (!contract.years.length) card.append(node('p', 'Dieser Vertrag beginnt in der Zukunft.', 'muted'));
    box.append(card);
  }
}

$('#reading-date').onchange = () => { $('#reading-value').value = readingDraft[$('#reading-date').value] ?? ''; };
$('#reading-editor').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && event.target.matches('input')) {
    event.preventDefault();
    $('#reading-add').click();
  }
});
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
form.elements.namedItem('base_period').addEventListener('change', (event) => {
  const input = form.elements.namedItem('base_fee');
  const next = event.target.value;
  if (next === previousBasePeriod) return;
  if (input.value !== '') {
    const converted = Number(input.value) * (next === 'yearly' ? 12 : 1 / 12);
    input.value = Number(converted.toFixed(next === 'yearly' ? 2 : 6)).toString();
  }
  previousBasePeriod = next;
});
form.addEventListener('input', (event) => { if (!event.target.closest('#reading-editor')) schedulePreview(); });
form.addEventListener('change', (event) => { if (!event.target.closest('#reading-editor')) schedulePreview(); });
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
  watchHomeAssistantTheme();
  await settings();
  await Promise.all([entities(), load()]);
}
start();
