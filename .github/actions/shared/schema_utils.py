"""
components.yaml のスキーマバージョン管理ユーティリティ。

`schema_version` はツール側のデータ構造（スキーマ）の互換性を表す。
ユーザーのモデル進化を表す `version`（単純インクリメント）とは別の関心事として扱う。
"""

import sys

# 現在のツールが書き出すスキーマバージョン
CURRENT_SCHEMA_VERSION = "1"

# 現在のツールが読み込みに対応しているスキーマバージョン
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1"})


def get_schema_version(data):
    """components.yaml のデータからスキーマバージョンを取得する。

    `schema_version` が無い既存ファイルは後方互換のため "1" として扱う。
    YAML上で `schema_version: 1` のように数値で書かれた場合も文字列に正規化する。
    """
    if not isinstance(data, dict):
        return CURRENT_SCHEMA_VERSION
    value = data.get("schema_version")
    if value is None:
        return CURRENT_SCHEMA_VERSION
    return str(value).strip()


def check_schema_version(data, source="components.yaml"):
    """スキーマバージョンを検証し、未知のバージョンなら警告を出す。

    警告は stdout を出力として使うスクリプトを汚さないよう stderr に出す
    （GitHub Actions のワークフローコマンドは stderr でも解釈される）。

    Returns:
        bool: 対応済みのバージョンなら True
    """
    version = get_schema_version(data)
    if version in SUPPORTED_SCHEMA_VERSIONS:
        return True
    supported = ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
    print(
        f"::warning::{source} の schema_version \"{version}\" は未知のバージョンです"
        f"（対応バージョン: {supported}）。"
        "gh architecture-tracker update でツールを更新してください",
        file=sys.stderr,
    )
    return False


def ensure_schema_version(data):
    """書き込み前に `schema_version` を補完する。

    未設定の場合のみ現在のバージョンを先頭に付与する。
    既に値がある場合（未知のバージョンを含む）は上書きしない。
    """
    if not isinstance(data, dict) or data.get("schema_version") is not None:
        return data
    rest = {k: v for k, v in data.items() if k != "schema_version"}
    return {"schema_version": CURRENT_SCHEMA_VERSION, **rest}
