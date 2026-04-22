#!/usr/bin/env python3
"""
build_wbs_html.py — Render a WBS (EDT) tree as a self-contained interactive HTML.

Input (JSON):
{
  "project": {
    "title": "...", "client": "...", "start_date": "...", "currency": "COP",
    "estimated_duration_months": 6, "estimated_team_size": 7
  },
  "wbs": {
    "id": "1",
    "name": "Sistema de Inventarios AcmeCorp",
    "description": "...",
    "branch": "product",          // "product" | "management"
    "is_leaf": false,
    "dictionary": null,            // only on leaves
    "children": [ ... ]
  }
}

Output: self-contained .html. Uses D3.js v7 from jsdelivr CDN.

Usage:
    python3 build_wbs_html.py spec.json --out wbs.html
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _augment(node, parent_branch="product", depth=0):
    """Walk the WBS tree and ensure each node has branch + depth set."""
    if "branch" not in node:
        node["branch"] = parent_branch
    node["_depth"] = depth
    if "is_leaf" not in node:
        node["is_leaf"] = not (node.get("children") or [])
    if node.get("children"):
        for ch in node["children"]:
            _augment(ch, node["branch"], depth + 1)


HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EDT — __TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<style>
  :root {
    --bg:#f7f8fa; --card:#fff; --ink:#1f2937; --mute:#6b7280; --line:#e5e7eb;
    --product:#2563eb;    /* azul — producto */
    --mgmt:#ea580c;       /* naranja — gerencia */
    --leaf-product:#3b82f6;
    --leaf-mgmt:#f97316;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         height:100vh; display:flex; flex-direction:column; overflow:hidden; }
  header { padding:16px 24px; background:var(--card); border-bottom:1px solid var(--line);
           display:flex; justify-content:space-between; align-items:center; gap:20px; flex-shrink:0; }
  h1 { margin:0 0 4px; font-size:20px; }
  .meta { color:var(--mute); font-size:12px; }
  .meta span + span:before { content:" · "; opacity:.4; }
  .legend { display:flex; gap:14px; font-size:11px; color:var(--mute); align-items:center; flex-wrap:wrap; }
  .legend .sw { display:inline-block; width:14px; height:14px; border-radius:3px; margin-right:5px; vertical-align:middle; }
  #search { padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:13px; width:220px; }
  #orient-toggle { padding:6px 12px; border:1px solid var(--line); border-radius:6px; font-size:12.5px;
                   background:#fff; color:var(--ink); cursor:pointer; font-weight:600; transition:all .15s;
                   display:flex; align-items:center; gap:6px; }
  #orient-toggle:hover { background:#f3f4f6; border-color:#94a3b8; }
  #orient-toggle .ori-icon { font-size:15px; line-height:1; }

  main { flex:1; display:flex; overflow:hidden; }

  #tree-container { flex:1; overflow:auto; background:var(--card); position:relative; }
  #tree-svg { display:block; }

  /* Node rectangles */
  .node-box { cursor:pointer; }
  .node-box rect { stroke-width:1.5; rx:6; ry:6; transition:all 0.2s; }
  .node-box:hover rect { stroke-width:2.5; filter:drop-shadow(0 2px 6px rgba(0,0,0,0.15)); }
  .node-box.branch-product rect        { fill:#dbeafe; stroke:var(--product); }
  .node-box.branch-product.leaf rect   { fill:#bfdbfe; stroke:var(--leaf-product); }
  .node-box.branch-management rect     { fill:#ffedd5; stroke:var(--mgmt); }
  .node-box.branch-management.leaf rect{ fill:#fed7aa; stroke:var(--leaf-mgmt); }
  .node-box.selected rect { stroke-width:3; filter:drop-shadow(0 3px 8px rgba(0,0,0,0.25)); }

  .node-id   { font-size:10px; fill:#6b7280; font-weight:600; }
  .node-name { font-size:12px; fill:#111827; font-weight:500; }
  .node-name.leaf { fill:#1e40af; }
  .node-box.branch-management .node-name.leaf { fill:#9a3412; }

  .collapse-badge { font-size:9px; fill:#ffffff; font-weight:700; text-anchor:middle; dominant-baseline:central; }
  .collapse-circle { fill:#6b7280; }
  .collapse-circle-collapsed { fill:#2563eb; }

  /* Connector lines */
  .link { fill:none; stroke:#cbd5e1; stroke-width:1.5; }

  /* Side panel for dictionary */
  #side { width:420px; background:var(--card); border-left:1px solid var(--line);
          overflow-y:auto; padding:20px; display:none; flex-shrink:0; }
  #side.open { display:block; }
  #side h2 { margin:0 0 4px; font-size:16px; }
  #side .code { font-size:11px; color:var(--mute); font-weight:600; letter-spacing:.05em; }
  #side section { margin-top:16px; padding-top:12px; border-top:1px solid var(--line); }
  #side section:first-of-type { border-top:0; margin-top:12px; padding-top:0; }
  #side h3 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--mute); margin:0 0 8px; }
  #side ul { padding-left:20px; margin:4px 0; }
  #side li { margin:4px 0; font-size:13px; }
  #side .tag { display:inline-block; padding:2px 8px; border-radius:999px; font-size:10px; font-weight:600; margin-right:4px; }
  #side .tag-prod { background:#dbeafe; color:#1e40af; }
  #side .tag-mgmt { background:#ffedd5; color:#9a3412; }
  #side table { width:100%; border-collapse:collapse; font-size:12px; margin:6px 0; }
  #side td, #side th { padding:4px 8px; border-bottom:1px solid var(--line); text-align:left; }
  #side th { font-size:10px; color:var(--mute); text-transform:uppercase; }
  #close-side { float:right; cursor:pointer; color:var(--mute); font-size:20px; line-height:1; }
  #close-side:hover { color:var(--ink); }

  /* No dictionary placeholder */
  .empty-state { color:var(--mute); font-size:13px; font-style:italic; padding:12px; background:#f9fafb; border-radius:4px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>__TITLE__</h1>
    <div class="meta">
      <span>Cliente: __CLIENT__</span>
      <span>Inicio: __START__</span>
      <span>Moneda: __CURRENCY__</span>
      <span>Duración est.: __DURATION__</span>
      <span>Equipo est.: __TEAM_SIZE__</span>
    </div>
  </div>
  <div style="display:flex; align-items:center; gap:12px;">
    <div class="legend">
      <span><span class="sw" style="background:#dbeafe;border:1px solid #2563eb;"></span>Producto</span>
      <span><span class="sw" style="background:#ffedd5;border:1px solid #ea580c;"></span>Gerencia</span>
      <span><span class="sw" style="background:#bfdbfe;border:1px solid #3b82f6;"></span>Hoja Producto</span>
      <span><span class="sw" style="background:#fed7aa;border:1px solid #f97316;"></span>Hoja Gerencia</span>
    </div>
    <input id="search" type="text" placeholder="Buscar…" oninput="onSearch(this.value)" />
    <button id="orient-toggle" title="Rotar árbol"><span class="ori-icon">↔</span> Horizontal</button>
  </div>
</header>

<main>
  <div id="tree-container">
    <svg id="tree-svg" width="100%" height="100%"></svg>
  </div>
  <aside id="side">
    <span id="close-side" onclick="closeSide()">×</span>
    <div id="side-content"></div>
  </aside>
</main>

<script>
const DATA = __DATA__;

// Augment
(function aug(n, parent){
  n._parent = parent;
  if(!n.branch) n.branch = parent ? parent.branch : 'product';
  if(n.children) n.children.forEach(c => aug(c, n));
})(DATA, null);

// Layout via d3.tree horizontal
const svg = d3.select('#tree-svg');
const container = document.getElementById('tree-container');

let selected = null;
let rootNode = null;
let orientation = 'horizontal';  // 'horizontal' | 'vertical'

function renderTree() {
  svg.selectAll('*').remove();

  const root = d3.hierarchy(DATA, d => d._children || d.children);
  rootNode = root;
  // Collapse nodes deeper than initial depth
  // (we keep first 3 levels expanded by default)
  function setInitial(n, depth){
    if(depth >= 2 && n.children) {
      n._children = n.children; n.children = null;
    }
    if(n.children) n.children.forEach(c => setInitial(c, depth+1));
  }
  setInitial(root, 0);

  const nodeWidth = 220, nodeHeight = 46;
  // Horizontal: [verticalSpacing, horizontalSpacing] → x=vertical, y=horizontal
  // Vertical:   [horizontalSpacing, verticalSpacing] → x=horizontal, y=vertical
  const H_SEP_Y = 58,  H_SEP_X = 260;  // horizontal layout
  const V_SEP_Y = 245, V_SEP_X = 130;  // vertical layout (more x-space for wide rects)

  function update() {
    const isH = orientation === 'horizontal';
    const tree = d3.tree().nodeSize(isH ? [H_SEP_Y, H_SEP_X] : [V_SEP_Y, V_SEP_X]);
    tree(root);

    const nodes = root.descendants();
    const links = root.links();

    // In d3, tree() always assigns x=along breadth, y=depth.
    // For horizontal tree: render (x, y) swapped → nodeX=y, nodeY=x
    // For vertical tree: render as-is → nodeX=x, nodeY=y
    const nx = d => isH ? d.y : d.x;
    const ny = d => isH ? d.x : d.y;

    // Compute bounds
    let minNX = Infinity, maxNX = -Infinity, minNY = Infinity, maxNY = -Infinity;
    nodes.forEach(n => {
      const x = nx(n), y = ny(n);
      if (x < minNX) minNX = x; if (x > maxNX) maxNX = x;
      if (y < minNY) minNY = y; if (y > maxNY) maxNY = y;
    });

    // Padding depends on node geometry.
    // Horizontal: rect extends from x=-10 to x=nodeWidth-10 relative to node center; vertical centers (nodeWidth/2 each side).
    const padLeft   = isH ? 20  : nodeWidth/2 + 20;
    const padRight  = isH ? nodeWidth + 40 : nodeWidth/2 + 40;
    const padTop    = nodeHeight/2 + 30;
    const padBottom = nodeHeight/2 + 30;

    const width  = (maxNX - minNX) + padLeft + padRight;
    const height = (maxNY - minNY) + padTop + padBottom;

    svg.attr('viewBox', `${minNX - padLeft} ${minNY - padTop} ${width} ${height}`)
       .attr('width', width).attr('height', height);

    // Links
    const linkGen = isH
      ? d3.linkHorizontal().x(d => d.y).y(d => d.x)
      : d3.linkVertical().x(d => d.x).y(d => d.y);
    const link = svg.selectAll('.link').data(links, d => d.target.data.id);
    link.exit().remove();
    link.enter().append('path').attr('class','link').merge(link).attr('d', linkGen);

    // Rect x offset depends on orientation
    const rectX = isH ? -10 : -nodeWidth/2;
    const textX = isH ? 0 : -nodeWidth/2 + 10;
    const badgeCx = isH ? (nodeWidth - 20) : (nodeWidth/2 - 10);

    // Nodes
    const node = svg.selectAll('.node-box').data(nodes, d => d.data.id);
    node.exit().remove();

    const nEnter = node.enter().append('g')
      .attr('class', d => 'node-box ' +
                        (d.data.branch === 'management' ? 'branch-management' : 'branch-product') +
                        (d.data.is_leaf ? ' leaf' : ''))
      .on('click', (evt, d) => {
        evt.stopPropagation();
        if (d.data.is_leaf) {
          selectNode(d); openSide(d);
        } else {
          toggle(d); update();
        }
      });

    nEnter.append('rect').attr('y', -nodeHeight/2)
          .attr('width', nodeWidth).attr('height', nodeHeight);
    nEnter.append('text').attr('class','node-id').attr('y', -nodeHeight/2 + 14);
    nEnter.append('text').attr('class', d => 'node-name' + (d.data.is_leaf ? ' leaf':''))
          .attr('y', -nodeHeight/2 + 30);
    // Collapse indicator (circle + count) for non-leaves
    const collapseG = nEnter.append('g').attr('class','collapse-g');
    collapseG.append('circle').attr('class','collapse-circle').attr('cy', 0).attr('r', 9);
    collapseG.append('text').attr('class','collapse-badge').attr('y', 0);

    // Merge
    const nAll = nEnter.merge(node)
      .attr('transform', d => `translate(${nx(d)},${ny(d)})`)
      .attr('class', d => 'node-box ' +
                        (d.data.branch === 'management' ? 'branch-management' : 'branch-product') +
                        (d.data.is_leaf ? ' leaf' : '') +
                        (selected && selected.data.id === d.data.id ? ' selected' : ''));

    nAll.select('rect').attr('x', rectX);
    nAll.select('.node-id').attr('x', textX).text(d => d.data.id);
    nAll.select('.node-name').attr('x', textX).text(d => {
      const n = d.data.name || '';
      return n.length > 30 ? n.slice(0, 28) + '…' : n;
    });
    nAll.select('.collapse-g').attr('display', d => d.data.is_leaf ? 'none' : null);
    nAll.select('.collapse-circle').attr('cx', badgeCx)
        .attr('class', d => 'collapse-circle' + (d._children ? ' collapse-circle-collapsed' : ''));
    nAll.select('.collapse-badge').attr('x', badgeCx).text(d => {
      if (d.data.is_leaf) return '';
      const count = (d._children || d.children || []).length;
      return d._children ? '+' + count : '−';
    });

    // Tooltips
    nAll.select('rect').append('title');
    nAll.selectAll('title').text(d => `${d.data.id} · ${d.data.name}${d.data.description ? '\n\n' + d.data.description : ''}`);
  }

  function toggle(d) {
    if (d.children) { d._children = d.children; d.children = null; }
    else if (d._children) { d.children = d._children; d._children = null; }
  }

  update();
  window._treeUpdate = update;
}

// Toggle button
document.getElementById('orient-toggle').addEventListener('click', () => {
  orientation = orientation === 'horizontal' ? 'vertical' : 'horizontal';
  const btn = document.getElementById('orient-toggle');
  btn.innerHTML = orientation === 'horizontal'
    ? '<span class="ori-icon">↔</span> Horizontal'
    : '<span class="ori-icon">↕</span> Vertical';
  window._treeUpdate && window._treeUpdate();
});

function selectNode(d) {
  selected = d;
  window._treeUpdate && window._treeUpdate();
}

function closeSide() {
  document.getElementById('side').classList.remove('open');
  selected = null;
  window._treeUpdate && window._treeUpdate();
}

function openSide(d) {
  const n = d.data;
  const branchTag = n.branch === 'management'
    ? '<span class="tag tag-mgmt">Gerencia</span>'
    : '<span class="tag tag-prod">Producto</span>';
  let html = `
    <div class="code">${n.id}</div>
    <h2>${esc(n.name)}</h2>
    ${branchTag}
    <section>
      <h3>Descripción</h3>
      <p>${esc(n.description || '—')}</p>
    </section>`;

  const dict = n.dictionary;
  if (dict) {
    if (dict.acceptance_criteria && dict.acceptance_criteria.length) {
      html += `<section><h3>Criterios de aceptación</h3><ul>` +
        dict.acceptance_criteria.map(c => `<li>${esc(c)}</li>`).join('') +
        `</ul></section>`;
    }
    if (dict.activities && dict.activities.length) {
      html += `<section><h3>Actividades (${dict.activities.length})</h3><ul>` +
        dict.activities.map(a => `<li>${esc(a.name)} <span style="color:#6b7280">(${esc(a.duration||'')})</span></li>`).join('') +
        `</ul></section>`;
    }
    if (dict.human_resources && dict.human_resources.length) {
      html += `<section><h3>Recursos humanos</h3><table><tr><th>Perfil</th><th>Días</th><th>Tarifa</th></tr>` +
        dict.human_resources.map(r => `<tr><td>${esc(r.profile)}</td><td>${esc(r.days||'')}</td><td>${esc(r.hourly_rate||'')}</td></tr>`).join('') +
        `</table></section>`;
    }
    if (dict.material_resources && dict.material_resources.length) {
      html += `<section><h3>Materiales</h3><table><tr><th>Recurso</th><th>Cant.</th><th>Costo</th></tr>` +
        dict.material_resources.map(r => `<tr><td>${esc(r.name)}</td><td>${esc(r.quantity||'')}</td><td>${esc(r.total_cost||'')}</td></tr>`).join('') +
        `</table></section>`;
    }
    if (dict.total_cost != null || dict.total_duration_days != null) {
      html += `<section><h3>Totales</h3>` +
        (dict.total_cost != null ? `<p><strong>Costo:</strong> ${esc(dict.total_cost)}</p>` : '') +
        (dict.total_duration_days != null ? `<p><strong>Duración:</strong> ${esc(dict.total_duration_days)} días</p>` : '') +
        `</section>`;
    }
    if (dict.risks && dict.risks.length) {
      html += `<section><h3>Riesgos</h3><table><tr><th>Descripción</th><th>P</th><th>I</th><th>Mitigación</th></tr>` +
        dict.risks.map(r => `<tr><td>${esc(r.description||'')}</td><td>${esc(r.probability||'')}</td><td>${esc(r.impact||'')}</td><td>${esc(r.mitigation||'')}</td></tr>`).join('') +
        `</table></section>`;
    }
    if (dict.assumptions && dict.assumptions.length) {
      html += `<section><h3>Supuestos</h3><ul>` +
        dict.assumptions.map(a => `<li>${esc(a)}</li>`).join('') +
        `</ul></section>`;
    }
  } else if (n.is_leaf) {
    html += `<section><div class="empty-state">Sin diccionario generado para esta hoja.</div></section>`;
  }

  document.getElementById('side-content').innerHTML = html;
  document.getElementById('side').classList.add('open');
}

function onSearch(q) {
  q = (q || '').toLowerCase().trim();
  if (!q) return;
  // Expand ancestors of matching nodes
  function expandMatching(node) {
    const matches = (node.data.name || '').toLowerCase().includes(q) ||
                    (node.data.id || '').includes(q);
    let childMatch = false;
    const children = node.children || node._children;
    if (children) {
      children.forEach(c => { if (expandMatching(c)) childMatch = true; });
      if (childMatch && node._children) {
        node.children = node._children; node._children = null;
      }
    }
    return matches || childMatch;
  }
  if (rootNode) {
    expandMatching(rootNode);
    window._treeUpdate && window._treeUpdate();
  }
}

function esc(s) {
  return (s === null || s === undefined ? '' : String(s))
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

renderTree();
</script>
</body>
</html>
"""


def render_wbs(spec: dict, out_path: Path, title_override: str | None = None) -> Path:
    """Render the WBS JSON spec into an interactive HTML page."""
    project = spec.get("project") or {}
    wbs = spec.get("wbs") or {}

    # Augment branch/depth before serialization
    _augment(wbs, parent_branch=wbs.get("branch", "product"))

    title = title_override or project.get("title") or wbs.get("name") or "EDT"
    client = project.get("client") or ""
    start = project.get("start_date") or ""
    currency = project.get("currency") or ""
    duration = project.get("estimated_duration_months")
    team_size = project.get("estimated_team_size")

    html = (HTML
            .replace("__TITLE__", esc(title))
            .replace("__CLIENT__", esc(client) or "—")
            .replace("__START__", esc(start) or "—")
            .replace("__CURRENCY__", esc(currency) or "—")
            .replace("__DURATION__", (f"{duration} meses" if duration else "—"))
            .replace("__TEAM_SIZE__", (f"{team_size} personas" if team_size else "—"))
            .replace("__DATA__", json.dumps(wbs, ensure_ascii=False)))

    out_path.write_text(html, encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Render a WBS JSON as an interactive HTML page.")
    ap.add_argument("spec", help="Path to JSON spec file, or '-' for stdin")
    ap.add_argument("--out", required=True, help="Output .html path")
    ap.add_argument("--title", default=None, help="Override project title")
    args = ap.parse_args()

    if args.spec == "-":
        spec = json.load(sys.stdin)
    else:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    written = render_wbs(spec, out, args.title)
    print(f"[mpp-reader] Wrote {written} ({written.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
