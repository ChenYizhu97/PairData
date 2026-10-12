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
      font-family: system-ui, sans-serif;
      color: #263444;
      background: #f2f5f9;
    }
    body {
      max-width: 1500px;
      margin: 32px auto;
      padding: 0 24px;
    }
    h1 { font-size: 26px; }
    a { color: #2767a4; }
    nav { margin: 16px 0; }
    input, select {
      font: inherit;
      padding: 8px;
      border: 1px solid #b7c7d9;
      border-radius: 6px;
      margin: 4px;
      max-width: 95%;
    }
    .toolbar {
      padding: 12px;
      background: white;
      border-radius: 12px;
      margin-bottom: 20px;
    }
    article {
      background: white;
      border: 1px solid #dbe4ef;
      border-radius: 12px;
      padding: 20px;
      margin: 16px 0;
    }
    .badge {
      font-size: 13px;
      background: #e5f3eb;
      border-radius: 4px;
      padding: 4px 8px;
    }
    .rejected { background: #ffe8e6; }
    .meta { color: #62738a; font-size: 14px; }
    .columns {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 20px;
    }
    .text {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.6;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #f5f7fa;
      padding: 16px;
      font-size: 12px;
    }
    summary { cursor: pointer; margin-top: 16px; }
    h3 { font-size: 15px; color: #365679; }
    .empty { color: #8090a3; }
    @media (max-width: 800px) {
      .columns { grid-template-columns: 1fr; }
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
      $('rows').replaceChildren();
      $('count').textContent = `显示 ${filtered.length} / ${rows.length}`;
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
