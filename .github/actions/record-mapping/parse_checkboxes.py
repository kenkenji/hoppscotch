"""Architecture Trackerのコメントからチェック済みコンポーネントを抽出する。"""

import json
import os
import re
import sys


MARKER = "## \U0001f3d7 Architecture Tracker"
CHECKBOX_PATTERN = re.compile(r"^(\s*)- \[([ xX])\] `([^`]+)`")
SOURCE_PATTERN = re.compile(r"<!--\s*source:\s*(ai|manual)\s*-->")
AI_COMPONENTS_PATTERN = re.compile(r"<!--\s*ai-components:\s*(\[.*?\])\s*-->")
NO_IMPACT_ID = "__no_impact__"


def is_architecture_tracker_comment(body):
    return MARKER in body


def extract_checked_components(body):
    checked = []
    for line in body.splitlines():
        m = CHECKBOX_PATTERN.match(line)
        if m and m.group(2).lower() == "x":
            if m.group(3) != NO_IMPACT_ID:
                checked.append(m.group(3))
    return checked


def extract_no_impact(body):
    for line in body.splitlines():
        m = CHECKBOX_PATTERN.match(line)
        if m and m.group(3) == NO_IMPACT_ID:
            return m.group(2).lower() == "x"
    return False


def extract_source(body):
    m = SOURCE_PATTERN.search(body)
    return m.group(1) if m else "manual"


def extract_ai_components(body):
    m = AI_COMPONENTS_PATTERN.search(body)
    if m:
        return json.loads(m.group(1))
    return []


def main():
    body = os.environ.get("COMMENT_BODY", "")

    if not is_architecture_tracker_comment(body):
        print("not-tracker-comment")
        sys.exit(0)

    result = {
        "components": extract_checked_components(body),
        "source": extract_source(body),
        "ai_components": extract_ai_components(body),
        "no_impact": extract_no_impact(body),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
