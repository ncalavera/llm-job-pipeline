const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
export function sentMaterials(rows) {
  return rows.filter(r => r.status === 'sent' && ['cv', 'cover_letter', 'answers', 'test'].includes(r.kind));
}
export function matchingStatements(rows, query, topic = '') {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return rows.filter(r => (!topic || r.topic === topic) && words.every(w => `${r.text} ${r.topic} ${r.kind}`.toLowerCase().includes(w)));
}
const kinds = {cv: 'CV', cover_letter: 'Cover letter', answers: 'Answers', test: 'Test response'};

async function start() {
  const host = document.getElementById('list');
  const counter = document.getElementById('count');
  const search = document.getElementById('search');
  const topic = document.getElementById('topic');
  const params = new URLSearchParams(location.search);
  const vacancy = params.get('vacancy');
  let mode = vacancy ? 'sent' : 'statements';
  let originals, statements;
  try {
    const responses = await Promise.all([fetch('/api/materials'), fetch('/api/materials?view=statements')]);
    if (responses.some(r => !r.ok)) throw new Error('load');
    [originals, statements] = await Promise.all(responses.map(r => r.json()));
  } catch {
    counter.textContent = 'Could not load your library. Please reload to try again.';
    return;
  }
  for (const name of [...new Set(statements.map(r => r.topic))].sort()) {
    const option = el('option', name); option.value = name; topic.append(option);
  }
  function originalLink(id, label = 'Read source') {
    const a = el('a', label); a.href = '/api/materials?id=' + encodeURIComponent(id); return a;
  }
  function fileCard(row) {
    const card = el('details', null, 'card');
    card.append(el('summary', row.title || row.filename));
    card.append(el('p', `${kinds[row.kind] || 'Supporting record'}${row.date ? ' · ' + row.date : ''}`, 'meta'));
    if (row.evidence) card.append(el('p', 'Submission evidence: ' + row.evidence));
    card.append(el('p', 'Source: ' + row.source, 'source'));
    if (row.text) card.append(el('pre', row.text));
    card.append(originalLink(row.id, 'Download original'));
    return card;
  }
  function render() {
    host.replaceChildren();
    document.getElementById('statementsTab').setAttribute('aria-pressed', String(mode === 'statements'));
    document.getElementById('sentTab').setAttribute('aria-pressed', String(mode === 'sent'));
    topic.parentElement.hidden = mode !== 'statements';
    if (mode === 'statements') {
      const selected = matchingStatements(statements, search.value, topic.value);
      counter.textContent = `${selected.length} statements · ${selected.filter(r => r.review === 'conflict').length} need your decision`;
      const scroll = el('div', null, 'table-scroll');
      const table = el('table');
      const head = el('tr');
      for (const label of ['Topic', 'Type', 'Statement', 'Sources / review']) head.append(el('th', label));
      const thead = el('thead'); thead.append(head); table.append(thead);
      const body = el('tbody');
      for (const row of selected) {
        const tr = el('tr'); tr.append(el('td', row.topic), el('td', row.kind));
        const text = el('td', row.text);
        if (row.note) text.append(el('p', row.note, 'meta'));
        tr.append(text);
        const sources = el('td');
        sources.append(el('strong', row.review === 'conflict' ? 'Resolve before reuse' : 'Previously recorded'));
        const detail = el('details'); detail.append(el('summary', `${row.sources.length} source${row.sources.length === 1 ? '' : 's'}`));
        for (const ref of row.sources) {
          const source = originals.find(r => r.id === ref.material_id);
          detail.append(el('blockquote', ref.quote), originalLink(ref.material_id, source?.filename || 'Read source'));
        }
        sources.append(detail); tr.append(sources); body.append(tr);
      }
      table.append(body); scroll.append(table); host.append(scroll);
    } else {
      const selected = sentMaterials(originals).filter(r => (!vacancy || r.vacancy_id === vacancy) && `${r.organisation} ${r.application_title || ''} ${r.title || r.filename} ${r.text}`.toLowerCase().includes(search.value.toLowerCase()));
      const groups = new Map();
      for (const row of selected) {
        const key = row.vacancy_id || row.organisation;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(row);
      }
      counter.textContent = `${groups.size} applications with identified submissions`;
      if (!selected.length) host.append(el('p', 'No exact submitted materials identified for this selection yet. Originals remain in the source archive below.'));
      for (const items of groups.values()) {
        const section = el('section');
        section.append(el('h2', items[0].employer || items[0].organisation.replaceAll('-', ' ')));
        section.append(el('p', items[0].application_title || 'Application'));
        if (items[0].vacancy_id) {
          const link = el('a', 'Open application'); link.href = '/?vacancy=' + encodeURIComponent(items[0].vacancy_id); section.append(link);
        }
        for (const row of items) section.append(fileCard(row));
        host.append(section);
      }
    }
    // Originals are evidence for the two catalogues, not a third primary catalogue.
    const archive = el('details', null, 'archive');
    archive.append(el('summary', 'Source archive — originals, drafts and earlier versions'));
    archive.addEventListener('toggle', () => {
      if (!archive.open || archive.children.length > 1) return;
      for (const row of originals.filter(r => !vacancy || r.vacancy_id === vacancy)) archive.append(fileCard(row));
    });
    host.append(archive);
  }
  document.getElementById('statementsTab').onclick = () => {mode = 'statements'; render();};
  document.getElementById('sentTab').onclick = () => {mode = 'sent'; render();};
  search.oninput = render; topic.onchange = render; render();
}
if (typeof document !== 'undefined') start();
