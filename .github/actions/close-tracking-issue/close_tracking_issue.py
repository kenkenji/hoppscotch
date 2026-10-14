"""PRのトラッキングIssueをクローズするかどうかと、クローズ時のコメント文言を決める。

読み取り専用。Issueのクローズ（副作用）は action.yml の後続ステップで行う。

入力:
- mappings.json（取得できなかった場合はファイルなし）: 対象PRの no_impact フラグを読む
- `gh issue list --json number,title` の出力: トラッキングIssueを探す

出力（stdout、GITHUB_OUTPUT 形式）:
- issue_number: クローズ対象のIssue番号（見つからなければ空）
- close_comment: クローズ時に付けるコメント
"""

import argparse
import json
import os


def load_mappings(path):
    """mappings.json を読む。ファイルがない・JSONとして読めない場合は None を返す"""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_no_impact(mappings, pr_number):
    """対象PRが「影響なし」と記録されているか。

    mappings.json を取得できなかった場合や、エントリがない場合は False（紐付け完了扱い）。
    """
    if not isinstance(mappings, dict):
        return False
    entries = mappings.get("mappings")
    if not isinstance(entries, dict):
        return False
    entry = entries.get(str(pr_number))
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("no_impact", False))


# track-architecture の Issue 作成箇所（`printf -v issue_title ...`）と同じ形式にすること
TRACKING_ISSUE_TITLE_PREFIX = "🏗 Architecture Tracking: PR #{pr_number} —"


def find_tracking_issue(issues, pr_number):
    """タイトルが `🏗 Architecture Tracking: PR #<番号> —` で始まる最初のIssueの番号を返す。見つからなければ None。

    先頭一致にするのは、PRタイトル自体に `PR #<番号> —` を含む別PRのIssue
    （例: `PR #50 — Revert PR #42 — xxx`）に一致させないため。
    ` —` まで含めるため、PR #12 の検索で PR #112 や PR #123 に一致することもない。
    """
    prefix = TRACKING_ISSUE_TITLE_PREFIX.format(pr_number=pr_number)
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        if (issue.get("title") or "").startswith(prefix):
            return issue.get("number")
    return None


def build_close_comment(pr_number, no_impact):
    if no_impact:
        return f"✅ PR #{pr_number} はアーキテクチャへの影響なしと記録されました。"
    return f"✅ PR #{pr_number} のコンポーネント紐付けが完了しました。"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Decide which tracking issue to close and its closing comment")
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--mappings-file", default="",
                        help="Path to mappings.json (missing file is treated as no_impact=false)")
    parser.add_argument("--issues-file", required=True,
                        help="Path to JSON output of `gh issue list --json number,title`")
    return parser.parse_args()


def main():
    args = parse_args()

    with open(args.issues_file, encoding="utf-8") as f:
        issues = json.load(f)

    issue_number = find_tracking_issue(issues, args.pr_number)
    no_impact = is_no_impact(load_mappings(args.mappings_file), args.pr_number)

    print(f"issue_number={issue_number if issue_number is not None else ''}")
    print(f"close_comment={build_close_comment(args.pr_number, no_impact)}")


if __name__ == "__main__":
    main()
