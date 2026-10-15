"""Offline source-target comparison with generic sampling and evaluation details."""

import html
import json
from collections import defaultdict
from itertools import islice

from pairdata.storage import RunStore, atomic_writer

PAGE_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PairData · __TITLE__</title>
<style>
body{font:15px/1.6 system-ui,sans-serif;background:#f8fafc;color:#172033;max-width:1200px;margin:auto;padding:24px}
article,.toolbar{background:white;border:1px solid #dce2eb;border-radius:10px;padding:18px;margin:16px 0}
h1,h2,h3{margin:0 0 10px}h2{font-size:18px;overflow-wrap:anywhere}
.toolbar{display:flex;gap:10px;flex-wrap:wrap}input,select{font:inherit;padding:6px;border:1px solid #cbd5e1;border-radius:5px}
input{flex:1;min-width:180px}.columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
p,pre{white-space:pre-wrap;overflow-wrap:anywhere}pre{font-size:12px}.meta{color:#64748b}a{color:#2563eb;margin-right:16px}
@media(max-width:700px){.columns{grid-template-columns:1fr}}
</style>
<h1>PairData · __TITLE__</h1>
<p class="meta">__SCOPE__</p><nav>__NAVIGATION__</nav>
<div class="toolbar">
<input id="search" placeholder="搜索当前页 / Search this page" aria-label="搜索当前页">
<select id="status" aria-label="状态"><option value="">所有状态 / All</option><option>passed</option><option>rejected</option></select>
<span id="count" class="meta"></span>
</div><main id="results"></main>
<script id="data" type="application/json">__DATA__</script>
<script>
const $ = id => document.getElementById(id);
const records = JSON.parse($('data').textContent);
function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}
function display(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
function details(label, value) {
  const node = element('details');
  node.append(element('summary', label), element('pre', display(value)));
  return node;
}
function card(record) {
  const article = element('article');
  article.appendChild(element('h2', `${record.id} · ${record.status}`));
  const columns = element('div'); columns.className = 'columns';
  for (const name of ['source', 'target']) {
    const column = element('section'), stage = record[name];
    column.appendChild(element('h3', name));
    if (!stage) column.appendChild(element('p', '未进入该阶段 / Not reached'));
    else {
      column.appendChild(element('p', display(stage.candidate)));
      column.appendChild(element('p', `${stage.status} · 生成次数 / Attempts: ${stage.generation_attempt}`));
      column.appendChild(details('Sample', stage.sample));
      column.appendChild(details('Evaluation', stage.evaluation));
      column.appendChild(details('Decision', stage.decision));
      if (stage.reason) column.appendChild(element('p', stage.reason));
    }
    columns.appendChild(column);
  }
  article.appendChild(columns);
  if (record.reason) article.appendChild(element('p', record.reason));
  article.appendChild(details('运行记录 / Run details', record));
  return article;
}
function render() {
  const query = $('search').value.toLocaleLowerCase();
  const visible = records.filter(record =>
    (!query || JSON.stringify(record).toLocaleLowerCase().includes(query)) &&
    (!$('status').value || record.status === $('status').value));
  $('count').textContent = `${visible.length} / ${records.length}`;
  $('results').replaceChildren(...visible.map(card));
}
$('search').addEventListener('input', render);
$('status').addEventListener('change', render);
render();
</script></html>"""


def embed_json(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False)
    for character, escaped in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        encoded = encoded.replace(character, escaped)
    return encoded


def write_page(path, records, title, scope, navigation):
    # Insert model text last so placeholders inside it are not processed as template syntax.
    page = (
        PAGE_TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__SCOPE__", html.escape(scope))
        .replace("__NAVIGATION__", navigation)
        .replace("__DATA__", embed_json(records))
    )
    with atomic_writer(path) as handle:
        handle.write(page)


def preview_records(store: RunStore):
    calls = defaultdict(list)
    for event in store.events:
        if event["event"] in {"request", "quality_decision", "technical_failure", "fatal"}:
            calls[event["sample_id"]].append(
                {
                    key: event[key]
                    for key in ("stage", "generation_attempt", "technical_attempt", "event")
                }
                | ({"reason": event["data"]["reason"]} if "reason" in event["data"] else {})
            )
    for result in store.results():
        yield {**result.model_dump(), "calls": calls[result.id]}


def write_preview(store: RunStore, page_size: int):
    total = sum(1 for _ in store.results())
    title = store.manifest["config"]["name"]
    records = preview_records(store)
    if total <= page_size:
        write_page(
            store.directory / "preview.html",
            list(records),
            title,
            f"共 {total} 个已完成样本；搜索和筛选作用于当前页。",
            "",
        )
        return
    directory = store.directory / "preview"
    directory.mkdir(exist_ok=True)
    page_count = (total + page_size - 1) // page_size
    for number in range(1, page_count + 1):
        navigation = ['<a href="../preview.html">索引</a>']
        if number > 1:
            navigation.append(f'<a href="page-{number - 1:04d}.html">上一页</a>')
        if number < page_count:
            navigation.append(f'<a href="page-{number + 1:04d}.html">下一页</a>')
        write_page(
            directory / f"page-{number:04d}.html",
            list(islice(records, page_size)),
            title,
            f"第 {number}/{page_count} 页；搜索和筛选作用于当前页。",
            " ".join(navigation),
        )
    navigation = " ".join(
        f'<a href="preview/page-{number:04d}.html">第 {number} 页</a>'
        for number in range(1, page_count + 1)
    )
    write_page(store.directory / "preview.html", [], title, f"共 {total} 个样本。", navigation)
