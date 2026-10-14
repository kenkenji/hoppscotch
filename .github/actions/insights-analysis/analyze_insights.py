#!/usr/bin/env python3
"""
アーキテクチャモデルとPRマッピングデータからLLMで分析レポートを生成するスクリプト。

コンポーネントごとのPR数、共変更ペア、期間ごとの推移を集計し、
LLMに渡して改善提案を生成する。
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))
from llm_utils import call_llm, detect_provider, parse_llm_response
from schema_utils import check_schema_version


def format_components_for_prompt(components_data):
    """components.yamlのデータをプロンプト用テキストに整形する"""
    components = components_data.get("components", [])
    relations = components_data.get("relations", [])

    lines = []
    for c in components:
        comp_id = c.get("id", "")
        level = c.get("level", "")
        if not comp_id or not level:
            continue
        parent = c.get("parent", "")
        tech = c.get("technology", "")
        lines.append(
            f"- `{comp_id}` ({level}, parent: {parent}): "
            f"{c.get('name', '')} — {c.get('description', '')} [{tech}]"
        )

    if relations:
        lines.append("")
        lines.append("### コンポーネント間の関係")
        for r in relations:
            r_from = r.get("from", "")
            r_to = r.get("to", "")
            if not r_from or not r_to:
                continue
            lines.append(
                f"- `{r_from}` → `{r_to}`: {r.get('description', '')} [{r.get('technology', '')}]"
            )

    return "\n".join(lines)


def aggregate_data(components_data, timeline_data):
    """timeline.jsonからコンポーネントごとのPR数、共変更ペア、月別推移を集計する"""
    entries = timeline_data.get("entries", [])
    valid_ids = {c["id"] for c in components_data.get("components", [])
                 if c.get("level") in ("container", "component")}
    name_map = {c["id"]: c.get("name", c["id"])
                for c in components_data.get("components", [])}

    prs_by_component = defaultdict(set)
    monthly_prs = defaultdict(lambda: defaultdict(set))
    co_change_count = defaultdict(int)
    total_prs = set()

    for entry in entries:
        if entry.get("no_impact"):
            continue

        pr_number = entry.get("pr_number")
        if pr_number is None:
            continue

        total_prs.add(pr_number)
        date_str = entry.get("merged_at") or entry.get("timestamp", "")
        month = date_str[:7] if len(date_str) >= 7 else "unknown"

        comps = sorted(set(c for c in entry.get("components", []) if c in valid_ids))

        for comp_id in comps:
            prs_by_component[comp_id].add(pr_number)
            monthly_prs[comp_id][month].add(pr_number)

        if len(comps) >= 2:
            for i in range(len(comps)):
                for j in range(i + 1, len(comps)):
                    pair_key = f"{comps[i]}+{comps[j]}"
                    co_change_count[pair_key] += 1

    hotspots = []
    for comp_id, pr_set in sorted(prs_by_component.items(), key=lambda x: -len(x[1])):
        period_counts = []
        for month in sorted(monthly_prs[comp_id].keys()):
            period_counts.append({"month": month, "count": len(monthly_prs[comp_id][month])})
        hotspots.append({
            "component_id": comp_id,
            "component_name": name_map.get(comp_id, comp_id),
            "pr_count": len(pr_set),
            "period_pr_counts": period_counts,
        })

    co_changes = []
    for pair_key, count in sorted(co_change_count.items(), key=lambda x: -x[1]):
        comp_a, comp_b = pair_key.split("+")
        total_a = len(prs_by_component.get(comp_a, set()))
        total_b = len(prs_by_component.get(comp_b, set()))
        max_total = max(total_a, total_b, 1)
        co_changes.append({
            "components": [comp_a, comp_b],
            "component_names": [name_map.get(comp_a, comp_a), name_map.get(comp_b, comp_b)],
            "co_change_count": count,
            "co_change_ratio": round(count / max_total, 2),
        })

    return {
        "total_prs_analyzed": len(total_prs),
        "components_analyzed": len(prs_by_component),
        "hotspots": hotspots,
        "co_changes": co_changes,
    }


def format_aggregated_data(agg):
    """集計データをプロンプト用テキストに整形する"""
    lines = []
    lines.append(f"分析対象PR数: {agg['total_prs_analyzed']}")
    lines.append(f"分析対象コンポーネント数: {agg['components_analyzed']}")
    lines.append("")

    lines.append("### コンポーネントごとのPR数（変更回数順）")
    for h in agg["hotspots"]:
        lines.append(f"- `{h['component_id']}` ({h['component_name']}): {h['pr_count']}件")
        if h["period_pr_counts"]:
            months_str = ", ".join(
                f"{p['month']}: {p['count']}件" for p in h["period_pr_counts"]
            )
            lines.append(f"  月別: {months_str}")

    if agg["co_changes"]:
        lines.append("")
        lines.append("### 共変更ペア（共変更回数順）")
        for cc in agg["co_changes"]:
            lines.append(
                f"- `{cc['components'][0]}` + `{cc['components'][1]}`: "
                f"{cc['co_change_count']}回 (共変更率: {cc['co_change_ratio']:.0%})"
            )

    return "\n".join(lines)


def build_prompt(template, components_text, aggregated_text):
    """プロンプトテンプレートにデータを埋め込む"""
    return (template
            .replace("{components}", components_text)
            .replace("{aggregated_data}", aggregated_text))


def validate_findings(findings, components_data):
    """LLMが返したfindingsのcomponent IDsを検証する"""
    valid_ids = {c["id"] for c in components_data.get("components", [])
                 if c.get("level") in ("container", "component")}
    validated = []
    for i, finding in enumerate(findings):
        comps = finding.get("components", [])
        valid_comps = [c for c in comps if c in valid_ids]
        if not valid_comps and comps:
            print(
                f"Warning: finding '{finding.get('id', i)}' has no valid component IDs, skipping",
                file=sys.stderr,
            )
            continue
        finding["components"] = valid_comps
        if "status" not in finding:
            finding["status"] = "new"
        validated.append(finding)
    return validated


def build_report(findings, agg, model_version):
    """insights-report.jsonのデータ構造を組み立てる"""
    months = set()
    for h in agg["hotspots"]:
        for p in h["period_pr_counts"]:
            if p["month"] != "unknown":
                months.add(p["month"])

    sorted_months = sorted(months) if months else []
    period_from = sorted_months[0] + "-01" if sorted_months else ""
    period_to = ""
    if sorted_months:
        last = sorted_months[-1]
        year, month = int(last[:4]), int(last[5:7])
        if month == 12:
            period_to = f"{year + 1}-01-01"
        else:
            period_to = f"{year}-{month + 1:02d}-01"

    for finding in findings:
        if "resolved_at" not in finding:
            finding["resolved_at"] = None
        if "resolved_by" not in finding:
            finding["resolved_by"] = None
        if "resolution_note" not in finding:
            finding["resolution_note"] = None

    return {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_version": model_version,
        "analysis_period": {
            "from": period_from,
            "to": period_to,
        },
        "summary": {
            "total_prs_analyzed": agg["total_prs_analyzed"],
            "components_analyzed": agg["components_analyzed"],
            "findings_count": len(findings),
        },
        "findings": findings,
    }


def main():
    parser = argparse.ArgumentParser(
        description="LLMでアーキテクチャ分析レポートを生成する"
    )
    parser.add_argument(
        "--components-file", required=True,
        help="components.yamlファイルのパス",
    )
    parser.add_argument(
        "--timeline-file", required=True,
        help="timeline.jsonファイルのパス",
    )
    parser.add_argument(
        "--prompt-template", required=True,
        help="プロンプトテンプレートファイルのパス",
    )
    parser.add_argument(
        "--model-version", default="",
        help="現在のモデルバージョン",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "claude-code"],
        help="LLMプロバイダー（未指定時は環境変数から自動検出）",
    )
    parser.add_argument(
        "--model",
        help="モデル名（デフォルト: プロバイダーに応じた標準モデル）",
    )
    parser.add_argument(
        "--output", default="-",
        help="出力ファイルパス（デフォルト: stdout）",
    )
    args = parser.parse_args()

    provider = args.provider or detect_provider()

    if provider is None:
        print(
            "::error::No LLM API key configured. "
            "Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN in repository secrets.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(args.components_file, encoding="utf-8") as f:
        components_data = yaml.safe_load(f)
    check_schema_version(components_data, args.components_file)

    with open(args.timeline_file, encoding="utf-8") as f:
        timeline_data = json.load(f)

    with open(args.prompt_template, encoding="utf-8") as f:
        template = f.read()

    agg = aggregate_data(components_data, timeline_data)

    if agg["total_prs_analyzed"] == 0:
        print("⚠️ No PR data to analyze", file=sys.stderr)
        report = build_report([], agg, args.model_version)
        output = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output == "-":
            print(output)
        else:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
        return

    components_text = format_components_for_prompt(components_data)
    aggregated_text = format_aggregated_data(agg)
    prompt = build_prompt(template, components_text, aggregated_text)

    model = args.model or None

    try:
        raw_response = call_llm(provider, prompt, model, max_tokens=4096)
    except Exception as e:
        print(f"::error::LLM API call failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        parsed = parse_llm_response(raw_response)
        raw_findings = parsed.get("findings", [])
    except json.JSONDecodeError as e:
        print(f"::error::Failed to parse LLM response as JSON: {e}", file=sys.stderr)
        sys.exit(1)

    findings = validate_findings(raw_findings, components_data)

    report = build_report(findings, agg, args.model_version)
    output = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)

    print(f"✅ Analysis complete: {len(findings)} finding(s) generated", file=sys.stderr)


if __name__ == "__main__":
    main()
