"""components.yamlのバージョンバンプとmodel-changelog.jsonの更新を行う。"""

import argparse
import json
import os
from datetime import datetime, timezone

import yaml


def parse_version(version_str: str) -> int:
    """バージョン文字列を整数に変換する。

    "1.0"/"1.1"等のドット付きはpre-scheme扱いで1を返す。
    整数文字列はそのまま変換。不正値は1を返す。
    """
    if not version_str:
        return 1
    version_str = str(version_str).strip()
    if "." in version_str:
        return 1
    try:
        n = int(version_str)
        return n if n >= 1 else 1
    except (ValueError, TypeError):
        return 1


def bump_version(yaml_path: str) -> tuple:
    """components.yamlのversionを+1してファイルに書き戻す。"""
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    old_version = parse_version(data.get("version", ""))
    new_version = old_version + 1
    data["version"] = str(new_version)

    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False)

    return old_version, new_version


def update_changelog(changelog_path: str, model_version: int,
                     changes: list, trigger: dict, author: str):
    """model-changelog.jsonにエントリを追加する。ファイルが無ければ初期化する。"""
    if os.path.exists(changelog_path):
        with open(changelog_path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"version": "1.0", "entries": []}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "model_version": str(model_version),
        "timestamp": now,
        "changes": changes,
        "trigger": trigger,
        "author": author,
    }
    data["entries"].insert(0, entry)

    with open(changelog_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Bump components.yaml version and update changelog")
    parser.add_argument("--components-file", required=True,
                        help="Path to components.yaml")
    parser.add_argument("--changelog-file", required=True,
                        help="Path to model-changelog.json")
    parser.add_argument("--changes", default="[]",
                        help="JSON array of change objects")
    parser.add_argument("--trigger-type", default="manual",
                        choices=["pr", "manual", "auto"])
    parser.add_argument("--trigger-pr-number", type=int, default=None)
    parser.add_argument("--trigger-pr-url", default=None)
    parser.add_argument("--author", default="github-actions[bot]")
    return parser.parse_args()


def main():
    args = parse_args()
    changes = json.loads(args.changes)

    old_version, new_version = bump_version(args.components_file)

    trigger = {"type": args.trigger_type}
    if args.trigger_pr_number is not None:
        trigger["pr_number"] = args.trigger_pr_number
    if args.trigger_pr_url is not None:
        trigger["pr_url"] = args.trigger_pr_url

    update_changelog(args.changelog_file, new_version, changes, trigger,
                     args.author)

    print(f"previous_version={old_version}")
    print(f"new_version={new_version}")


if __name__ == "__main__":
    main()
