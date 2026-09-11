"""mappings.jsonとtimeline.jsonを更新する。"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import yaml


def read_model_version(components_path: str) -> str:
    """components.yamlからモデルバージョンを読み取る。"""
    try:
        with open(components_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return str(data.get("version", ""))
    except (OSError, yaml.YAMLError):
        return ""


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mappings-file", required=True)
    parser.add_argument("--timeline-file", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--merged-at", required=True)
    parser.add_argument("--components", required=True, help="JSON array of component ids")
    parser.add_argument("--author", required=True)
    parser.add_argument("--source", default="manual", choices=["manual", "ai"])
    parser.add_argument("--ai-components", default="", help="JSON array of AI-suggested component ids")
    parser.add_argument("--no-impact", action="store_true", help="Mark as intentionally no impact")
    parser.add_argument("--model-version", default=None, help="Current model version from components.yaml")
    parser.add_argument("--additions", type=int, default=None, help="Number of added lines")
    parser.add_argument("--deletions", type=int, default=None, help="Number of deleted lines")
    parser.add_argument("--changed-files", type=int, default=None, help="Number of changed files")
    parser.add_argument("--labels", default=None, help="JSON array of label names")
    parser.add_argument("--auto-approved", action="store_true", help="Mark as auto-approved by auto_approve mode")
    return parser.parse_args()


def _mapping_key(pr_number, source_repo=None):
    """mappings.jsonのキーを生成する。外部リポジトリは "owner/repo#N" 形式。"""
    if source_repo:
        return f"{source_repo}#{pr_number}"
    return str(pr_number)


def update_mappings(data, pr_number, pr_title, pr_url, merged_at, components,
                    author, timestamp, source="manual", ai_components=None,
                    no_impact=False, model_version=None,
                    diff_stats=None, labels=None, auto_approved=False,
                    source_repo=None):
    if components and no_impact:
        no_impact = False
    entry = {
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_url": pr_url,
        "merged_at": merged_at,
        "components": components,
        "source": source,
        "author": author,
        "timestamp": timestamp,
    }
    if source_repo:
        entry["source_repo"] = source_repo
    if ai_components is not None:
        entry["ai_components"] = ai_components
    if no_impact:
        entry["no_impact"] = True
    if model_version is not None:
        entry["model_version"] = model_version
    if diff_stats is not None:
        entry["diff_stats"] = diff_stats
    if labels is not None:
        entry["labels"] = labels
    if auto_approved:
        entry["auto_approved"] = True
    key = _mapping_key(pr_number, source_repo)
    data["mappings"][key] = entry
    return data


def _is_duplicate_timeline_entry(entries, pr_number, components, source, source_repo=None):
    """同一PRのエントリにcomponents+sourceが一致するものがあるか判定する。"""
    sorted_components = sorted(components)
    for entry in entries:
        if entry.get("pr_number") == pr_number and entry.get("source_repo") == source_repo:
            if entry.get("source") == source and sorted(entry.get("components") or []) == sorted_components:
                return True
    return False


def _entry_sort_key(entry):
    """エントリのソートキーを返す。merged_atがあればそちらを優先する。"""
    return entry.get("merged_at") or entry.get("timestamp", "")


def _find_insertion_index(entries, merged_at):
    """merged_atを基準に降順を維持する挿入位置を二分探索で決定する。"""
    lo, hi = 0, len(entries)
    while lo < hi:
        mid = (lo + hi) // 2
        if _entry_sort_key(entries[mid]) >= merged_at:
            lo = mid + 1
        else:
            hi = mid
    return lo


def update_timeline(data, pr_number, pr_title, pr_url, components,
                    author, timestamp, source="manual", ai_components=None,
                    no_impact=False, diff_stats=None, labels=None,
                    auto_approved=False, merged_at=None, source_repo=None):
    if components and no_impact:
        no_impact = False

    if _is_duplicate_timeline_entry(data.get("entries", []), pr_number, components, source, source_repo):
        print(f"Skipped duplicate timeline entry for PR #{pr_number}")
        return data

    compact = timestamp.replace("-", "").replace(":", "").replace(".", "")
    if source_repo:
        entry_id = f"pr-{source_repo}#{pr_number}-{compact}"
    else:
        entry_id = f"pr-{pr_number}-{compact}"

    entry = {
        "id": entry_id,
        "timestamp": timestamp,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_url": pr_url,
        "components": components,
        "source": source,
        "author": author,
    }
    if source_repo:
        entry["source_repo"] = source_repo
    if merged_at is not None:
        entry["merged_at"] = merged_at
    if ai_components is not None:
        entry["ai_components"] = ai_components
    if no_impact:
        entry["no_impact"] = True
    if diff_stats is not None:
        entry["diff_stats"] = diff_stats
    if labels is not None:
        entry["labels"] = labels
    if auto_approved:
        entry["auto_approved"] = True

    if merged_at is not None:
        idx = _find_insertion_index(data["entries"], merged_at)
        data["entries"].insert(idx, entry)
    else:
        data["entries"].insert(0, entry)
    return data


def main():
    args = parse_args()
    components = json.loads(args.components)
    ai_components = json.loads(args.ai_components) if args.ai_components else None
    labels = json.loads(args.labels) if args.labels else None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    diff_stats = None
    if args.additions is not None and args.deletions is not None and args.changed_files is not None:
        diff_stats = {
            "additions": args.additions,
            "deletions": args.deletions,
            "changed_files": args.changed_files,
        }

    with open(args.mappings_file, encoding="utf-8") as f:
        mappings_data = json.load(f)

    with open(args.timeline_file, encoding="utf-8") as f:
        timeline_data = json.load(f)

    mappings_data = update_mappings(
        mappings_data, args.pr_number, args.pr_title, args.pr_url,
        args.merged_at, components, args.author, now,
        source=args.source, ai_components=ai_components,
        no_impact=args.no_impact, model_version=args.model_version,
        diff_stats=diff_stats, labels=labels,
        auto_approved=args.auto_approved,
    )
    entry_count_before = len(timeline_data.get("entries", []))
    timeline_data = update_timeline(
        timeline_data, args.pr_number, args.pr_title, args.pr_url,
        components, args.author, now,
        source=args.source, ai_components=ai_components,
        no_impact=args.no_impact, diff_stats=diff_stats, labels=labels,
        auto_approved=args.auto_approved,
        merged_at=args.merged_at,
    )
    timeline_skipped = len(timeline_data["entries"]) == entry_count_before

    with open(args.mappings_file, "w", encoding="utf-8") as f:
        json.dump(mappings_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(args.timeline_file, "w", encoding="utf-8") as f:
        json.dump(timeline_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    if timeline_skipped:
        print(f"Updated mappings for PR #{args.pr_number} (timeline skipped: duplicate)")
    else:
        print(f"Updated mappings and timeline for PR #{args.pr_number}")
    print(f"Components: {components}")


if __name__ == "__main__":
    main()
