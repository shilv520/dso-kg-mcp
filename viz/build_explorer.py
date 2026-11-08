#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_explorer.py — 生成自包含交互式知识图谱浏览器 viz/kg_explorer.html。

数据来自 data/kb.sqlite（实体/关系/evidence）；vis-network 内联，离线可用。
节点按 DSO 类型着色，大小按度数；点击节点看描述/属性/教材出处；
左侧图例可按类型过滤，支持搜索定位。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "kb.sqlite"
VIS_JS = ROOT / "viz" / "vis-network.min.js"
OUT = ROOT / "viz" / "kg_explorer.html"

COLORS = {
    "Method": "#4e79a7", "DomainConcept": "#59a14f", "ReferenceClaim": "#f28e2b",
    "Guideline": "#e15759", "Constraint": "#b07aa1", "Metric": "#76b7b2",
    "EvaluationProtocol": "#9c755f", "ProblemType": "#edc948",
    "ActivityPattern": "#86bcb6", "Stage": "#8cd17d", "WorkflowPattern": "#b6992d",
    "SolutionTemplate": "#d37295", "MethodImplementation": "#a0cbe8",
    "SoftwareComponent": "#ffbe7d", "DataCondition": "#d4a6c8",
    "DataCharacteristic": "#9d7660", "DataStatistic": "#d7b5a6",
    "IndustryDomain": "#bab0ac",
}
CLAIMY = {"ReferenceClaim", "Guideline", "Constraint"}
PROCESS = {"SolutionTemplate", "Stage", "WorkflowPattern", "ActivityPattern"}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    ents = {r["uri"]: r for r in con.execute("SELECT * FROM entities")}
    degree: Counter = Counter()
    edges = []
    rel_evidence: dict = defaultdict(list)
    for r in con.execute("SELECT DISTINCT s, p, o FROM relation_evidence"):
        degree[r["s"]] += 1
        degree[r["o"]] += 1
    for r in con.execute("SELECT s, p, o, quote, fragment_id FROM relation_evidence"):
        rel_evidence[(r["s"], r["p"], r["o"])].append(r["quote"] or "")
    for (s, p, o), quotes in sorted(rel_evidence.items()):
        if s not in ents or o not in ents:
            continue
        st, ot = ents[s]["type"], ents[o]["type"]
        style = "dashed" if p.endswith("BroaderMethod") or "Broader" in p or p.endswith("RefinesDataCondition") else "solid"
        color = "#e15759" if st in CLAIMY or ot in CLAIMY else \
                "#b6992d" if st in PROCESS and ot in PROCESS else "#9aa0a6"
        q = next((x for x in quotes if x), "")
        edges.append({"from": s, "to": o, "label": p, "color": color, "dashes": style == "dashed",
                      "title": f"{p}\n“{q[:160]}”" if q else p})

    prov = defaultdict(list)
    for r in con.execute("SELECT ef.uri, f.locator, ef.evidence FROM entity_fragments ef "
                         "JOIN fragments f ON f.fragment_id=ef.fragment_id"):
        prov[r["uri"]].append({"locator": r["locator"], "quote": r["evidence"] or ""})

    nodes = []
    for uri, r in ents.items():
        t = r["type"]
        props = json.loads(r["props_json"] or "{}")
        deg = degree.get(uri, 0)
        nodes.append({
            "id": uri, "label": r["display_name"], "group": t,
            "color": {"background": COLORS.get(t, "#cccccc"),
                      "border": "#333333"},
            "size": 8 + min(deg, 20) * 1.4,
            "font": {"size": 12 + min(deg, 12)},
            "desc": r["description"] or "",
            "props": props,
            "prov": prov.get(uri, [])[:4],
            "title": f"{r['display_name']} ({t}) — deg {deg}",
        })

    stats = Counter(r["type"] for r in ents.values())
    vis_js = VIS_JS.read_text(encoding="utf-8")
    data_js = "const NODES = " + json.dumps(nodes, ensure_ascii=False) + ";\n" + \
              "const EDGES = " + json.dumps(edges, ensure_ascii=False) + ";\n" + \
              "const COLORS = " + json.dumps(COLORS) + ";\n" + \
              "const STATS = " + json.dumps(dict(stats.most_common())) + ";"

    html = HTML_TEMPLATE.replace("/*__VIS__*/", vis_js).replace("/*__DATA__*/", data_js)
    OUT.write_text(html, encoding="utf-8")
    print(f"written: {OUT} ({len(html)//1024} KB, {len(nodes)} nodes, {len(edges)} edges)")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>DSO 教材知识图谱浏览器 — Hands-On ML 全书 16 章</title>
<style>
  html,body{margin:0;height:100%;font-family:"Segoe UI",system-ui,sans-serif;background:#111;color:#eee}
  #app{display:flex;height:100%}
  #side{width:290px;min-width:290px;background:#1a1a1a;border-right:1px solid #333;padding:14px;overflow-y:auto}
  #net{flex:1;position:relative}
  h1{font-size:16px;margin:0 0 4px}
  .sub{font-size:11px;color:#999;margin-bottom:10px}
  #q{width:100%;box-sizing:border-box;padding:6px 8px;border-radius:6px;border:1px solid #444;background:#222;color:#eee;margin-bottom:10px}
  .lg{display:flex;align-items:center;gap:6px;font-size:12px;padding:2px 4px;cursor:pointer;border-radius:4px;user-select:none}
  .lg:hover{background:#262626}
  .lg .dot{width:11px;height:11px;border-radius:50%;flex:none}
  .lg .n{margin-left:auto;color:#888}
  .lg.off{opacity:.35;text-decoration:line-through}
  #detail{position:absolute;right:10px;top:10px;width:340px;max-height:calc(100% - 20px);overflow-y:auto;background:rgba(20,20,20,.96);border:1px solid #444;border-radius:8px;padding:12px;font-size:12px;display:none;z-index:5}
  #detail h2{font-size:14px;margin:0 0 2px}
  #detail .t{color:#999;font-size:11px;margin-bottom:8px}
  #detail .props div{margin:2px 0;color:#cde}
  #detail .prov{margin-top:8px;border-top:1px solid #333;padding-top:6px}
  #detail .prov div{margin:4px 0;color:#bca}
  #detail .x{float:right;cursor:pointer;color:#888;font-size:16px}
  #count{font-size:11px;color:#888;margin:8px 0}
  button{background:#333;color:#eee;border:1px solid #555;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;margin:2px 2px 8px 0}
</style>
</head>
<body>
<div id="app">
  <div id="side">
    <h1>DSO 教材知识图谱</h1>
    <div class="sub">Hands-On ML (1st ed.) 全书 16 章 · DSO v0.2 · SHACL 0 违规 · 嵌入判重+混合检索</div>
    <input id="q" placeholder="搜索实体…（回车定位）"/>
    <div><button id="all">全选</button><button id="none">全不选</button><button id="fit">全图</button></div>
    <div id="legend"></div>
    <div id="count"></div>
    <div class="sub">虚线 = *Broader* 分类关系 · 红 = claim 相关 · 黄 = 过程模型 · 点击节点看详情/出处</div>
  </div>
  <div id="net"></div>
  <div id="detail"><span class="x" onclick="document.getElementById('detail').style.display='none'">✕</span><div id="dbody"></div></div>
</div>
<script>/*__VIS__*/</script>
<script>/*__DATA__*/</script>
<script>
const nodesDS = new vis.DataSet(NODES);
const edgesDS = new vis.DataSet(EDGES.map((e,i)=>({id:i,...e,arrows:{to:{enabled:true,scaleFactor:.4}},width:e.dashes?1.5:1,font:{size:0,color:'#888'},smooth:{type:'continuous'}})));
const net = new vis.Network(document.getElementById('net'), {nodes:nodesDS, edges:edgesDS}, {
  physics:{barnesHut:{gravitationalConstant:-2600,springLength:110,springConstant:.03,damping:.5},stabilization:{iterations:250}},
  interaction:{hover:true,tooltipDelay:120,navigationButtons:false,keyboard:false},
  nodes:{shape:'dot',borderWidth:1}
});
const hidden = new Set();
const legend = document.getElementById('legend');
for (const [t,n] of Object.entries(STATS)) {
  const d = document.createElement('div'); d.className='lg';
  d.innerHTML = `<span class="dot" style="background:${COLORS[t]||'#ccc'}"></span><span>${t}</span><span class="n">${n}</span>`;
  d.onclick = () => { hidden.has(t)?hidden.delete(t):hidden.add(t); d.classList.toggle('off',hidden.has(t)); applyFilter(); };
  legend.appendChild(d);
}
function applyFilter(){
  nodesDS.forEach(n=>nodesDS.update({id:n.id, hidden:hidden.has(n.group)}));
  const vis = new Set(NODES.filter(n=>!hidden.has(n.group)).map(n=>n.id));
  edgesDS.forEach(e=>edgesDS.update({id:e.id, hidden:!(vis.has(e.from)&&vis.has(e.to))}));
  document.getElementById('count').textContent = `显示 ${vis.size}/${NODES.length} 实体`;
}
applyFilter();
document.getElementById('all').onclick=()=>{hidden.clear();document.querySelectorAll('.lg').forEach(x=>x.classList.remove('off'));applyFilter()};
document.getElementById('none').onclick=()=>{Object.keys(STATS).forEach(t=>hidden.add(t));document.querySelectorAll('.lg').forEach(x=>x.classList.add('off'));applyFilter()};
document.getElementById('fit').onclick=()=>net.fit({animation:true});
document.getElementById('q').onkeydown=(ev)=>{
  if(ev.key!=='Enter')return;
  const q=ev.target.value.trim().toLowerCase(); if(!q)return;
  const hit=NODES.filter(n=>!hidden.has(n.group)&&n.label.toLowerCase().includes(q));
  if(hit.length){net.focus(hit[0].id,{scale:1.1,animation:true});net.selectNodes([hit[0].id]);showDetail(hit[0].id);}
};
net.on('click',(p)=>{ if(p.nodes.length) showDetail(p.nodes[0]); else document.getElementById('detail').style.display='none'; });
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function showDetail(id){
  const n=NODES.find(x=>x.id===id); if(!n)return;
  let h=`<h2>${esc(n.label)}</h2><div class="t">ds:${n.group} · <span style="color:#666">${esc(id)}</span></div>`;
  if(n.desc)h+=`<div>${esc(n.desc)}</div>`;
  const pk=Object.entries(n.props||{}).filter(([k])=>!['knowledgeOriginRole','displayName','identifier'].includes(k));
  if(pk.length)h+=`<div class="props"><b>属性</b>`+pk.map(([k,v])=>`<div>• ${esc(k)}: ${esc(v)}</div>`).join('')+`</div>`;
  if(n.prov&&n.prov.length)h+=`<div class="prov"><b>教材出处</b>`+n.prov.map(p=>`<div>📖 ${esc(p.locator)}${p.quote?`<br><i>“${esc(p.quote)}”</i>`:''}</div>`).join('')+`</div>`;
  document.getElementById('dbody').innerHTML=h;
  document.getElementById('detail').style.display='block';
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
