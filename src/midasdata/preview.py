"""Self-contained, paginated previews. Model output only reaches textContent."""

import html
import json
from collections import defaultdict
from pathlib import Path

from midasdata.io import RunStore, atomic_lines
from midasdata.sampler import DIMENSIONS

TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>MidasData · __TITLE__</title>
  <style>
    :root {
      --bg-color: #f8fafc;
      --card-bg: #ffffff;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --border-color: #e2e8f0;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --focus-ring: rgba(59, 130, 246, 0.25);
      --font-sans: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    
    body {
      font-family: var(--font-sans);
      color: var(--text-main);
      background: var(--bg-color);
      max-width: 1400px;
      margin: 0 auto;
      padding: 32px 24px;
      line-height: 1.6;
    }
    
    h1 { 
      font-size: 28px; 
      font-weight: 700;
      letter-spacing: -0.025em;
      margin-bottom: 8px;
    }
    
    p.meta {
      margin-top: 0;
      margin-bottom: 24px;
    }
    
    a { 
      color: var(--primary); 
      text-decoration: none;
      font-weight: 500;
      transition: color 0.2s;
    }
    
    a:hover { 
      color: var(--primary-hover); 
      text-decoration: underline;
    }
    
    nav { 
      margin: 16px 0 24px 0; 
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border-color);
    }
    
    nav a { margin-right: 12px; }
    
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
      padding: 16px;
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      margin-bottom: 24px;
      box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1);
    }
    
    input, select {
      font-family: inherit;
      font-size: 14px;
      padding: 8px 12px;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      background-color: #fff;
      color: var(--text-main);
      transition: all 0.2s;
    }
    
    input:focus, select:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px var(--focus-ring);
    }
    
    input#search { 
      flex: 1; 
      min-width: 240px; 
    }
    
    #dimensions {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
    }
    
    #count {
      margin-left: auto;
      font-weight: 500;
      color: var(--text-muted);
      font-size: 14px;
    }
    
    article {
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      padding: 24px;
      margin: 20px 0;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
      transition: box-shadow 0.2s, transform 0.2s;
    }
    
    article:hover {
      box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.05);
    }
    
    article h2 {
      margin-top: 0;
      font-size: 20px;
      display: flex;
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid var(--border-color);
      padding-bottom: 16px;
      margin-bottom: 16px;
    }
    
    .badge {
      font-size: 12px;
      font-weight: 600;
      background: #dcfce7;
      color: #166534;
      border-radius: 9999px;
      padding: 4px 10px;
      text-transform: uppercase;
      letter-spacing: 0.025em;
    }
    
    .rejected { 
      background: #fee2e2; 
      color: #991b1b; 
    }
    
    .meta { 
      color: var(--text-muted); 
      font-size: 14px; 
    }
    
    .columns {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 20px;
      margin-top: 20px;
    }
    
    .columns section {
      background: #f8fafc;
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 16px;
    }
    
    .text {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.6;
      font-size: 15px;
      color: #334155;
    }
    
    h3 { 
      font-size: 13px; 
      color: var(--text-muted); 
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-top: 0;
      margin-bottom: 12px;
    }
    
    summary { 
      cursor: pointer; 
      margin-top: 20px;
      font-weight: 500;
      color: var(--primary);
      user-select: none;
      display: inline-block;
      transition: color 0.2s;
    }
    
    summary:hover { color: var(--primary-hover); }
    
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #1e293b;
      color: #f8fafc;
      padding: 16px;
      border-radius: 8px;
      font-size: 13px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      max-height: 400px;
      overflow-y: auto;
      margin-top: 12px;
    }
    
    .empty { 
      color: var(--text-muted); 
      text-align: center;
      padding: 48px;
      font-style: italic;
    }
    
    @media (max-width: 900px) {
      .columns { grid-template-columns: 1fr; }
      .toolbar { flex-direction: column; align-items: stretch; }
      input#search { min-width: 100%; }
      #count { margin-left: 0; text-align: right; }
    }
  </style>
</head>
<body>
  <h1>MidasData · __TITLE__</h1>
  <p class="meta">__SCOPE__</p>
  <nav>__NAV__</nav>
  <div class="toolbar">
    <input id="search" placeholder="搜索 ID 或文本" aria-label="搜索">
    <select id="status" aria-label="状态">
      <option value="">所有状态</option>
      <option>passed</option>
      <option>rejected</option>
    </select>
    <select id="origin" aria-label="来源">
      <option value="">所有来源</option>
    </select>
    <span id="dimensions"></span>
    <span id="count" class="meta"></span>
  </div>
  <main id="rows"></main>
  <script type="application/json" id="data">__DATA__</script>
  <script>
    const $ = id => document.getElementById(id);
    const rows = JSON.parse($('data').textContent);
    const filters = {};

    function element(tag, text, cls) {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (cls) node.className = cls;
      return node;
    }

    function origin(row) {
      return row.source.origin || row.source.metadata?.origin || '';
    }

    function options(select, values) {
      for (const value of [...new Set(values)].sort()) {
        const option = element('option', value || '（无关联元数据）');
        option.value = value || '(empty)';
        select.appendChild(option);
      }
    }

    function initializeFilters() {
      options($('origin'), rows.map(origin));
      for (const dimension of __DIMENSIONS__) {
        const select = element('select');
        select.setAttribute('aria-label', dimension);
        const option = element('option', dimension);
        option.value = '';
        select.appendChild(option);
        options(select, rows.map(row => row.source.sample?.[dimension] || ''));
        $('dimensions').appendChild(select);
        filters[dimension] = select;
        select.onchange = render;
      }
      for (const id of ['search', 'status', 'origin']) {
        $(id).addEventListener(id === 'search' ? 'input' : 'change', render);
      }
    }

    function match(value, selected) {
      return !selected || (selected === '(empty)' ? !value : value === selected);
    }

    function filteredRows() {
      const query = $('search').value.toLocaleLowerCase();
      return rows.filter(row =>
        (!query || JSON.stringify(row).toLocaleLowerCase().includes(query)) &&
        match(row.status, $('status').value) &&
        match(origin(row), $('origin').value) &&
        Object.entries(filters).every(([dimension, select]) =>
          match(row.source.sample?.[dimension] || '', select.value)
        )
      );
    }

    function renderLevel(row, level) {
      const column = element('section');
      column.appendChild(element('h3', level));
      const current = row.details.levels?.[level];
      const text = row.records.find(record => record.level === level)?.prompt ||
        current?.prompt || '（无结果）';
      column.appendChild(element('p', text, 'text'));
      if (current) {
        const summary = `生成次数：${current.generation_attempt} · ${current.status} · ` +
          `新增 PIU：${current.decision?.added_count ?? '未校验'}`;
        column.appendChild(element('p', summary, 'meta'));
      }
      return column;
    }

    function renderContent(card, row) {
      if (row.workflow === 'generate') {
        card.appendChild(element('p', '视觉概念：' + (row.concept || '（空）'), 'meta'));
        const text = row.records[0]?.prompt || row.details.prompt || '（无生成文本）';
        card.appendChild(element('p', text, 'text'));
      } else {
        card.appendChild(element('p', row.source.prompt, 'text'));
        const columns = element('div', undefined, 'columns');
        for (const level of ['light', 'medium', 'rich']) {
          columns.appendChild(renderLevel(row, level));
        }
        card.appendChild(columns);
      }
    }

    function renderCard(row) {
      const card = element('article');
      const title = element('h2', row.id);
      const badgeClass = 'badge ' + (row.status === 'rejected' ? 'rejected' : '');
      title.appendChild(element('span', ' ' + row.status, badgeClass));
      card.appendChild(title);
      const conditions = Object.keys(row.source.sample || {}).length ?
        JSON.stringify(row.source.sample) : '（无关联元数据）';
      const metadata = '来源：' + (origin(row) || '（无关联元数据）') +
        ' · 条件：' + conditions;
      card.appendChild(element('p', metadata, 'meta'));
      renderContent(card, row);
      if (row.reason) card.appendChild(element('p', row.reason, 'text'));
      const details = element('details');
      details.appendChild(element('summary', '展开校验、PIU、证据、反馈与尝试信息'));
      details.appendChild(element('pre', JSON.stringify(row.details, null, 2)));
      card.appendChild(details);
      return card;
    }

    function render() {
      const filtered = filteredRows();
      $('rows').replaceChildren();$('count').textContent = `显示 ${filtered.length} / ${rows.length}`;
      for (const row of filtered) $('rows').appendChild(renderCard(row));
      if (!filtered.length) {
        $('rows').appendChild(element('p', '当前页没有匹配样本。', 'empty'));
      }
    }

    initializeFilters();
    render();
  </script>
</body>
</html>"""


def safe_json(rows) -> str:
    return (
        json.dumps(rows, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _page(path: Path, rows: list, title: str, scope: str, navigation: str):
    content = (
        TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__SCOPE__", html.escape(scope))
        .replace("__NAV__", navigation)
        .replace("__DIMENSIONS__", json.dumps(DIMENSIONS))
        .replace("__DATA__", safe_json(rows))
    )
    atomic_lines(path, [content])


def write_preview(store: RunStore, page_size: int):
    calls = defaultdict(list)
    for event in store.events:
        if event["event"] in {"request", "technical_failure", "quality_decision", "fatal"}:
            calls[event["sample_id"]].append(
                {
                    key: event[key]
                    for key in (
                        "stage",
                        "level",
                        "round",
                        "generation_attempt",
                        "technical_attempt",
                        "event",
                        "data",
                    )
                    if key != "data"
                }
                | ({"reason": event["data"]["reason"]} if "reason" in event["data"] else {})
            )

    def rows():
        for result in store.results():
            row = result.model_dump()
            row["details"]["calls"] = calls[result.id]
            yield row

    total = sum(1 for _ in store.results())
    if total <= page_size:
        _page(
            store.out / "preview.html",
            list(rows()),
            store.manifest["workflow"],
            f"全部 {total} 个已完成样本；搜索和筛选作用于本文件。",
            "",
        )
        return
    directory = store.out / "preview"
    directory.mkdir(exist_ok=True)
    count = (total + page_size - 1) // page_size
    batch, page = [], 1
    for row in rows():
        batch.append(row)
        if len(batch) == page_size:
            _write_batch(directory, batch, page, count, total, store.manifest["workflow"])
            batch, page = [], page + 1
    if batch:
        _write_batch(directory, batch, page, count, total, store.manifest["workflow"])
    links = " ".join(
        f'<a href="preview/page-{index:04d}.html">第 {index} 页</a>'
        for index in range(1, count + 1)
    )
    _page(
        store.out / "preview.html",
        [],
        "分页索引",
        f"共 {total} 个样本、{count} 页；进入分页查看。搜索和筛选仅作用于当前页。",
        links,
    )


def _write_batch(directory, batch, page, count, total, workflow):
    links = ['<a href="../preview.html">索引</a>']
    if page > 1:
        links.append(f'<a href="page-{page - 1:04d}.html">上一页</a>')
    if page < count:
        links.append(f'<a href="page-{page + 1:04d}.html">下一页</a>')
    _page(
        directory / f"page-{page:04d}.html",
        batch,
        workflow,
        f"第 {page}/{count} 页，共 {total} 个样本；搜索和筛选仅作用于当前页。",
        " · ".join(links),
    )