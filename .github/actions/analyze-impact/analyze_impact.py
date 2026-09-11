#!/usr/bin/env python3
"""
Issueの内容とアーキテクチャモデルから影響範囲を分析し、
結果をIssueコメントとして投稿するスクリプト。

環境変数 ANTHROPIC_API_KEY、OPENAI_API_KEY、CLAUDE_CODE_OAUTH_TOKEN の
いずれかが設定されている場合に対応するプロバイダーを使用する。
優先順位: ANTHROPIC_API_KEY > OPENAI_API_KEY > CLAUDE_CODE_OAUTH_TOKEN
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))
from llm_utils import call_llm, detect_provider, parse_llm_response


def format_components_for_prompt(components_data):
    components = components_data.get("components", [])
    relations = components_data.get("relations", [])

    lines = []
    lines.append("#### コンポーネント一覧")
    for c in components:
        comp_id = c.get("id", "")
        level = c.get("level", "")
        if not comp_id or not level:
            continue
        parent = c.get("parent", "")
        tech = c.get("technology", "")
        tags = c.get("tags", [])
        tags_str = f" [tags: {', '.join(tags)}]" if tags else ""
        lines.append(
            f"- `{comp_id}` ({level}, parent: {parent}): "
            f"{c.get('name', '')} — {c.get('description', '')} [{tech}]{tags_str}"
        )

    if relations:
        lines.append("")
        lines.append("#### コンポーネント間の関係")
        for r in relations:
            r_from = r.get("from", "")
            r_to = r.get("to", "")
            if not r_from or not r_to:
                continue
            lines.append(
                f"- `{r_from}` → `{r_to}`: {r.get('description', '')} [{r.get('technology', '')}]"
            )

    return "\n".join(lines)


def format_mappings_for_prompt(mappings_data):
    mappings = mappings_data.get("mappings", {})
    if not mappings:
        return "(変更履歴なし)"

    sorted_entries = sorted(
        mappings.values(),
        key=lambda m: m.get("merged_at") or m.get("timestamp", ""),
        reverse=True,
    )
    recent = sorted_entries[:30]

    lines = []
    for m in recent:
        pr_num = m.get("pr_number", "?")
        pr_title = m.get("pr_title", "")
        comps = ", ".join(m.get("components", []))
        merged_at = m.get("merged_at", "")
        if merged_at:
            merged_at = merged_at[:10]
        lines.append(f"- PR #{pr_num}: {pr_title} → [{comps}] ({merged_at})")

    return "\n".join(lines)


def build_prompt(template, components_data, mappings_data, issue_text):
    components_text = format_components_for_prompt(components_data)
    mappings_text = format_mappings_for_prompt(mappings_data)

    sentinel_components = "\x00COMPONENTS\x00"
    sentinel_mappings = "\x00MAPPINGS\x00"
    sentinel_issue = "\x00ISSUE\x00"

    result = template
    result = result.replace("{components}", sentinel_components)
    result = result.replace("{mappings}", sentinel_mappings)
    result = result.replace("{issue}", sentinel_issue)

    result = result.replace(sentinel_components, components_text)
    result = result.replace(sentinel_mappings, mappings_text)
    result = result.replace(sentinel_issue, issue_text)
    return result


VALID_IMPACT_LEVELS = {"high", "medium", "low"}


def validate_component_ids(affected_components, components_data):
    valid_ids = {
        c.get("id")
        for c in components_data.get("components", [])
        if c.get("level") in ("container", "component")
    }
    validated = []
    seen = set()
    for item in affected_components:
        if not isinstance(item, dict):
            print(f"Warning: non-dict item in affected_components, skipping", file=sys.stderr)
            continue
        cid = item.get("id", "")
        if cid not in valid_ids:
            print(f"Warning: invalid component ID '{cid}' returned by LLM, skipping", file=sys.stderr)
            continue
        if cid in seen:
            continue
        seen.add(cid)
        raw_level = (item.get("impact_level") or "").lower().strip()
        if raw_level not in VALID_IMPACT_LEVELS:
            print(f"Warning: invalid impact_level '{item.get('impact_level')}', defaulting to medium", file=sys.stderr)
            raw_level = "medium"
        item["impact_level"] = raw_level
        validated.append(item)
    level_order = {"high": 0, "medium": 1, "low": 2}
    validated.sort(key=lambda x: level_order.get(x.get("impact_level", "medium"), 1))
    return validated


def validate_related_prs(related_prs, mappings_data):
    known_prs = set()
    for pr_num_str, mapping in mappings_data.get("mappings", {}).items():
        known_prs.add(int(mapping.get("pr_number", pr_num_str)))

    validated = []
    seen = set()
    for pr in related_prs:
        if not isinstance(pr, dict):
            print(f"Warning: non-dict item in related_prs, skipping", file=sys.stderr)
            continue
        pr_num = pr.get("pr_number")
        if not isinstance(pr_num, int):
            continue
        if pr_num not in known_prs:
            print(f"Warning: PR #{pr_num} not found in mappings, skipping", file=sys.stderr)
            continue
        if pr_num in seen:
            continue
        seen.add(pr_num)
        validated.append(pr)
    return validated


def build_component_index(components_data):
    return {c["id"]: c for c in components_data.get("components", []) if "id" in c}


def get_component_info(comp_id, comp_index):
    return comp_index.get(comp_id, {"id": comp_id, "name": comp_id})


def get_component_name(comp_id, comp_index):
    return get_component_info(comp_id, comp_index).get("name", comp_id)


def get_recent_prs_for_component(comp_id, mappings_data, limit=5):
    prs = []
    for _key, m in mappings_data.get("mappings", {}).items():
        if comp_id in m.get("components", []):
            prs.append(m)

    prs.sort(
        key=lambda m: m.get("merged_at") or m.get("timestamp", ""),
        reverse=True,
    )
    return prs[:limit]


def get_model_last_updated(components_data, timeline_data):
    entries = timeline_data.get("entries", [])
    if entries:
        return entries[0].get("timestamp", "")[:10]

    version = components_data.get("version", "")
    if version:
        return f"version {version}"
    return "不明"


IMPACT_BADGE = {
    "high": "🔴 高",
    "medium": "🟡 中",
    "low": "🟢 低",
}


def get_affected_relations(affected_ids, components_data):
    relations = components_data.get("relations", [])
    affected_set = set(affected_ids)
    result = []
    for r in relations:
        r_from = r.get("from", "")
        r_to = r.get("to", "")
        if r_from in affected_set and r_to in affected_set:
            result.append(r)
    return result


COMMENT_MARKER = "## 🏗 Architecture Tracker — 影響範囲分析"


def build_comment(analysis, components_data, mappings_data, timeline_data):
    comp_index = build_component_index(components_data)

    lines = []
    lines.append(COMMENT_MARKER)
    lines.append("")

    summary = analysis.get("summary", "").replace("\n", " ").strip()
    if summary:
        lines.append(f"> {summary}")
        lines.append("")

    affected = analysis.get("affected_components", [])
    if affected:
        count_by_level = {}
        for item in affected:
            level = item.get("impact_level", "medium")
            count_by_level[level] = count_by_level.get(level, 0) + 1
        level_summary = " / ".join(
            f"{badge} {count_by_level[lv]}件"
            for lv, badge in IMPACT_BADGE.items()
            if lv in count_by_level
        )

        lines.append(f"### 影響コンポーネント ({len(affected)}件: {level_summary})")
        lines.append("")

        for item in affected:
            comp_id = item["id"]
            comp_info = get_component_info(comp_id, comp_index)
            comp_name = comp_info.get("name", comp_id)
            parent = comp_info.get("parent", "")
            tech = comp_info.get("technology", "")
            impact_level = item.get("impact_level", "medium")
            badge = IMPACT_BADGE.get(impact_level, "")
            reason = item.get("reason", "")

            parent_label = ""
            if parent:
                parent_name = get_component_name(parent, comp_index)
                parent_label = f" / {parent_name}"

            tech_label = f" `{tech}`" if tech else ""

            lines.append(f"- {badge} **{comp_name}** (`{comp_id}`{parent_label}){tech_label}<br>")
            if reason:
                lines.append(f"  {reason}")

            recent_prs = get_recent_prs_for_component(comp_id, mappings_data)
            if recent_prs:
                lines.append(f"  <details><summary>直近の変更 ({len(recent_prs)}件)</summary>")
                lines.append("")
                for pr in recent_prs:
                    pr_num = pr.get("pr_number", "?")
                    pr_title = pr.get("pr_title", "")
                    merged_at = (pr.get("merged_at") or "")[:10]
                    lines.append(f"  - #{pr_num} {pr_title} ({merged_at})")
                lines.append("")
                lines.append("  </details>")
            lines.append("")

        affected_ids = [item["id"] for item in affected]
        affected_relations = get_affected_relations(affected_ids, components_data)
        if affected_relations:
            lines.append("#### 影響コンポーネント間の関連")
            lines.append("")
            for r in affected_relations:
                from_name = get_component_name(r["from"], comp_index)
                to_name = get_component_name(r["to"], comp_index)
                desc = r.get("description", "")
                tech = r.get("technology", "")
                tech_label = f" [{tech}]" if tech else ""
                lines.append(f"- {from_name} → {to_name}: {desc}{tech_label}")
            lines.append("")
    else:
        lines.append("### 影響コンポーネント")
        lines.append("")
        lines.append("影響を受けるコンポーネントは特定されませんでした。")
        lines.append("")

    related_prs = analysis.get("related_prs", [])
    if related_prs:
        lines.append("### 関連PR")
        lines.append("")
        for pr in related_prs:
            pr_num = pr.get("pr_number", "?")
            pr_title = pr.get("pr_title", "")
            reason = pr.get("reason", "")
            lines.append(f"- #{pr_num} {pr_title} — {reason}")
        lines.append("")

    risks = analysis.get("risks", [])
    if risks:
        lines.append("### リスク・注意点")
        lines.append("")
        for risk in risks:
            lines.append(f"- ⚠️ {risk}")
        lines.append("")

    last_updated = get_model_last_updated(components_data, timeline_data)
    lines.append("---")
    lines.append(f"*この分析はアーキテクチャモデル（最終更新: {last_updated}）に基づいています。*")

    return "\n".join(lines)


def find_existing_comment(repo, issue_number):
    jq_filter = '.[] | select(.body | startswith("' + COMMENT_MARKER.replace('"', '\\"') + '")) | .id'
    cmd = [
        "gh", "api",
        f"repos/{repo}/issues/{issue_number}/comments",
        "--paginate",
        "--jq", jq_filter,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"Warning: failed to search existing comments: {result.stderr.strip()}",
              file=sys.stderr)
        return None
    ids = result.stdout.strip().splitlines()
    if ids and ids[-1]:
        return ids[-1]
    return None


def post_comment(repo, issue_number, body):
    existing_id = find_existing_comment(repo, issue_number)
    body_file = None
    try:
        body_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        )
        body_file.write(body)
        body_file.close()

        if existing_id:
            url = f"repos/{repo}/issues/comments/{existing_id}"
            method = "PATCH"
        else:
            url = f"repos/{repo}/issues/{issue_number}/comments"
            method = "POST"

        cmd = [
            "gh", "api", url,
            "--method", method,
            "-F", f"body=@{body_file.name}",
            "--jq", ".id",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to post comment: {result.stderr.strip()}")
        comment_id = result.stdout.strip()
        action = "Updated" if existing_id else "Posted"
        print(f"✅ {action} analysis comment (id: {comment_id}) on issue #{issue_number}")
        return comment_id
    finally:
        if body_file:
            os.unlink(body_file.name)


def main():
    parser = argparse.ArgumentParser(description="Issueから影響範囲を分析する")
    parser.add_argument("--components-file", required=True)
    parser.add_argument("--mappings-file", required=True)
    parser.add_argument("--timeline-file", required=True)
    parser.add_argument("--issue-file", required=True)
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--prompt-template", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--issue-comment-file", default="")
    args = parser.parse_args()

    provider = detect_provider()
    if provider is None:
        print(
            "No API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(args.components_file, encoding="utf-8") as f:
        components_data = yaml.safe_load(f) or {}

    with open(args.mappings_file, encoding="utf-8") as f:
        mappings_data = json.load(f) if os.path.getsize(args.mappings_file) > 2 else {}

    with open(args.timeline_file, encoding="utf-8") as f:
        timeline_data = json.load(f) if os.path.getsize(args.timeline_file) > 2 else {}

    with open(args.issue_file, encoding="utf-8") as f:
        issue_text = f.read()

    if args.issue_comment_file:
        with open(args.issue_comment_file, encoding="utf-8") as f:
            comment_text = f.read().strip()
        if comment_text:
            issue_text += f"\n\n---\n追加コメント:\n{comment_text}"

    with open(args.prompt_template, encoding="utf-8") as f:
        template = f.read()

    prompt = build_prompt(template, components_data, mappings_data, issue_text)

    try:
        model = args.model or None
        raw_response = call_llm(provider, prompt, model, max_tokens=4096)

        analysis = parse_llm_response(raw_response)

        affected = analysis.get("affected_components", [])
        analysis["affected_components"] = validate_component_ids(affected, components_data)

        related_prs = analysis.get("related_prs", [])
        analysis["related_prs"] = validate_related_prs(related_prs, mappings_data)

        comment_body = build_comment(
            analysis, components_data, mappings_data, timeline_data
        )
        post_comment(args.repo, args.issue_number, comment_body)

        print(f"Provider: {provider}")
        print(f"Affected components: {len(analysis['affected_components'])}")
        print(f"Related PRs: {len(analysis['related_prs'])}")
        print(f"Risks: {len(analysis.get('risks', []))}")

    except json.JSONDecodeError as e:
        print(f"Failed to parse LLM response as JSON: {e}", file=sys.stderr)
        sys.exit(1)

    except Exception as e:
        print(f"Analysis failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
