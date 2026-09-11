#!/usr/bin/env python3
"""承認済み提案をcomponents.yamlに適用する（冪等）。"""

import argparse
import json
import re
import sys

import yaml

ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_STRING_LENGTH = 200


def sanitize(value):
    if not isinstance(value, str):
        return str(value)[:MAX_STRING_LENGTH]
    return value.strip()[:MAX_STRING_LENGTH]


def validate_id(comp_id):
    return bool(comp_id and isinstance(comp_id, str) and ID_PATTERN.match(comp_id))


def find_component(components, comp_id):
    for i, c in enumerate(components):
        if c["id"] == comp_id:
            return i
    return -1


def apply_add_component(data, proposal):
    comp_id = proposal.get("id", "")
    if not validate_id(comp_id):
        return {"id": comp_id, "status": "skipped", "reason": "invalid_id"}

    components = data.get("components", [])
    if find_component(components, comp_id) >= 0:
        return {"id": comp_id, "status": "skipped", "reason": "already_exists"}

    new_component = {
        "id": comp_id,
        "name": sanitize(proposal.get("name", comp_id)),
        "level": proposal.get("level", "component"),
        "description": sanitize(proposal.get("description", "")),
    }

    parent = proposal.get("parent", "")
    if parent:
        new_component["parent"] = sanitize(parent)

    tech = proposal.get("technology", "")
    if tech:
        new_component["technology"] = sanitize(tech)

    parent_idx = find_component(components, parent) if parent else -1
    if parent_idx >= 0:
        insert_idx = parent_idx + 1
        while insert_idx < len(components) and components[insert_idx].get("parent") == parent:
            insert_idx += 1
        components.insert(insert_idx, new_component)
    else:
        components.append(new_component)

    data["components"] = components
    return {"id": comp_id, "status": "applied", "action": "add"}


def apply_remove_component(data, proposal):
    comp_id = proposal.get("id", "")
    if not validate_id(comp_id):
        return {"id": comp_id, "status": "skipped", "reason": "invalid_id"}

    components = data.get("components", [])
    idx = find_component(components, comp_id)
    if idx < 0:
        return {"id": comp_id, "status": "skipped", "reason": "not_found"}

    children = [c for c in components if c.get("parent") == comp_id]
    if children:
        child_ids = [c["id"] for c in children]
        return {"id": comp_id, "status": "skipped",
                "reason": f"has_children: {', '.join(child_ids)}"}

    relations = data.get("relations", [])
    removed_relations = [f"{r.get('from')} -> {r.get('to')}"
                         for r in relations
                         if r.get("from") == comp_id or r.get("to") == comp_id]

    components.pop(idx)
    data["components"] = components

    data["relations"] = [r for r in relations
                         if r.get("from") != comp_id and r.get("to") != comp_id]

    result = {"id": comp_id, "status": "applied", "action": "remove"}
    if removed_relations:
        result["cascade_removed_relations"] = removed_relations
    return result


def apply_modify_component(data, proposal):
    comp_id = proposal.get("id", "")
    if not validate_id(comp_id):
        return {"id": comp_id, "status": "skipped", "reason": "invalid_id"}

    components = data.get("components", [])
    idx = find_component(components, comp_id)
    if idx < 0:
        return {"id": comp_id, "status": "skipped", "reason": "not_found"}

    comp = components[idx]
    changed_fields = []

    for field in ("name", "description", "technology"):
        if field in proposal and proposal[field]:
            new_val = sanitize(proposal[field])
            if comp.get(field) != new_val:
                comp[field] = new_val
                changed_fields.append(field)

    if not changed_fields:
        return {"id": comp_id, "status": "skipped", "reason": "no_changes"}

    return {"id": comp_id, "status": "applied", "action": "modify",
            "changed_fields": changed_fields}


def apply_add_relation(data, proposal):
    from_id = proposal.get("from", "")
    to_id = proposal.get("to", "")
    if not from_id or not to_id:
        return {"relation": f"{from_id} -> {to_id}", "status": "skipped",
                "reason": "missing_ids"}

    relations = data.setdefault("relations", [])

    for r in relations:
        if r.get("from") == from_id and r.get("to") == to_id:
            return {"relation": f"{from_id} -> {to_id}", "status": "skipped",
                    "reason": "already_exists"}

    component_ids = {c["id"] for c in data.get("components", [])}
    if from_id not in component_ids or to_id not in component_ids:
        return {"relation": f"{from_id} -> {to_id}", "status": "skipped",
                "reason": "unknown_component"}

    new_relation = {
        "from": from_id,
        "to": to_id,
        "description": sanitize(proposal.get("description", "")),
    }
    tech = proposal.get("technology", "")
    if tech:
        new_relation["technology"] = sanitize(tech)

    relations.append(new_relation)
    return {"relation": f"{from_id} -> {to_id}", "status": "applied", "action": "add_relation"}


def apply_proposals(data, proposals):
    results = []
    for p in proposals:
        action = p.get("action", "")
        target = p.get("target", "component")

        if target == "relation" or (action == "add" and "from" in p and "to" in p):
            results.append(apply_add_relation(data, p))
        elif action == "add":
            results.append(apply_add_component(data, p))
        elif action == "remove":
            results.append(apply_remove_component(data, p))
        elif action == "modify":
            results.append(apply_modify_component(data, p))
        else:
            results.append({"action": action, "status": "skipped", "reason": "unknown_action"})

    return results


def main():
    parser = argparse.ArgumentParser(description="承認済み提案をcomponents.yamlに適用する")
    parser.add_argument("--proposals", required=True,
                        help="承認済み提案のJSON文字列またはファイルパス")
    parser.add_argument("--components-file", required=True,
                        help="components.yamlファイルのパス")
    args = parser.parse_args()

    if args.proposals.startswith("[") or args.proposals.startswith("{"):
        proposals = json.loads(args.proposals)
    else:
        with open(args.proposals, encoding="utf-8") as f:
            proposals = json.load(f)

    if isinstance(proposals, dict):
        proposals = proposals.get("approved_proposals", [])

    if not proposals:
        print(json.dumps({"results": [], "applied_count": 0}))
        return

    with open(args.components_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    results = apply_proposals(data, proposals)

    with open(args.components_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    applied_count = sum(1 for r in results if r.get("status") == "applied")
    output = {
        "results": results,
        "applied_count": applied_count,
    }
    print(json.dumps(output, ensure_ascii=False))

    for r in results:
        status = r.get("status", "")
        if status == "applied":
            action = r.get("action", "")
            target = r.get("id", r.get("relation", ""))
            cascade = r.get("cascade_removed_relations", [])
            print(f"  ✅ {action}: {target}", file=sys.stderr)
            for rel in cascade:
                print(f"     ↳ cascade removed relation: {rel}", file=sys.stderr)
        elif status == "skipped":
            reason = r.get("reason", "")
            target = r.get("id", r.get("relation", ""))
            print(f"  ⏭️ skipped ({reason}): {target}", file=sys.stderr)


if __name__ == "__main__":
    main()
