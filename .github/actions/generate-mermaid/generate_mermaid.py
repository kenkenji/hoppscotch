"""components.yamlとmappings.jsonからMermaid図を生成する。"""

import argparse
import json
import re
import sys
import yaml


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--components-file", required=True)
    parser.add_argument("--mappings-file", required=True)
    parser.add_argument("--pr-number", type=int, default=None)
    parser.add_argument("--output-comment", default=None, help="Path to write PR comment body")
    return parser.parse_args()


def load_components(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_mappings(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"mappings": {}}


def _sanitize_id(node_id):
    """MermaidのFlowchartで安全に使えるノードIDに変換する。"""
    return re.sub(r'[^a-zA-Z0-9_]', '_', node_id)


def escape_label(text):
    """Flowchartのノードラベル用エスケープ。"""
    return text.replace('"', "'")


def build_structure_diagram(data, highlighted_ids=None):
    """構成をMermaidのFlowchartダイアグラムで表現する。"""
    if highlighted_ids is None:
        highlighted_ids = set()

    components = data.get("components", [])
    relations = data.get("relations", [])

    systems = [c for c in components if c.get("level") == "system"]
    containers = [c for c in components if c.get("level") == "container"]
    component_items = [c for c in components if c.get("level") == "component"]

    all_ids = {c["id"] for c in components}
    system_ids = {c["id"] for c in systems}
    container_ids = {c["id"] for c in containers}
    for c in components:
        parent = c.get("parent")
        if parent and parent not in all_ids:
            print(f"::warning::Component '{c['id']}' references unknown parent '{parent}'", file=sys.stderr)
        if c.get("level") == "container" and c.get("parent") not in system_ids:
            print(f"::warning::Container '{c['id']}' has no valid parent system, will not appear in diagram", file=sys.stderr)
        if c.get("level") == "component" and c.get("parent") not in container_ids:
            print(f"::warning::Component '{c['id']}' has no valid parent container, will not appear in diagram", file=sys.stderr)

    lines = ["flowchart TB"]

    for system in systems:
        sid = system["id"]
        safe_id = _sanitize_id(sid)
        is_external = "external" in system.get("tags", [])
        children_containers = [c for c in containers if c.get("parent") == sid]
        name = escape_label(system["name"])

        if is_external:
            lines.append(f'    {safe_id}["{name}"]:::external')
        elif children_containers:
            lines.append(f'    subgraph {safe_id}["{name}"]')
            for container in children_containers:
                _add_container_flowchart(lines, container, component_items)
            lines.append("    end")
        else:
            lines.append(f'    {safe_id}["{name}"]')

    for rid in {r.get("from") for r in relations} | {r.get("to") for r in relations}:
        if rid and rid not in all_ids:
            print(f"::warning::Relation references unknown component '{rid}'", file=sys.stderr)

    for rel in relations:
        from_id = _sanitize_id(rel["from"])
        to_id = _sanitize_id(rel["to"])
        desc = escape_label(rel.get("description", ""))
        tech = escape_label(rel.get("technology", ""))
        if tech and desc:
            lines.append(f'    {from_id} -->|"{desc} / {tech}"| {to_id}')
        elif tech:
            lines.append(f'    {from_id} -->|"{tech}"| {to_id}')
        elif desc:
            lines.append(f'    {from_id} -->|"{desc}"| {to_id}')
        else:
            lines.append(f'    {from_id} --> {to_id}')

    if highlighted_ids:
        affected_safe_ids = [_sanitize_id(cid) for cid in sorted(highlighted_ids)]
        lines.append("    classDef affected fill:#fdda0d,stroke:#e6b800,stroke-width:2px,color:#333")
        lines.append(f"    class {','.join(affected_safe_ids)} affected")

    lines.append("    classDef external fill:#999,stroke:#666,stroke-dasharray:5 5")

    return "\n".join(lines)


def _add_container_flowchart(lines, container, component_items):
    cid = container["id"]
    safe_id = _sanitize_id(cid)
    children = [c for c in component_items if c.get("parent") == cid]
    name = escape_label(container["name"])
    tech = container.get("technology", "")

    if children:
        label = name
        if tech:
            label = f"{name}<br/><i>{escape_label(tech)}</i>"
        lines.append(f'        subgraph {safe_id}["{label}"]')
        for child in children:
            child_name = escape_label(child["name"])
            child_tech = child.get("technology", "")
            child_safe_id = _sanitize_id(child["id"])
            child_label = child_name
            if child_tech:
                child_label = f"{child_name}<br/><i>{escape_label(child_tech)}</i>"
            lines.append(f'            {child_safe_id}["{child_label}"]')
        lines.append("        end")
    else:
        label = name
        if tech:
            label = f"{name}<br/><i>{escape_label(tech)}</i>"
        lines.append(f'        {safe_id}["{label}"]')


def build_pr_comment(data, pr_number, highlighted_ids, no_impact=False):
    diagram = build_structure_diagram(data, highlighted_ids)
    component_names = {c["id"]: c["name"] for c in data.get("components", [])}

    affected_list = ", ".join(
        f"`{cid}` ({component_names.get(cid, cid)})"
        for cid in sorted(highlighted_ids)
    )

    lines = [
        f"## 📊 Architecture Impact — PR #{pr_number}",
        "",
    ]

    if highlighted_ids:
        lines += [
            f"**影響コンポーネント**: {affected_list}",
            "",
            "```mermaid",
            diagram,
            "```",
            "",
            "> ハイライトされたノードがこのPRの影響範囲です",
        ]
    elif no_impact:
        lines += [
            "このPRはアーキテクチャへの影響なしと記録されました。",
        ]
    else:
        lines += [
            "コンポーネントのマッピングがありません。",
        ]

    lines += [
        "",
        "---",
        "<sub>🤖 <a href=\"https://github.com/kenkenji/architecture-tracker\">Architecture Tracker</a></sub>",
        "",
    ]
    return "\n".join(lines)


def main():
    args = parse_args()
    data = load_components(args.components_file)
    if not data or "components" not in data:
        print("::error::Invalid components.yaml", file=sys.stderr)
        sys.exit(1)

    mappings_data = load_mappings(args.mappings_file)

    if args.pr_number and args.output_comment:
        highlighted_ids = set()
        no_impact = False
        pr_key = str(args.pr_number)
        if pr_key in mappings_data.get("mappings", {}):
            mapping = mappings_data["mappings"][pr_key]
            highlighted_ids = set(mapping.get("components", []))
            no_impact = mapping.get("no_impact", False)

        comment = build_pr_comment(data, args.pr_number, highlighted_ids, no_impact=no_impact)
        with open(args.output_comment, "w", encoding="utf-8") as f:
            f.write(comment)
        print(f"Generated {args.output_comment}")


if __name__ == "__main__":
    main()
