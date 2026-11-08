#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""omm2schema.py — 将 DSO v0.2 OMM YAML 编译为两个下游工件（只读编译，不改 YAML）：

  1) schema/textbook_profile.json — 教材抽取 Profile（封闭词表）：
     Reference Knowledge 子集的 objectTypes / relationTypes / 枚举，
     供 schema-guided LLM 抽取时做输出约束与事后校验。
  2) schema/dso_reference_shapes.ttl — 由 OMM 基数语义编译出的 SHACL shapes，
     对应规范“实例级复合约束由 SHACL / DSO Validator Rule Catalog 承担”。

OMM relationType 基数语义（依据规范描述交叉验证）：
  - source 块的 minCount/maxCount：每个【目标实例】经由该关系可拥有的入边数
  - target 块的 minCount/maxCount：每个【源实例】经由该关系可拥有的出边数
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent           # 仓库根目录
# 老师提供的 YAML 约定放在仓库同级的 DSO_extracted/ 下；可用环境变量 DSO_YAML 覆盖
YAML_PATH = Path(os.environ.get(
    "DSO_YAML", ROOT.parent / "DSO_extracted" / "DSO_v0.2_Ontology_Model.yaml"))
OUT_DIR = ROOT / "schema"

# 教材 Reference-Knowledge Profile（DSO 规范 §6.1 + Reference Knowledge 规范 §3 的 A/B/C/D 四层）
PROFILE_TYPES = [
    # D. Provenance
    "KnowledgeSource", "SourceFragment",
    # A. Problem domain
    "IndustryDomain", "DomainConcept", "ProblemType", "Metric", "EvaluationProtocol",
    # A. Data domain
    "DataCharacteristic", "DataStatistic", "DataCondition",
    # A. Method domain
    "Method", "ModelSpecification", "MethodImplementation", "SoftwareComponent",
    # B. Process domain
    "SolutionTemplate", "Stage", "WorkflowPattern", "ActivityPattern", "TransitionPattern",
    # C. Claim layer
    "ReferenceClaim", "Guideline", "Constraint",
]

OMM_BASE_TO_XSD = {
    "omm:string": "xsd:string",
    "omm:date": "xsd:date",
    "omm:datetime": "xsd:dateTime",
    "omm:integer": "xsd:integer",
    "omm:int": "xsd:integer",
    "omm:long": "xsd:integer",
    "omm:float": "xsd:decimal",
    "omm:double": "xsd:decimal",
    "omm:decimal": "xsd:decimal",
    "omm:number": "xsd:decimal",
    "omm:boolean": "xsd:boolean",
    "omm:uri": "xsd:anyURI",
}


def local(name: str) -> str:
    """ds:Foo / omm:string -> Foo / string (keep prefix info separately)."""
    return name.split(":")[-1]


def esc(text: str) -> str:
    """Escape a string for a Turtle literal."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text.replace("\\", "\\\\").replace('"', '\\"')


def load_model():
    with open(YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def ancestor_closure(type_names: set[str], object_types: dict) -> set[str]:
    """Include abstract supertypes so hierarchy-aware relations (source=Entity etc.) survive filtering."""
    out = set(type_names)
    stack = list(type_names)
    while stack:
        t = stack.pop()
        for sup in object_types.get(t, {}).get("superTypes", []) or []:
            s = local(sup)
            if s in object_types and s not in out:
                out.add(s)
                stack.append(s)
    return out


def build_profile(model: dict):
    kn = model["knowledge"]
    ots, pts, rts, vts = kn["objectTypes"], kn["propertyTypes"], kn["relationTypes"], kn.get("valueTypes", {})

    types_in = ancestor_closure(set(PROFILE_TYPES), ots)

    obj_out: dict[str, dict] = {}
    for t in sorted(types_in):
        ot = ots[t]
        props = []
        for p in ot.get("properties", []) or []:
            pname = local(p["property"])
            pdef = pts.get(pname, {})
            ptype = pdef.get("type", "omm:string")
            enum = None
            vt_desc = None
            if ptype.startswith("ds:"):
                vt = vts.get(local(ptype), {})
                enum = vt.get("enum")
                vt_desc = vt.get("description")
            props.append({
                "name": pname,
                "description": pdef.get("description", ""),
                "type": ptype,
                "valueTypeDescription": vt_desc,
                "enum": enum,
                "minCount": p.get("minCount", 0),
                "maxCount": p.get("maxCount", "*"),
            })
        obj_out[t] = {
            "name": ot.get("name", t),
            "description": ot.get("description", ""),
            "abstract": bool(ot.get("abstract", False)),
            "superTypes": [local(s) for s in ot.get("superTypes", []) or []],
            "properties": props,
        }

    rel_out: dict[str, dict] = {}
    for r, rd in rts.items():
        st = local(rd["source"]["type"])
        tt = local(rd["target"]["type"])
        if st in obj_out and tt in obj_out:
            rel_out[r] = {
                "name": rd.get("name", r),
                "description": rd.get("description", ""),
                "sourceType": st,
                "targetType": tt,
                # per-target-instance incoming-edge cardinality (source block)
                "incomingMin": rd["source"].get("minCount", 0),
                "incomingMax": rd["source"].get("maxCount", "*"),
                # per-source-instance outgoing-edge cardinality (target block)
                "outgoingMin": rd["target"].get("minCount", 0),
                "outgoingMax": rd["target"].get("maxCount", "*"),
            }

    # concrete subclasses map (helps prompt building expand abstract domains)
    subclasses: dict[str, list[str]] = {}
    for t, td in obj_out.items():
        for s in td["superTypes"]:
            subclasses.setdefault(s, []).append(t)

    profile = {
        "profile": "dso-textbook-reference-v1",
        "ontology": {
            "id": model["manifest"]["id"],
            "version": model["manifest"]["version"],
            "prefix": model["manifest"]["namespace"]["prefix"],
            "namespace": model["manifest"]["namespace"]["uri"],
            "conformsTo": model["manifest"].get("conformsTo"),
            "compiledFrom": YAML_PATH.name,
        },
        "stats": {
            "objectTypesInProfile": len(obj_out),
            "relationTypesInProfile": len(rel_out),
            "objectTypesTotal": len(ots),
            "relationTypesTotal": len(rts),
        },
        "subclasses": subclasses,
        "objectTypes": obj_out,
        "relationTypes": rel_out,
    }
    return profile, vts


def build_shacl(profile: dict, vts: dict) -> str:
    ns = profile["ontology"]["namespace"]
    obj, rel = profile["objectTypes"], profile["relationTypes"]

    # accumulate property-shape blocks per target class
    blocks: dict[str, list[str]] = {t: [] for t in obj}

    def range_constraint(ptype: str) -> str:
        if ptype.startswith("omm:"):
            xsd = OMM_BASE_TO_XSD.get(ptype)
            return f"sh:datatype {xsd} ;" if xsd else ""
        vt = vts.get(local(ptype), {})
        enum = vt.get("enum")
        if enum:
            vals = " ".join(f'"{esc(v)}"' for v in enum)
            return f"sh:in ( {vals} ) ;"
        base = vt.get("base")
        if base and base in OMM_BASE_TO_XSD:
            return f"sh:datatype {OMM_BASE_TO_XSD[base]} ;"
        return ""

    # datatype/object property constraints declared on each objectType
    for t, td in obj.items():
        for p in td["properties"]:
            bits = [f"sh:path ds:{p['name']} ;"]
            if int(p.get("minCount", 0) or 0) > 0:
                bits.append(f"sh:minCount {p['minCount']} ;")
            if str(p.get("maxCount", "*")) != "*":
                bits.append(f"sh:maxCount {p['maxCount']} ;")
            rc = range_constraint(p["type"])
            if rc:
                bits.append(rc)
            if p.get("description"):
                bits.append(f'rdfs:comment "{esc(p["description"])}" ;')
            body = " ".join(bits)
            blocks[t].append(f"  sh:property [ {body.rstrip(' ;')} ] ;")

    # relation constraints
    for r, rd in rel.items():
        st, tt = rd["sourceType"], rd["targetType"]
        # outgoing edges constrained on the source class (target block cardinality)
        bits = [f"sh:path ds:{r} ;", f"sh:class ds:{tt} ;"]
        if int(rd.get("outgoingMin", 0) or 0) > 0:
            bits.append(f"sh:minCount {rd['outgoingMin']} ;")
        if str(rd.get("outgoingMax", "*")) != "*":
            bits.append(f"sh:maxCount {rd['outgoingMax']} ;")
        if rd.get("description"):
            bits.append(f'rdfs:comment "{esc(rd["description"])}" ;')
        blocks[st].append(f"  sh:property [ {' '.join(bits).rstrip(' ;')} ] ;")
        # incoming edges constrained on the target class (source block cardinality)
        ibits = [f"sh:path [ sh:inversePath ds:{r} ] ;", f"sh:class ds:{st} ;"]
        if int(rd.get("incomingMin", 0) or 0) > 0:
            ibits.append(f"sh:minCount {rd['incomingMin']} ;")
        if str(rd.get("incomingMax", "*")) != "*":
            ibits.append(f"sh:maxCount {rd['incomingMax']} ;")
        blocks[tt].append(f"  sh:property [ {' '.join(ibits).rstrip(' ;')} ] ;")

    out = [
        "# SHACL shapes compiled from DSO v0.2 OMM YAML (adapter/omm2schema.py).",
        "# DSO_v0.2_Ontology_Model.yaml remains the single machine source of truth;",
        "# this file is a derived validation artifact for the textbook Reference-Knowledge profile.",
        f"@prefix ds:   <{ns}> .",
        "@prefix sh:   <http://www.w3.org/ns/shacl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "",
    ]

    # class hierarchy declarations
    for t, td in sorted(obj.items()):
        supers = "".join(f" ds:{s} ;" for s in td["superTypes"])
        out.append(f"ds:{t} a rdfs:Class ; rdfs:label \"{esc(td['name'])}\" ;")
        for s in td["superTypes"]:
            out.append(f"    rdfs:subClassOf ds:{s} ;")
        out.append(f"    rdfs:comment \"{esc(td['description'])}\" .")
        out.append("")

    # node shapes
    for t, td in sorted(obj.items()):
        if not blocks.get(t):
            continue
        out.append(f"ds:Shape{t} a sh:NodeShape ;")
        out.append(f"  sh:targetClass ds:{t} ;")
        for b in blocks[t]:
            out.append(b.rstrip(";").rstrip() + " ;")
        # fix trailing separator
        out[-1] = out[-1].rstrip(" ;") + " ."
        out.append("")

    return "\n".join(out)


def build_prompt_vocab(profile: dict) -> str:
    """Compact vocabulary sheet embedded into extraction prompts (closed schema)."""
    obj, rel = profile["objectTypes"], profile["relationTypes"]
    lines = ["# DSO Textbook Reference-Knowledge Extraction Vocabulary (closed schema)", ""]
    lines.append("## Entity types (instantiate concrete types only)")
    for t, td in sorted(obj.items()):
        if td["abstract"]:
            continue
        req = [p["name"] for p in td["properties"] if int(p.get("minCount", 0) or 0) > 0]
        enums = {p["name"]: p["enum"] for p in td["properties"] if p.get("enum")}
        desc = re.sub(r"\s+", " ", td["description"])[:220]
        lines.append(f"- {t}: {desc}")
        lines.append(f"    required: {', '.join(req) if req else '(none)'}")
        for pn, ev in enums.items():
            lines.append(f"    {pn} enum: {'|'.join(ev)}")
        opt = [p["name"] for p in td["properties"] if p["name"] not in req and not p.get("enum")]
        if opt:
            lines.append(f"    optional: {', '.join(opt)}")
    lines.append("")
    lines.append("## Relation types (only these predicates are allowed)")
    by_src: dict[str, list[str]] = {}
    for r, rd in sorted(rel.items()):
        by_src.setdefault(rd["sourceType"], []).append(f"- {r} -> {rd['targetType']}: {re.sub(chr(10), ' ', rd['description'])[:140]}")
    for st in sorted(by_src):
        lines.append(f"### source: {st}")
        lines.extend(by_src[st])
    lines.append("")
    lines.append("## Abstract types (do not instantiate; use concrete subtypes)")
    for t, td in sorted(obj.items()):
        if td["abstract"]:
            subs = ", ".join(sorted(s for s, sd in obj.items() if t in sd["superTypes"]))
            lines.append(f"- {t} (subtypes: {subs})")
    return "\n".join(lines)


def main():
    model = load_model()
    profile, vts = build_profile(model)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pj = OUT_DIR / "textbook_profile.json"
    with open(pj, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    ttl = OUT_DIR / "dso_reference_shapes.ttl"
    with open(ttl, "w", encoding="utf-8") as f:
        f.write(build_shacl(profile, vts))

    vocab = OUT_DIR / "vocab_for_prompt.md"
    with open(vocab, "w", encoding="utf-8") as f:
        f.write(build_prompt_vocab(profile))

    s = profile["stats"]
    print(f"profile objectTypes : {s['objectTypesInProfile']} / {s['objectTypesTotal']} (incl. abstract ancestors)")
    print(f"profile relationTypes: {s['relationTypesInProfile']} / {s['relationTypesTotal']}")
    concrete = [t for t, d in profile["objectTypes"].items() if not d["abstract"]]
    print(f"concrete instantiable types: {len(concrete)}")
    print(f"written: {pj}")
    print(f"written: {ttl}")
    print(f"written: {vocab}")
    # sanity: every relation endpoint resolvable
    bad = [r for r, d in profile["relationTypes"].items()
           if d["sourceType"] not in profile["objectTypes"] or d["targetType"] not in profile["objectTypes"]]
    print("relations with unresolved endpoints:", len(bad))


if __name__ == "__main__":
    main()
