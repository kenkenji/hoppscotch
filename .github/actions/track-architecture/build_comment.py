"""components.yaml からチェックボックス付きコメント本文を生成する。"""

import argparse
import json
import re
import sys
import yaml


# record-mapping/parse_checkboxes.py の NO_IMPACT_ID と同期が必要
NO_IMPACT_ID = "__no_impact__"

_MD_ESCAPE_RE = re.compile(r"([\[\]()|`<>])")


def escape_markdown(text):
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--components-file", required=True, help="Path to components.yaml")
    parser.add_argument(
        "--ai-components",
        default="",
        help="JSON array of AI-suggested component IDs (empty = manual mode)",
    )
    parser.add_argument(
        "--no-impact-default",
        action="store_true",
        help="Pre-check the no-impact checkbox (used when AI finds 0 components)",
    )
    parser.add_argument(
        "--proposals",
        default="",
        help="JSON array of structural change proposals (from detect-changes)",
    )
    parser.add_argument(
        "--detection-method",
        default="",
        help="Detection method used (llm / none / skip)",
    )
    parser.add_argument(
        "--spa-url",
        default="",
        help="SPA base URL for editor link",
    )
    parser.add_argument(
        "--checked-components",
        default="",
        help="JSON array of currently checked component IDs (for regeneration)",
    )
    parser.add_argument(
        "--no-impact-checked",
        action="store_true",
        help="Pre-check the no-impact checkbox (for regeneration)",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Enable auto-approve mode (record mapping immediately without user action)",
    )
    return parser.parse_args()


def build_checkbox_section(data, ai_component_ids=None, checked_ids=None):
    if ai_component_ids is None:
        ai_component_ids = set()
    else:
        ai_component_ids = set(ai_component_ids)

    if checked_ids is not None:
        pre_checked = set(checked_ids) | ai_component_ids
    else:
        pre_checked = ai_component_ids

    components = data.get("components", [])

    systems = {c["id"]: c["name"] for c in components if c.get("level") == "system"}
    containers = [c for c in components if c.get("level") == "container"]
    component_items = [c for c in components if c.get("level") == "component"]

    lines = []
    for container in containers:
        system_label = ""
        parent = container.get("parent")
        if parent and parent in systems:
            system_label = f" ({systems[parent]})"

        lines.append(f"### {container['name']}{system_label}")
        lines.append("")

        check = "x" if container["id"] in pre_checked else " "
        lines.append(f"- [{check}] `{container['id']}` — {container['name']}")

        children = [c for c in component_items if c.get("parent") == container["id"]]
        for child in children:
            check = "x" if child["id"] in pre_checked else " "
            lines.append(f"  - [{check}] `{child['id']}` — {child['name']}")

        lines.append("")

    return "\n".join(lines)


def build_no_impact_line(checked=False):
    mark = "x" if checked else " "
    return f"- [{mark}] `{NO_IMPACT_ID}` — 影響なし（このPRはアーキテクチャに影響しません）"


def _source_label(source):
    labels = {
        "llm": "AI",
    }
    return labels.get(source, source or "")


def _action_label(proposal):
    if proposal.get("target") == "relation":
        return "Relation追加"
    action = proposal.get("action", "add")
    return {"add": "追加", "remove": "削除", "modify": "変更"}.get(action, escape_markdown(action))


def _proposal_target(proposal):
    if proposal.get("target") == "relation":
        from_id = escape_markdown(proposal.get("from", ""))
        to_id = escape_markdown(proposal.get("to", ""))
        return f"`{from_id}` → `{to_id}`"
    return f"`{escape_markdown(proposal.get('id', ''))}`"


def _proposal_summary(proposal):
    desc = escape_markdown(proposal.get("description", ""))
    source = _source_label(proposal.get("source", ""))
    if source:
        return f"{desc}（{source}）"
    return desc


def _status_label(status):
    labels = {
        "accepted": "✅ 適用",
        "rejected": "❌ 却下",
    }
    return labels.get(status, "")


def build_proposals_section(proposals, detection_method, pr_number=""):
    if not proposals:
        return ""

    method_label = "AI" if detection_method == "llm" else detection_method
    has_status = any(p.get("status") in ("accepted", "rejected") for p in proposals)

    lines = []
    lines.append("### 🔄 構造変更の検出")
    lines.append("")
    lines.append(
        f"このPRのマージにより、アーキテクチャモデルの構造変更が検出されました"
        f"（検出: {method_label}）。"
    )
    lines.append("")
    if has_status:
        lines.append("| 種別 | 対象 | 概要 | 状態 |")
        lines.append("|------|------|------|------|")
    else:
        lines.append("| 種別 | 対象 | 概要 |")
        lines.append("|------|------|------|")
    for p in proposals:
        action = _action_label(p)
        target = _proposal_target(p)
        summary = _proposal_summary(p)
        if has_status:
            status = _status_label(p.get("status", ""))
            lines.append(f"| {action} | {target} | {summary} | {status} |")
        else:
            lines.append(f"| {action} | {target} | {summary} |")
    lines.append("")
    return "\n".join(lines)


def build_editor_link_section(spa_url, pr_number=""):
    if not spa_url:
        return ""
    editor_url = f"{spa_url}/editor?pr={pr_number}"
    lines = []
    lines.append(f'> 📝 <a href="{editor_url}" target="_blank">アーキテクチャモデルを編集する</a>')
    lines.append("")
    return "\n".join(lines)


def build_comment(pr_number, pr_title, data, source="manual", ai_component_ids=None,
                  no_impact_default=False, proposals=None, detection_method="",
                  spa_url="", checked_ids=None, auto_approve=False):
    checkbox_section = build_checkbox_section(data, ai_component_ids,
                                              checked_ids=checked_ids)

    if auto_approve and source == "ai":
        intro = "✅ 自動承認モードにより自動記録されました。必要に応じてチェックボックスを修正してください（修正内容は自動的に反映されます）。"
    elif source == "ai":
        intro = "AIがこのPRの影響コンポーネントを提案しました（✅ = AI提案済み）。必要に応じて修正してください。"
    else:
        intro = "このPRが影響したコンポーネントを選択してください。"

    ai_marker = ""
    if source == "ai" and ai_component_ids:
        ai_marker = f"\n<!-- ai-components: {json.dumps(ai_component_ids, ensure_ascii=False)} -->"

    auto_approve_marker = ""
    if auto_approve:
        auto_approve_marker = "\n<!-- auto-approved: true -->"

    proposals_markers = ""
    if proposals:
        proposals_markers = f"\n<!-- has-proposals: true -->\n<!-- detection-method: {detection_method} -->"

    no_impact_line = build_no_impact_line(checked=no_impact_default)

    proposals_section = build_proposals_section(
        proposals, detection_method, pr_number=pr_number)
    editor_link_section = build_editor_link_section(spa_url, pr_number=pr_number)

    extra_sections = f"{proposals_section}{editor_link_section}"
    if extra_sections.strip():
        proposals_block = f"\n{extra_sections}\n---\n"
    else:
        proposals_block = ""

    return f"""\
## 🏗 Architecture Tracker

<!-- source: {source} -->{ai_marker}{auto_approve_marker}{proposals_markers}

**PR #{pr_number}**: {escape_markdown(pr_title)}

{intro}

{checkbox_section}
### その他

{no_impact_line}

---
{proposals_block}<sub>🤖 このコメントは <a href="https://github.com/kenkenji/architecture-tracker">Architecture Tracker</a> が自動投稿しました</sub>"""


def main():
    args = parse_args()
    with open(args.components_file, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "components" not in data:
        print("::error::Invalid components.yaml: 'components' key not found", file=sys.stderr)
        sys.exit(1)

    ai_ids = []
    source = "manual"
    if args.ai_components:
        ai_ids = json.loads(args.ai_components)
        if ai_ids:
            source = "ai"

    proposals = []
    if args.proposals:
        proposals = json.loads(args.proposals)

    checked_ids = None
    if args.checked_components:
        checked_ids = json.loads(args.checked_components)

    no_impact = args.no_impact_default or args.no_impact_checked

    print(build_comment(args.pr_number, args.pr_title, data, source=source,
                        ai_component_ids=ai_ids, no_impact_default=no_impact,
                        proposals=proposals, detection_method=args.detection_method,
                        spa_url=args.spa_url,
                        checked_ids=checked_ids,
                        auto_approve=args.auto_approve))


if __name__ == "__main__":
    main()
