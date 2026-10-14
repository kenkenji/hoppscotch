#!/usr/bin/env python3
"""
Generate Model アクションの実体（gh architecture-tracker generate-model から起動するワークフローで実行する）。

チェックアウトしたリポジトリからメタデータ（ディレクトリ構造・依存定義・README 等・主要ファイルの
先頭コメントと定義行）を収集して LLM に渡し、C4 モデル（components.yaml の形式）の初期案を生成する。
terraform の plan / apply のように、1回の実行で生成（plan）と適用（apply）を行う。

データフロー（_architecture-tracker ブランチ）:
- generated-model.json（生成結果。Source of Truth ではない派生データ）
  - Read: 実行の最初。解析したコミットと粒度が同じなら、LLM を呼ばずに再利用する（--regenerate で作り直す）
  - Write: 新しく生成したとき（plan でも保存する。SPA 等から生成結果を参照できるようにするため）と、適用したとき（applied を記録）
- components.yaml / model-changelog.json / タグ model-v{N}
  - Write: --apply のときだけ。既存のモデルに要素も関係も1つもない場合に限る
    （LLM の出力で確定済みのモデル（手動定義・承認済み提案の適用結果）を上書きしないため）
- mappings.json（Read のみ。モデルが空の間に記録された PR を警告する）/ config.yaml（Read のみ。llm.model）

書き込みはすべて1回の書き込みごとに1コミットで行い、ブランチは fast-forward でのみ更新する。
他の書き込み（SPA の保存・他のワークフロー）と競合した場合は、最新のコミットを読み直し、
事前条件を確かめ直してからやり直す。

- ソースコード全体は送らない。送るのは収集したメタデータのみ（送ったファイルは Job Summary に表示する）
- 生成結果は初期提案。SPA の Editor ページで調整する前提
"""

import argparse
import html
import json
import os
import random
import re
import secrets
import subprocess
import sys
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "shared"))
# バージョン番号の解釈は bump-model-version、構成図は generate-mermaid と共有する
sys.path.insert(0, os.path.join(HERE, "..", "bump-model-version"))
sys.path.insert(0, os.path.join(HERE, "..", "generate-mermaid"))

DATA_BRANCH = "_architecture-tracker"
DRAFT_FILE = "generated-model.json"
# generated-model.json の形式のバージョン。フィールドの削除・意味の変更をする場合は上げること
DRAFT_FORMAT_VERSION = "1"
COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")

LEVELS = ("system", "container", "component")
LEVEL_ORDER = {level: i for i, level in enumerate(LEVELS)}
PROVIDERS = ("anthropic", "openai", "claude-code")
KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# SPA の js-yaml（YAML 1.2）が数値として読む id（8e5, 1e-5, 0x1f, 0o17 等）。文字列のまま扱えるよう接頭辞を付ける
NUMERIC_LIKE_ID_RE = re.compile(r"^(?:[0-9]+(?:e-?[0-9]+)?|0x[0-9a-f]+|0o[0-7]+)$")

PROMPT_FILE = os.path.join(HERE, "prompt.txt")

# --- 収集の上限（ソースコード全体を送らないよう、メタデータに絞って量も制限する） ---

TREE_DEPTH = 3
TREE_MAX_CHILDREN = 25
TREE_MAX_ROOT_CHILDREN = 60
TREE_MAX_LINES = 400
# 生成結果の上限（README 等に仕込まれた指示で巨大なモデルや長い説明が作られても、そのまま書き込まない）
MAX_COMPONENTS = 200
MAX_RELATIONS = 400
MAX_ID_CHARS = 64
MAX_NAME_CHARS = 100
MAX_DESCRIPTION_CHARS = 500
MAX_TECHNOLOGY_CHARS = 100
MAX_TAGS = 10
MAX_TAG_CHARS = 50
MAX_MANIFESTS = 30
MAX_MANIFEST_CHARS = 3000
MAX_DOCS = 8
MAX_ROOT_DOC_CHARS = 8000
MAX_DOC_CHARS = 3000
MAX_SOURCES = 40
# Maven / Gradle の src/main/java/com/example/... も対象にできる深さ
MAX_SOURCE_DEPTH = 10
MAX_COMMENT_LINES = 15
MAX_IMPORT_LINES = 15
MAX_DEFINITION_LINES = 25
MAX_LINE_CHARS = 160
MAX_READ_BYTES = 64 * 1024
MAX_FILE_BYTES = 1024 * 1024
# セクションごとの文字数の上限。超えた分のファイルは送らない（優先度の低いものから落とす）
SECTION_BUDGETS = {"tree": 20000, "manifests": 25000, "docs": 20000, "sources": 35000}

# 依存パッケージやビルド成果物のディレクトリ（コミットされていても収集しない）
EXCLUDED_DIRS = {
    "node_modules", "vendor", "dist", "build", "out", "target", ".venv", "venv", "__pycache__",
    ".next", ".nuxt", ".svelte-kit", "coverage", ".idea", ".vscode", "Pods", ".gradle", ".terraform",
}

# Architecture Tracker 自体の配布ファイル（init / update で配置するもの）。
# 利用者のリポジトリのアーキテクチャではないため収集しない。
# gh-architecture-tracker の配置リストと一致することを tests/test_distribution_lists.py で確かめている
TRACKER_WORKFLOWS = {
    "architecture-tracker.yml", "impact-analysis.yml", "catch-up.yml", "insights-analysis.yml",
    "visualize-impact.yml", "generate-model.yml", "architecture-tracker-init.yml",
}
TRACKER_ACTIONS = {
    "shared", "track-architecture", "extract-components", "record-mapping", "generate-mermaid",
    "catch-up-mapping", "deploy-spa", "detect-model-changes", "bump-model-version", "analyze-impact",
    "catch-up-bulk", "insights-analysis", "close-tracking-issue", "generate-model",
}
TRACKER_SKILL_DIR = ".claude/skills/architecture-tracker/"

# 実行環境用・認証情報用のファイル（ファイル名も含めて LLM に送らない）
SENSITIVE_FILE_RE = re.compile(
    r"(^\.env(\..*)?$|\.env$|^\.envrc$|^\.npmrc$|^\.pypirc$|^\.netrc$|^\.git-credentials$|^\.dockercfg$"
    r"|^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$|\.(pem|key|p12|pfx|jks|keystore)$"
    r"|^(secrets?|credentials?)(\..*)?$|\.tfvars(\.json)?$|\.tfstate(\.backup)?$|^kubeconfig$"
    r"|^service[-_]?account.*\.json$)",
    re.IGNORECASE,
)

MANIFEST_NAMES = {
    "package.json", "go.mod", "go.work", "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "pipfile", "cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "gemfile", "composer.json", "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml", "serverless.yml", "serverless.yaml", "app.yaml", "procfile",
    "cmakelists.txt", "pubspec.yaml", "mix.exs", "deno.json", "pnpm-workspace.yaml", "lerna.json",
    "nx.json", "turbo.json", "vercel.json", "netlify.toml", "firebase.json", "template.yaml",
    "chart.yaml", "skaffold.yaml", "wrangler.toml",
}
MANIFEST_SUFFIXES = (".csproj", ".fsproj", ".sln")
# package.json から送るキー（lint 設定等の大きな値は除く）
PACKAGE_JSON_KEYS = (
    "name", "description", "private", "workspaces", "main", "module", "bin", "type", "scripts",
    "dependencies", "devDependencies", "peerDependencies",
)

DOC_NAMES = {"readme.md", "readme.rst", "readme.txt", "readme", "architecture.md", "architecture.rst"}

SOURCE_EXTS = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java", ".kt", ".kts", ".scala",
    ".rb", ".php", ".cs", ".fs", ".rs", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".ex",
    ".exs", ".dart", ".vue", ".svelte",
}
# エントリポイントとみなすファイル名（拡張子を除く）
ENTRY_STEMS = {
    "main", "index", "app", "server", "cli", "__main__", "manage", "wsgi", "asgi", "program",
    "application", "handler", "lambda_function", "routes", "router", "urls", "api", "worker",
}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures", "e2e", "__mocks__"}
TEST_FILE_RE = re.compile(
    r"(^test_|_test\.|\.test\.|\.spec\.|Tests?\.[a-z]+$|^tests?\.py$|^conftest\.py$)"
)
# 生成ファイル・ミニファイ済みファイル（代表として選ばない）
GENERATED_FILE_RE = re.compile(
    r"(\.d\.ts$|\.min\.js$|\.bundle\.js$|\.pb\.go$|\.pb\.(?:cc|h)$|_pb2(?:_grpc)?\.py$|\.g\.dart$"
    r"|\.freezed\.dart$|\.gen\.go$|[._]generated\.)"
)

COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", ";;", "<!--")
# Python の docstring の開始（r""" / u''' 等の接頭辞付きを含む）
DOCSTRING_START_RE = re.compile(r"^[rRuUbBfF]{0,2}(\"\"\"|''')")
# "#" で始まるが、コメントではなくプリプロセッサ指令（C/C++ 等）
PREPROCESSOR_RE = re.compile(r"^#\s*(?:include|import|define|if|ifdef|ifndef|pragma|undef)\b")
# ライセンス本文の段落の書き出し（docstring で段落ごと除くかの判定に使う）
LICENSE_START_RE = re.compile(r"[ \t]*(?:copyright\b|\(c\)\s*\d{4}|spdx-license-identifier|licensed under|all rights reserved)",
                              re.IGNORECASE)
# ライセンスヘッダーとみなす語（"license" だけではライセンス管理のモジュールの説明も消えるため含めない）
LICENSE_RE = re.compile(r"(copyright|\(c\)\s*\d{4}|spdx-license-identifier|licensed under|all rights reserved)",
                        re.IGNORECASE)
IMPORT_RE = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\s|package\s|using\s+[\w.]+\s*;|use\s+[\w:{}, ]+;?"
    r"|(?:const|let|var)\s+\S+\s*=\s*require\(|require\(|#\s*include\s)"
)
DEFINITION_RE = re.compile(
    r"^\s{0,4}(?:"
    r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?(?:class|interface|enum|function|type)\s+\w+"
    r"|(?:export\s+)?const\s+\w+\s*=\s*(?:async\s*)?(?:\(|function\b|\w+\s*=>)"
    r"|(?:async\s+)?def\s+\w+"
    r"|func\s+(?:\([^)]*\)\s*)?\w+"
    r"|(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl|mod|fn)\b"
    r"|(?:public|private|protected|internal)\s+(?:static\s+)?(?:final\s+)?(?:abstract\s+)?(?:sealed\s+)?"
    r"(?:partial\s+)?(?:class|interface|enum|record|struct|object)\s+\w+"
    r"|(?:data\s+|sealed\s+)?(?:class|object)\s+\w+"
    r"|module\s+\w+|namespace\s+[\w.]+"
    r"|@\w+"
    r"|(?:app|router|server|api)\.(?:get|post|put|patch|delete|use|route)\s*\("
    r")"
)

# 認証情報らしい値を伏せる（docker-compose の environment: や .env 形式の例示等）。
# 接続 URL（DATABASE_URL 等）は構成情報（接続先のサービス）を残すため、URL の user:pass@ だけを伏せる
SECRET_NAME = (
    r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|apikey|private[_-]?key|credential"
    r"|access[_-]?key|webhook|authorization"
    r"|(?:account|signing|master|shared[_-]?access|encryption|client)[_-]?key"
    r"|[_-]key(?![a-z0-9])|pgpass|(?<![a-z0-9])pass(?![a-z0-9])|パスワード)"
)
SECRET_NAME_RE = re.compile(SECRET_NAME, re.IGNORECASE)
# 伏せ字の対象とするキー名。キーの前後の文字数に上限を設け、キーの途中から照合を始めないよう後読みを付ける
# （上限がないと、長い行（ミニファイ済みのコードや埋め込みのデータ）で照合の時間が行の長さの2乗以上で増える）
# 前後の文字は ASCII に限る（\w だと仮名・漢字にも一致し、空白で区切らない日本語の文がキー名とみなされて
# 「パスワードリセット機能：…」の説明が伏せられるため）
SECRET_KEY = r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{0,64}?" + SECRET_NAME + r"[A-Za-z0-9_.-]{0,64}"
# キー名に _key を含んでも、認証情報ではないもの（データベースの主キー等）
NON_SECRET_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:hash|sort|primary|foreign|partition|range|cache|lookup|unique|composite|public|row|group"
    r"|idempotency|dedup|order)[_-]key$",
    re.IGNORECASE,
)
# 区切りの空白は [ \t]*（改行をまたいで次の行の単語を伏せないようにする）。"==" は区切りとみなさない
# （requirements.txt の tokenizers==0.15.0 等）。型ヒント付きの既定値（password: str = "..."）と、
# PHP / Ruby の "=>"（'password' => 'xxx'）も対象にする。
# 値は同じ行の引用符付きの文字列（"..." / '...'。別の種類の引用符を含んでもよい）か、引用符なしの1語（bare）
SECRET_KEY_RE = re.compile(
    r"(?P<key>" + SECRET_KEY + r")"
    r"(?P<sep>[\"']?[ \t]*(?::[ \t]*[\w.\[\]]{1,64}(?:[ \t]*\|[ \t]*[\w.\[\]]{1,64}){0,8}[ \t]*=(?![=>])"
    r"|=>(?=[ \t]*[\"'])|:=|(?<=[ \t])(?=[\"'])|[:=：](?![=>]))[ \t]*)"
    r"(?:\"(?P<dq>[^\"\n]*)\"|'(?P<sq>[^'\n]*)'|(?P<bare>[^\s\"'#,;&}\]]+))",
    re.IGNORECASE,
)
# XML の要素（<db.password>xxx</db.password>）
XML_SECRET_RE = re.compile(r"<(?P<tag>[\w.-]{0,64}?" + SECRET_NAME + r"[\w.-]{0,64})>[^<\n]{1,1024}</(?P=tag)>",
                           re.IGNORECASE)
# キー名に関係なく、値の形で判定できるトークン。長さに上限を設ける。JWT は "." が続かないと一致しないため、
# 途中の位置から照合を始めないよう後読みを付ける（"eyJ" が繰り返す入力で、照合時間が2乗で増えないようにする）
TOKEN_VALUE_RE = re.compile(
    r"(?:sk_(?:live|test)_[0-9A-Za-z]{8,4096}|rk_(?:live|test)_[0-9A-Za-z]{8,4096}|sk-(?:ant-)?[A-Za-z0-9_-]{20,4096}"
    r"|gh[pousr]_[A-Za-z0-9]{20,4096}|github_pat_[A-Za-z0-9_]{20,4096}|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|xox[abposr]-[A-Za-z0-9-]{8,4096}|AIza[0-9A-Za-z_-]{35}|hooks\.slack\.com/services/[A-Za-z0-9/_-]{1,4096}"
    r"|glpat-[A-Za-z0-9_-]{20,4096}|npm_[A-Za-z0-9]{36}|whsec_[A-Za-z0-9+/=]{20,4096}"
    r"|(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,4096}\.eyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096})"
)
# Authorization ヘッダー。スキーム名（Bearer / Basic / Token 等）と、その後の値をまとめて伏せる
AUTHORIZATION_RE = re.compile(
    r"(?P<key>\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?)(?:(?:bearer|basic|token|digest)[ \t]+)?[^\s\"',}]+",
    re.IGNORECASE,
)
# Authorization ヘッダー以外に書かれた Bearer / Basic の値（数字を含む8文字以上の語だけを対象にし、
# 文章中の "Bearer authentication" 等は残す）
AUTH_SCHEME_RE = re.compile(r"(?P<scheme>\b(?:Bearer|Basic)[ \t]+)(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,}",
                            re.IGNORECASE)
# 秘密鍵ブロック（PEM と PGP）。
# - 中身が base64 の行（と Version: 等のヘッダー行・空行）だけなら、END までをブロックとして伏せる
# - それ以外（END がない、1行に空白区切りで書いた、JSON の中で改行を "\n" と書いた等）は、BEGIN の後に続く
#   16文字以上の base64 の塊を伏せる（文章中の言及の後の短い単語は伏せない）
# 各行・各塊が1通りにしか一致しない形にし、行数・長さに上限を設ける（照合の時間が爆発的に増えないようにする）
PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    r"(?:[ \t]*\r?\n(?:[ \t]*(?:[A-Za-z0-9+/=]{1,4096}[ \t]*|[\w-]{1,32}:[^\r\n]{0,256})?\r?\n){0,1000}?"
    r"[ \t]*-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    r"|(?:(?:[ \t]|\\n|\r?\n){1,8}[A-Za-z0-9+/=]{16,4096}(?![A-Za-z0-9+/=])){0,512})",
)
# Dockerfile の ENV / ARG の空白区切り（ENV DB_PASSWORD supersecret）。"=" 区切りは SECRET_KEY_RE で伏せる。
# ENV / ARG は大文字だけを対象にする（文章中の "Env token rotation" 等を伏せないため）
DOCKER_ENV_RE = re.compile(
    r"^(?P<head>[ \t]*(?-i:ENV|ARG)[ \t]+[\w.-]{0,64}?" + SECRET_NAME + r"[\w.-]{0,64}[ \t]+)(?P<value>[^=\s\\].*)$",
    re.IGNORECASE | re.MULTILINE,
)
# Dockerfile の行継続（ENV DB_PASSWORD \ の次の行が値）
DOCKER_CONTINUATION_RE = re.compile(
    r"^[ \t]*(?-i:ENV|ARG)[ \t]+[\w.-]{0,64}?" + SECRET_NAME + r"[\w.-]{0,64}(?:[ \t]+|[ \t]*=[ \t]*)\\[ \t]*$",
    re.IGNORECASE,
)
# Dockerfile の命令の行（行継続の次の行がこれなら、値ではないので伏せない）
DOCKER_INSTRUCTION_RE = re.compile(r"^[ \t]*(?:FROM|RUN|CMD|LABEL|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|VOLUME|USER|WORKDIR"
                                   r"|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL)\b")
# YAML の複数行の値（KEY: |- / KEY: >）と、Kubernetes 等の name / value 形式（name: DB_PASSWORD の次の value:）
YAML_BLOCK_KEY_RE = re.compile(r"^(?P<indent>[ \t]*)(?:-[ \t]+)?[\"']?[\w.-]{0,64}?" + SECRET_NAME
                               + r"[\w.-]{0,64}[\"']?[ \t]*:[ \t]*[|>][-+0-9]*[ \t]*$", re.IGNORECASE)
YAML_NAME_RE = re.compile(r"^[ \t]*(?:-[ \t]+)?name[ \t]*:[ \t]*[\"']?[\w.-]{0,64}?" + SECRET_NAME
                          + r"[\w.-]{0,64}[\"']?[ \t]*$", re.IGNORECASE)
YAML_VALUE_RE = re.compile(r"^(?P<head>[ \t]*(?:-[ \t]+)?value[ \t]*:[ \t]*)(?P<value>\S.*)$", re.IGNORECASE)
# package.json で値をそのまま送るキー（パッケージ名に token 等を含んでもバージョンは伏せない）
PACKAGE_DEPENDENCY_KEYS = {"dependencies", "devDependencies", "peerDependencies"}
# URL の認証情報。ユーザー名がない場合（redis://:pw@host）や、パスワードに @ を含む場合（amqp://guest:gu@st@rabbit）も、
# 最後の @ までを伏せる。コロンのないトークンだけの userinfo（https://<token>@github.com/...）は20文字以上を対象にする。
# スキームの途中から照合を始めないよう後読みを付け、長さに上限を設ける（長い行で照合が遅くならないようにする）
URL_USERINFO_RE = re.compile(
    r"(?<![a-z0-9+.-])(?P<scheme>[a-z][a-z0-9+.-]{0,30}://)(?:[^/\s:@]{0,256}:[^/\s]{0,1024}|[^/\s:@]{20,256})@",
    re.IGNORECASE,
)


class GenerateError(Exception):
    """Job Summary とログに表示して終了すべきエラー"""


class PreconditionError(GenerateError):
    """apply の事前条件を満たさない（既存のモデルがある等）。API の失敗とは区別する"""


class GhApiError(GenerateError):
    """gh api の呼び出しに失敗した（HTTP ステータスを判定できるよう stderr を保持する）"""

    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


def sanitize(value):
    """1行の表示用テキストにする（制御文字を除き、改行・タブは空白にする）。

    リポジトリや LLM 由来の文字列で、エスケープシーケンスや双方向制御文字をログに送り込まれないようにする。
    """
    text = "" if value is None else str(value)
    out = []
    for ch in text:
        if ch in "\r\n\t":
            out.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def sanitize_multiline(value):
    """複数行の表示用テキストから制御文字を除く（改行・タブは残す）"""
    text = "" if value is None else str(value)
    return "".join(
        ch for ch in text
        if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs")
    )


# --- コードベースの収集 ---


def run_git(root, *args):
    try:
        result = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=60)
    except FileNotFoundError:
        raise GenerateError("git コマンドが見つかりません。")
    return result.returncode, result.stdout.decode("utf-8", errors="replace"), \
        result.stderr.decode("utf-8", errors="replace").strip()


def list_repo_files(root):
    """LLM に送る候補のファイルの一覧（ファイル名はディレクトリ構造として送る）。

    Git で管理しているファイルだけを対象にする（未追跡のファイルは含めない）。
    Git の管理下にあっても .gitignore に一致するもの（コミットした後に .gitignore に加えたもの）と、
    実行環境用・認証情報用のファイル（.env 等）は、ファイル名も含めて送らない。
    """
    code, out, err_text = run_git(root, "ls-files", "-z")
    if code != 0:
        raise GenerateError(f"git ls-files に失敗しました: {err_text}")
    code, ignored_out, err_text = run_git(root, "ls-files", "-z", "--cached", "--ignored", "--exclude-standard")
    if code != 0:
        raise GenerateError(f"git ls-files に失敗しました: {err_text}")
    ignored = set(p for p in ignored_out.split("\0") if p)
    return [p for p in out.split("\0") if p and p not in ignored and not is_excluded(p)]


def is_excluded(path):
    parts = path.split("/")
    if any(part in EXCLUDED_DIRS for part in parts[:-1]):
        return True
    if SENSITIVE_FILE_RE.search(parts[-1]):
        return True
    if path.startswith(TRACKER_SKILL_DIR):
        return True
    if len(parts) == 3 and parts[:2] == [".github", "workflows"] and parts[2] in TRACKER_WORKFLOWS:
        return True
    if len(parts) >= 4 and parts[:2] == [".github", "actions"] and parts[2] in TRACKER_ACTIONS:
        return True
    return False


def depth_of(path):
    return path.count("/") + 1


def _new_node():
    return {"dirs": {}, "files": [], "count": 0}


def build_tree(paths, max_depth=TREE_DEPTH, max_children=TREE_MAX_CHILDREN, max_lines=TREE_MAX_LINES):
    """ディレクトリツリーを文字列にする。

    浅い階層から順に（幅優先で）行数の上限まで展開する。そのため、ファイルの多いディレクトリがあっても
    同じ階層の他のディレクトリ（src/ 等）が消えない。展開しなかったディレクトリはファイル数だけ表示する。
    """
    root = _new_node()
    for path in paths:
        node = root
        node["count"] += 1
        parts = path.split("/")
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, _new_node())
            node["count"] += 1
        node["files"].append(parts[-1])

    def entries_of(node):
        entries = [(name, child) for name, child in sorted(node["dirs"].items())]
        entries += [(name, None) for name in sorted(node["files"])]
        # ルートはディレクトリが多くても README 等のファイルまで表示できるよう、上限を広げる
        limit = max(max_children, TREE_MAX_ROOT_CHILDREN) if node is root else max_children
        return entries[:limit], max(0, len(entries) - limit)

    expanded = set()
    used = 0
    queue = deque([(root, 0)])
    while queue:
        node, depth = queue.popleft()
        shown, rest = entries_of(node)
        cost = len(shown) + (1 if rest else 0)
        if node is not root and (depth >= max_depth or used + cost > max_lines):
            continue
        expanded.add(id(node))
        used += cost
        queue.extend((child, depth + 1) for _, child in shown if child is not None)

    lines = []

    def render(node, depth):
        shown, rest = entries_of(node)
        indent = "  " * depth
        for name, child in shown:
            if child is None:
                lines.append(f"{indent}{name}")
            elif id(child) in expanded:
                lines.append(f"{indent}{name}/")
                render(child, depth + 1)
            else:
                lines.append(f"{indent}{name}/ （{child['count']} ファイル）")
        if rest:
            lines.append(f"{indent}…（他 {rest} 件）")

    render(root, 0)
    return "\n".join(lines)


def read_text(root, path, limit=MAX_READ_BYTES):
    """テキストファイルの先頭を読む。バイナリ・大きすぎるファイル・読めないファイルは None。

    シンボリックリンクは読まない（リンク先がリポジトリ外、例えば ~/.aws/credentials の場合があるため）。
    """
    full = os.path.join(root, *path.split("/"))
    try:
        if os.path.islink(full) or not os.path.isfile(full):
            return None
        real_root = os.path.realpath(root)
        if os.path.commonpath([os.path.realpath(full), real_root]) != real_root:
            return None
        if os.path.getsize(full) > MAX_FILE_BYTES:
            return None
        with open(full, "rb") as f:
            data = f.read(limit)
    except (OSError, ValueError):
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def mask_secrets(text, quoted_only=False):
    """認証情報らしい値（password: xxx、API_KEY=xxx、URL の user:pass@ 等）を伏せる。

    quoted_only=True のときは引用符付きの値だけを伏せる（ソースコードの型ヒント
    `def login(token: str)` のような、値ではない記述を伏せないため）。
    値の形で判定できるもの（トークン・秘密鍵・URL の認証情報・Authorization ヘッダー）は常に伏せる。
    """
    text = mask_token_values(mask_url_userinfo(text))
    text = AUTHORIZATION_RE.sub(lambda m: f"{m.group('key')}***", text)
    text = XML_SECRET_RE.sub(lambda m: f"<{m.group('tag')}>***</{m.group('tag')}>", text)
    if not quoted_only:
        text = DOCKER_ENV_RE.sub(lambda m: f"{m.group('head')}***", text)
        text = _mask_yaml_multiline(text)

    def replace(m):
        non_secret = NON_SECRET_KEY_RE.search(m.group("key"))
        # SECRET_HASH_KEY / password_hash_key 等は、前半に認証情報の語があるので伏せる
        if non_secret and not SECRET_NAME_RE.search(m.group("key")[:non_secret.start()]):
            return m.group(0)
        if m.group("dq") is not None:
            return f'{m.group("key")}{m.group("sep")}"***"'
        if m.group("sq") is not None:
            return f"{m.group('key')}{m.group('sep')}'***'"
        if quoted_only:
            return m.group(0)
        return f"{m.group('key')}{m.group('sep')}***"

    return SECRET_KEY_RE.sub(replace, text)


def _mask_yaml_multiline(text):
    """1行の正規表現では伏せられない YAML の値を伏せる。

    - 複数行の値: `DB_PASSWORD: |-` の後の、より深く字下げした行
    - name / value 形式: `- name: DB_PASSWORD` の次（2行以内）の `value: xxx`
    - Dockerfile の行継続: 行末がバックスラッシュの `ENV DB_PASSWORD` の次の行
    """
    lines = text.split("\n")
    block_indent = None
    name_pending = 0
    continuation = False
    for i, line in enumerate(lines):
        if continuation:
            if not line.strip():
                continue
            continuation = False
            if not DOCKER_INSTRUCTION_RE.match(line):
                indent = len(line) - len(line.lstrip(" \t"))
                lines[i] = line[:indent] + "***" + (" \\" if line.rstrip().endswith("\\") else "")
                continue
        if DOCKER_CONTINUATION_RE.match(line):
            continuation = True
            continue
        if block_indent is not None:
            indent = len(line) - len(line.lstrip(" \t"))
            if line.strip() and indent > block_indent:
                lines[i] = line[:indent] + "***"
                continue
            if line.strip():
                block_indent = None
        if name_pending:
            m = YAML_VALUE_RE.match(line)
            if m:
                lines[i] = m.group("head") + "***"
                name_pending = 0
                if m.group("value").strip()[:1] in ("|", ">"):
                    block_indent = len(line) - len(line.lstrip(" \t"))
                continue
            name_pending -= 1
        m = YAML_BLOCK_KEY_RE.match(line)
        if m:
            block_indent = len(m.group("indent"))
            continue
        if YAML_NAME_RE.match(line):
            name_pending = 2
    return "\n".join(lines)


def mask_url_userinfo(text):
    """URL の user:pass@（git+https://x-access-token:TOKEN@github.com/... 等）を伏せる"""
    return URL_USERINFO_RE.sub(lambda m: f"{m.group('scheme')}***@", text)


def mask_token_values(text):
    """キー名に関係なく、値の形で判定できる認証情報（ghp_… / AKIA… / 秘密鍵ブロック等）を伏せる"""
    text = PEM_PRIVATE_KEY_RE.sub("***（秘密鍵）", text)
    text = TOKEN_VALUE_RE.sub("***", text)
    return AUTH_SCHEME_RE.sub(lambda m: f"{m.group('scheme')}***", text)


def _mask_json(value, key=None):
    if isinstance(value, dict):
        return {k: _mask_json(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_json(v) for v in value]
    if isinstance(value, str):
        if key and SECRET_NAME_RE.search(key):
            return "***"
        return mask_secrets(value)
    return value


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（省略）"


def is_manifest(path):
    name = path.rsplit("/", 1)[-1].lower()
    if name in MANIFEST_NAMES or name.endswith(MANIFEST_SUFFIXES):
        return True
    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return True
    return name.startswith("requirements") and name.endswith(".txt")


def _mask_dependencies(value):
    """依存定義はキー名（パッケージ名）で伏せず、参照先 URL の認証情報とトークンの形の値だけを伏せる"""
    if isinstance(value, dict):
        return {k: mask_token_values(mask_url_userinfo(v)) if isinstance(v, str) else v
                for k, v in value.items()}
    return value


def condense_package_json(text):
    """送るキーに絞り、依存関係以外の値の認証情報を伏せる（解析できなければ None）"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    picked = {
        k: _mask_dependencies(data[k]) if k in PACKAGE_DEPENDENCY_KEYS else _mask_json(data[k], k)
        for k in PACKAGE_JSON_KEYS if k in data
    }
    return json.dumps(picked, ensure_ascii=False, indent=1)


def collect_manifests(root, paths):
    candidates = sorted((p for p in paths if is_manifest(p)), key=lambda p: (depth_of(p), p))
    items = []
    for path in candidates[:MAX_MANIFESTS]:
        text = read_text(root, path)
        if text is None:
            continue
        condensed = condense_package_json(text) if path.rsplit("/", 1)[-1] == "package.json" else None
        content = condensed if condensed is not None else mask_secrets(text)
        items.append({"path": path, "content": truncate(content.strip(), MAX_MANIFEST_CHARS)})
    return items


def is_doc(path):
    name = path.rsplit("/", 1)[-1].lower()
    if depth_of(path) <= 3 and name in DOC_NAMES:
        return True
    return path.lower().startswith("docs/") and "architecture" in name and name.endswith(".md")


def collect_docs(root, paths):
    def priority(p):
        name = p.rsplit("/", 1)[-1].lower()
        # ルートの README → ARCHITECTURE → 浅い順
        return (depth_of(p), 0 if name.startswith("readme") else 1, p)

    items = []
    for path in sorted((p for p in paths if is_doc(p)), key=priority):
        if len(items) >= MAX_DOCS:
            break
        text = read_text(root, path)
        if text is None or not text.strip():
            continue
        limit = MAX_ROOT_DOC_CHARS if depth_of(path) == 1 else MAX_DOC_CHARS
        items.append({"path": path, "content": truncate(mask_secrets(text.strip()), limit)})
    return items


def is_source(path):
    name = path.rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1].lower()
    if ext not in SOURCE_EXTS or depth_of(path) > MAX_SOURCE_DEPTH:
        return False
    if any(part.lower() in TEST_DIRS for part in path.split("/")[:-1]):
        return False
    return not TEST_FILE_RE.search(name) and not GENERATED_FILE_RE.search(name)


def file_size(root, path):
    try:
        return os.path.getsize(os.path.join(root, *path.split("/")))
    except OSError:
        return 0


def select_sources(root, paths, max_files=MAX_SOURCES):
    """エントリポイントと、ディレクトリごとの代表ファイル（最も大きいファイル）を優先度順に選ぶ。

    予算を超えた分は末尾（優先度の低いもの）から落とすため、並べ替えずに優先度順のまま返す。
    """
    candidates = [p for p in paths if is_source(p)]
    entries = sorted(
        (p for p in candidates if os.path.splitext(p.rsplit("/", 1)[-1])[0].lower() in ENTRY_STEMS),
        key=lambda p: (depth_of(p), p),
    )
    chosen = entries[: max_files // 2]
    chosen_set = set(chosen)

    by_dir = {}
    for p in candidates:
        if p not in chosen_set:
            by_dir.setdefault(p.rsplit("/", 1)[0] if "/" in p else "", []).append(p)
    # 浅いディレクトリから順に代表を選ぶ
    for d in sorted(by_dir, key=lambda d: (d.count("/") + (1 if d else 0), d)):
        if len(chosen) >= max_files:
            break
        chosen.append(max(by_dir[d], key=lambda p: (file_size(root, p), p)))
    return chosen


def _clip(line):
    line = line.rstrip()
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


def _comment_blocks(lines):
    """ファイル先頭のコメント・docstring を、空行で区切ったブロックのリストにする"""
    blocks, current = [], []
    in_doc = None
    for i, line in enumerate(lines):
        s = line.strip()
        # shebang と、PHP の開始タグ（<?php）は飛ばす
        if i == 0 and (s.startswith("#!") or s.lower() == "<?php"):
            continue
        if in_doc:
            current.append(line)
            if in_doc in s:
                in_doc = None
            continue
        if not s:
            if current:
                blocks.append(current)
                current = []
            continue
        doc = DOCSTRING_START_RE.match(s)
        if doc:
            quote_mark = doc.group(1)
            body = s[doc.end():]
            # コメント（ライセンスヘッダー等）の直後に空行なしで続く docstring は、別のブロックにする
            if current:
                blocks.append(current)
                current = []
            current.append(line)
            if quote_mark not in body:
                in_doc = quote_mark
        elif s.startswith(COMMENT_PREFIXES) and not PREPROCESSOR_RE.match(s):
            current.append(line)
        else:
            break
    if current:
        blocks.append(current)
    return blocks


def _drop_license_paragraphs(block):
    """docstring から、ライセンスの語を含む段落（空行で区切られた単位）を除く"""
    paragraphs, current = [], []
    for line in block:
        if not line.strip():
            paragraphs.append(current)
            current = []
        else:
            current.append(line)
    paragraphs.append(current)
    kept = []
    for i, para in enumerate(paragraphs):
        if not para:
            continue
        doc = DOCSTRING_START_RE.match(para[0].strip()) if i == 0 else None
        first = para[0].strip()[doc.end():] if doc else para[0]
        if LICENSE_START_RE.match(first):
            # 段落がライセンスの語で始まる（ライセンス本文）: 段落ごと除く。開始の引用符は残す
            if doc:
                kept.append([para[0][:para[0].index(doc.group(1)) + 3]])
            continue
        # 説明の段落の途中にある "(c) 2024" 等で始まる行だけを除く（説明は残す。"copyright claims" 等の
        # 説明の中の語では除かない）
        kept.append([line for j, line in enumerate(para) if j == 0 or not LICENSE_START_RE.match(line)])
    return [line for para in kept for line in para]


def leading_comment(lines):
    """ファイル先頭のコメント・docstring（ライセンスヘッダーは除く）。

    コメントのブロックはライセンスの語を含めばブロックごと除き、docstring はその行だけを除く
    （docstring の説明の中に "(c) 2024" 等があっても、説明は残すため）。
    """
    out = []
    for block in _comment_blocks(lines):
        if DOCSTRING_START_RE.match(block[0].strip()):
            block = _drop_license_paragraphs(block)
        elif any(LICENSE_RE.search(line) for line in block):
            continue
        out.extend(block)
        if len(out) >= MAX_COMMENT_LINES:
            break
    return [_clip(line) for line in out[:MAX_COMMENT_LINES]]


def extract_imports(lines):
    """import 行。複数行の import（TS の `import {`、Go の `import (`）は1行にまとめる"""
    imports = []
    i = 0
    while i < len(lines) and len(imports) < MAX_IMPORT_LINES:
        line = lines[i]
        i += 1
        if not IMPORT_RE.match(line):
            continue
        parts = [line.strip()]
        # 括弧の対応が取れるまで次の行をつなげる（行末コメント中の括弧は数えない。Rust の入れ子の { も対応する）
        depth = _bracket_depth(parts[0])
        while depth > 0 and i < len(lines) and len(parts) < 30:
            parts.append(lines[i].strip())
            depth += _bracket_depth(parts[-1])
            i += 1
        imports.append(_clip(" ".join(p for p in parts if p)))
    return imports


def _bracket_depth(line):
    """行の括弧（{ / (）の開き − 閉じ。// と # 以降の行末コメントは除く"""
    code = re.split(r"//|\s#", line, maxsplit=1)[0]
    return code.count("{") + code.count("(") - code.count("}") - code.count(")")


def extract_outline(text):
    # 伏せてから行を取り出して切り詰める（切り詰めた後に伏せると、行末の値の先頭が残るため）。
    # コメントは文章なので、引用符なしの値（STRIPE_API_KEY=sk_... 等）も伏せる。
    # import・定義行には型ヒント（token: str 等）が多いため、引用符付きの値だけを伏せる
    text = "\n".join(text.split("\n")[:2000])
    comment = leading_comment(mask_secrets(text).split("\n"))
    code_lines = mask_secrets(text, quoted_only=True).split("\n")
    imports = extract_imports(code_lines)
    definitions = [_clip(line) for line in code_lines if DEFINITION_RE.match(line)][:MAX_DEFINITION_LINES]
    parts = []
    if comment:
        parts.append("先頭コメント:\n" + "\n".join(comment))
    if imports:
        parts.append("import:\n" + "\n".join(imports))
    if definitions:
        parts.append("定義:\n" + "\n".join(definitions))
    return "\n".join(parts)


def collect_sources(root, paths):
    items = []
    for path in select_sources(root, paths):
        text = read_text(root, path)
        if text is None:
            continue
        outline = extract_outline(text)
        if outline:
            items.append({"path": path, "content": outline})
    return items


def within_budget(items, budget):
    """先頭（優先度の高いもの）から、合計が budget に収まる分だけ残す（残りは送らない）"""
    kept, total = [], 0
    for item in items:
        size = len(item["path"]) + len(item["content"])
        if kept and total + size > budget:
            break
        kept.append(item)
        total += size
    return kept, len(items) - len(kept)


def collect_codebase(root):
    paths = list_repo_files(root)
    tree = truncate(build_tree(paths), SECTION_BUDGETS["tree"])
    manifests, omitted_manifests = within_budget(collect_manifests(root, paths), SECTION_BUDGETS["manifests"])
    docs, omitted_docs = within_budget(collect_docs(root, paths), SECTION_BUDGETS["docs"])
    sources, omitted_sources = within_budget(collect_sources(root, paths), SECTION_BUDGETS["sources"])
    return {
        "file_count": len(paths),
        "tree": tree,
        "manifests": manifests,
        "docs": docs,
        "sources": sources,
        "omitted": omitted_manifests + omitted_docs + omitted_sources,
    }


def input_files(collected):
    return [item["path"] for key in ("manifests", "docs", "sources") for item in collected[key]]


# --- プロンプト ---


def render_files(items):
    if not items:
        return "（該当なし）"
    blocks = []
    for item in items:
        # パスを属性値として安全に埋め込み、内容に閉じタグがあっても境界が崩れないようにする
        content = item["content"].replace("</file>", "<\\/file>")
        blocks.append(f'<file path="{html.escape(item["path"], quote=True)}">\n{content}\n</file>')
    return "\n\n".join(blocks)


def build_prompt(collected, level, repo_label):
    with open(PROMPT_FILE, encoding="utf-8") as f:
        template = f.read()
    if level == "container":
        level_instruction = "system と container のみを作成してください。component は作成しないでください。"
    else:
        level_instruction = "system、container、component のすべてを作成してください。"
    # 収集データに "{...}" が含まれても壊れないよう format は使わず、埋め込んだデータを再度置換しないよう1回の置換で組み立てる
    values = {
        "{repo}": repo_label,
        "{level_instruction}": level_instruction,
        "{tree_depth}": str(TREE_DEPTH),
        "{tree}": (collected["tree"] or "（ファイルなし）").replace("</tree>", "<\\/tree>"),
        "{manifests}": render_files(collected["manifests"]),
        "{docs}": render_files(collected["docs"]),
        "{sources}": render_files(collected["sources"]),
    }
    pattern = re.compile("|".join(re.escape(k) for k in values))
    return pattern.sub(lambda m: values[m.group(0)], template)


# --- LLM ---


def load_llm_utils():
    import llm_utils  # ../shared（モジュールの先頭で sys.path に追加済み）
    return llm_utils


def resolve_model(requested, provider, config_model):
    if requested:
        return requested
    # config.yaml の既定値（claude-*）は OpenAI では使えないため、プロバイダーの標準モデルにする
    if not config_model or (provider == "openai" and config_model.startswith("claude")):
        return None
    return config_model


def call_llm(provider, prompt, model):
    llm_utils = load_llm_utils()
    # llm_utils はリトライのときに LLM の出力をそのまま stderr に出すことがある。その間はワークフローコマンドを
    # 止めておく（LLM の出力に "::set-output" 等が含まれても、コマンドとして解釈されないようにする）
    stop_token = secrets.token_hex(16)
    print(f"::stop-commands::{stop_token}", file=sys.stderr, flush=True)
    try:
        return llm_utils.call_llm(provider, prompt, model=model, max_tokens=8192, timeout=300)
    except Exception as e:  # noqa: BLE001 — SDK ごとに例外の型が異なるため、まとめて利用者向けのエラーにする
        raise GenerateError(f"LLM の呼び出しに失敗しました（{provider}）: {e}")
    finally:
        print(f"::{stop_token}::", file=sys.stderr, flush=True)


def parse_response(text):
    """LLM の応答から components を含む JSON オブジェクトを取り出す。

    JSON より前の文章に "{" が含まれることがあるため、"{" の位置を順に試す。
    components を含むオブジェクトだけを受け付ける（途中で切れた応答の中にある要素1つ分の
    オブジェクトを、生成結果として扱わないため）。
    """
    text = text or ""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(data, dict) and "components" in data:
            return data
    preview = sanitize(text[:300])
    hint = ("応答が途中で切れた可能性があります。level を container にして粒度を下げ、再実行してください。"
            if '"components"' in text else "regenerate を指定して再実行してください。")
    raise GenerateError(f"LLM の応答を JSON として解析できませんでした。{hint}\n応答の先頭: {preview}")


# --- 生成結果の検証・正規化 ---


def to_kebab(value):
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = str(value).strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def to_id(value):
    """要素の id にする（kebab-case。YAML 1.2 で数値として読まれる値には接頭辞を付ける）"""
    cid = to_kebab(value)[:MAX_ID_CHARS].strip("-")
    if cid and NUMERIC_LIKE_ID_RE.match(cid):
        # 接頭辞を付けても上限を超えないようにする（超えると正規化が冪等でなくなり、保存した生成結果を使えなくなる）
        cid = f"id-{cid}"[:MAX_ID_CHARS].strip("-")
    return cid


def clean(value, limit=None):
    """1行のテキストにする（制御文字・単独のサロゲートを除き、改行は空白にする。limit 文字を超えたら切り詰める）"""
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = "".join(ch for ch in sanitize(value) if unicodedata.category(ch) != "Cs")
    text = " ".join(text.split())
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def clean_tags(value):
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    tags = []
    for tag in value:
        t = clean(tag, MAX_TAG_CHARS)
        if t and t not in tags:
            tags.append(t)
    return tags[:MAX_TAGS]


def is_external(component):
    return "external" in component["tags"]


def normalize_model(raw, level, system_name):
    """LLM の出力を components.yaml の制約（SPA エディタの検証と同じ）を満たす形に整える。

    満たせない要素は除外し、理由を warnings に入れる。
    Returns: (components, relations, warnings)
    """
    warnings = []
    components = []
    ids = set()
    removed = []  # --level container で除外した component（id, name, parent）

    raw_components = raw.get("components")
    if not isinstance(raw_components, list):
        raw_components = []
    truncated = 0
    for item in raw_components:
        if not isinstance(item, dict):
            continue
        # id が日本語等で kebab-case にできない場合は name から作る
        cid = to_id(item.get("id")) or to_id(item.get("name"))
        comp_level = clean(item.get("level")).lower()
        if not cid:
            warnings.append("id のない要素を除外しました")
            continue
        if comp_level not in LEVELS:
            warnings.append(f"'{cid}' を除外しました（level が不正: {clean(item.get('level')) or '未指定'}）")
            continue
        if level == "container" and comp_level == "component":
            removed.append((cid, to_kebab(item.get("name")), item.get("parent")))
            continue
        if cid in ids:
            warnings.append(f"'{cid}' の重複を除外しました")
            continue
        # 上限の判定は、有効な要素だけで数える（ちょうど上限の件数なら警告しない）
        if len(components) >= MAX_COMPONENTS:
            warnings.append(f"要素が {MAX_COMPONENTS} 件を超えたため、残りを除外しました")
            break
        name = clean(item.get("name"), MAX_NAME_CHARS) or cid
        description = clean(item.get("description"), MAX_DESCRIPTION_CHARS) or name
        if len(clean(item.get("name"))) > MAX_NAME_CHARS or len(clean(item.get("description"))) > MAX_DESCRIPTION_CHARS:
            truncated += 1
        ids.add(cid)
        components.append({
            "id": cid,
            "name": name,
            "level": comp_level,
            "parent": item.get("parent"),
            "technology": clean(item.get("technology"), MAX_TECHNOLOGY_CHARS) or None,
            "description": description,
            "tags": clean_tags(item.get("tags")),
        })
    if truncated:
        warnings.append(f"{truncated} 件の要素の name / description が長すぎるため切り詰めました")

    # LLM が id ではなく name で参照することがあるため、name → id の対応でも解決する
    aliases = {}
    for c in components:
        key = to_kebab(c["name"])
        if key and key not in ids:
            aliases.setdefault(key, c["id"])

    def resolve(value):
        ref = to_id(value)
        if ref in ids:
            return ref
        return aliases.get(to_kebab(value), ref)

    for c in components:
        c["parent"] = resolve(c["parent"]) or None

    # 除外した component の関係は、親の container の関係にする（同じ id の要素が残っている場合は付け替えない）
    removed_parent = {}
    for cid, name_key, parent in removed:
        target = resolve(parent)
        for key in (cid, name_key):
            if key and key not in ids and key not in aliases:
                removed_parent.setdefault(key, target)
    if removed:
        warnings.append(f"component レベルの要素を {len(removed)} 件除外しました（--level container）")

    components = _fix_parents(components, system_name, warnings)
    # システムを自動で追加して上限を超えた場合は、子のない要素を末尾から除いて上限に収める
    while len(components) > MAX_COMPONENTS:
        parents = {c["parent"] for c in components}
        victim = next(c for c in reversed(components) if c["id"] not in parents)
        components.remove(victim)
        warnings.append(f"要素が {MAX_COMPONENTS} 件を超えたため、'{victim['id']}' を除外しました")
    relations = _normalize_relations(raw.get("relations"), components, resolve, removed_parent, warnings)
    components.sort(key=lambda c: (LEVEL_ORDER[c["level"]], 1 if is_external(c) else 0))
    return components, relations, warnings


def _fix_parents(components, system_name, warnings):
    systems = [c for c in components if c["level"] == "system"]
    internal_systems = [c for c in systems if not is_external(c)]
    for c in systems:
        c["parent"] = None

    # 親を解決できない container がある場合の受け皿。"internal" タグ付きの system が1つならそれを、
    # なければ external でない system が1つならそれを使う。1つもなければリポジトリ名のシステムを作る
    # （複数あるとどれか判断できないため除外する）
    tagged_internal = [c for c in systems if "internal" in c["tags"]]
    if len(tagged_internal) == 1:
        fallback = tagged_internal[0]["id"]
    else:
        fallback = internal_systems[0]["id"] if len(internal_systems) == 1 else None
    system_ids = {c["id"] for c in systems}
    needs_system = any(
        c["level"] == "container" and c["parent"] not in system_ids for c in components
    )
    if needs_system and not internal_systems:
        sid = to_id(system_name) or "system"
        taken = {c["id"] for c in components}
        n = 1
        while sid in taken:
            # 接尾辞を付けても id の上限を超えないようにする
            suffix = "-system" if n == 1 else f"-system-{n}"
            sid = (to_id(system_name) or "system")[:MAX_ID_CHARS - len(suffix)].strip("-") + suffix
            n += 1
        components.insert(0, {
            "id": sid, "name": system_name, "level": "system", "parent": None, "technology": None,
            "description": f"{system_name}（コードベースから自動生成）", "tags": ["internal"],
        })
        system_ids.add(sid)
        fallback = sid
        warnings.append(f"このリポジトリのシステムが含まれていなかったため、'{sid}' を追加しました")

    kept = []
    for c in components:
        if c["level"] == "container" and c["parent"] not in system_ids:
            if fallback is None:
                warnings.append(f"'{c['id']}' を除外しました（親の system を特定できません）")
                continue
            c["parent"] = fallback
        kept.append(c)

    container_ids = {c["id"] for c in kept if c["level"] == "container"}
    result = []
    for c in kept:
        if c["level"] == "component" and c["parent"] not in container_ids:
            warnings.append(f"'{c['id']}' を除外しました（親の container '{c['parent'] or '未指定'}' がありません）")
            continue
        result.append(c)
    return result


def _normalize_relations(raw_relations, components, resolve, removed_parent, warnings):
    ids = {c["id"] for c in components}
    relations = []
    seen = set()
    for item in raw_relations if isinstance(raw_relations, list) else []:
        if not isinstance(item, dict):
            continue
        ends = []
        from_removed = False
        for value in (item.get("from"), item.get("to")):
            ref = resolve(value)
            if ref not in ids:
                key = to_id(value)
                if key in removed_parent:
                    ref, from_removed = removed_parent[key], True
                elif to_kebab(value) in removed_parent:
                    ref, from_removed = removed_parent[to_kebab(value)], True
            ends.append(ref)
        src, dst = ends
        if src not in ids or dst not in ids:
            if not from_removed:
                warnings.append(f"関係 '{src or '?'}' → '{dst or '?'}' を除外しました（要素が存在しません）")
            continue
        if src == dst or (src, dst) in seen:
            continue
        description = clean(item.get("description"), MAX_DESCRIPTION_CHARS)
        if not description:
            warnings.append(f"関係 '{src}' → '{dst}' を除外しました（説明がありません）")
            continue
        if len(relations) >= MAX_RELATIONS:
            warnings.append(f"関係が {MAX_RELATIONS} 件を超えたため、残りを除外しました")
            break
        seen.add((src, dst))
        relations.append({
            "from": src,
            "to": dst,
            "description": description,
            "technology": clean(item.get("technology"), MAX_TECHNOLOGY_CHARS) or None,
        })
    return relations


def build_model(components, relations, version, base=None):
    """components.yaml の内容を作る。base（既存の components.yaml）の components / relations 以外のキーは残す"""
    from schema_utils import CURRENT_SCHEMA_VERSION

    # 正規化した要素（キーがすべてある）と、generated-model.json に保存した要素（省略したキーがない）の両方を受け付ける
    out_components = []
    for c in components:
        entry = {"id": c["id"], "name": c["name"], "level": c["level"]}
        if c.get("parent"):
            entry["parent"] = c["parent"]
        if c.get("technology"):
            entry["technology"] = c["technology"]
        entry["description"] = c["description"]
        if c.get("tags"):
            entry["tags"] = list(c["tags"])
        out_components.append(entry)
    out_relations = []
    for r in relations:
        entry = {"from": r["from"], "to": r["to"], "description": r["description"]}
        if r.get("technology"):
            entry["technology"] = r["technology"]
        out_relations.append(entry)
    base = base if isinstance(base, dict) else {}
    model = {
        "schema_version": base.get("schema_version") or CURRENT_SCHEMA_VERSION,
        "version": str(version),
        "model": base["model"] if isinstance(base.get("model"), dict) else {"type": "c4"},
        "components": out_components,
        "relations": out_relations,
    }
    # 未知のキー（将来のスキーマ拡張等）は消さない
    model.update({k: v for k, v in base.items() if k not in model})
    return model


def dump_yaml(model):
    body = yaml.dump(model, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return (
        "# Architecture Tracker — コンポーネント定義\n"
        "# gh architecture-tracker generate-model でコードベースから生成した初期案です。SPA のエディタで調整してください\n"
        + body
    )


def summarize(model):
    counts = {level: 0 for level in LEVELS}
    for c in model["components"]:
        counts[c["level"]] += 1
    return (f"System {counts['system']} / Container {counts['container']} / "
            f"Component {counts['component']} / 関係 {len(model['relations'])}")


# --- データブランチ ---


def _gh(method, path, body=None, raw=False):
    """gh api を呼び出して本文を返す。失敗時は GhApiError（stderr に HTTP ステータスを含む）"""
    cmd = ["gh", "api", "-X", method, path]
    if raw:
        cmd += ["-H", "Accept: application/vnd.github.raw"]
    payload = None
    if body is not None:
        cmd += ["--input", "-"]
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        result = subprocess.run(cmd, input=payload, capture_output=True, timeout=60)
    except FileNotFoundError:
        raise GenerateError("gh コマンドが見つかりません。")
    except subprocess.TimeoutExpired:
        raise GenerateError(f"GitHub API の呼び出しがタイムアウトしました: {method} {path}")
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise GhApiError(f"GitHub API の呼び出しに失敗しました: {method} {path}\n{stderr}", stderr)
    return (result.stdout or b"").decode("utf-8", errors="replace")


def gh_api(method, path, body=None):
    text = _gh(method, path, body)
    try:
        return json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        raise GenerateError(f"GitHub API の応答を解析できませんでした: {method} {path}")


def fetch_data_file(repo, name, ref):
    """データブランチ（またはそのコミット）にあるファイルの内容。なければ None"""
    try:
        return _gh("GET", f"repos/{repo}/contents/{quote(name)}?ref={quote(ref, safe='')}", raw=True)
    except GhApiError as e:
        if "HTTP 404" in e.stderr:
            return None
        raise


def load_config_model(repo, branch):
    """config.yaml の llm.model（取得できなければ None）"""
    try:
        text = fetch_data_file(repo, "config.yaml", branch)
        config = yaml.safe_load(text) if text else None
    except (GenerateError, yaml.YAMLError):
        return None
    llm = config.get("llm") if isinstance(config, dict) else None
    model = llm.get("model") if isinstance(llm, dict) else None
    return str(model).strip() if model else None


def _parse_version(value):
    from bump_version import parse_version
    return parse_version(value)


def next_version_for(components_text):
    """既存の components.yaml に適用できるか確かめ、適用後のモデルバージョンを返す"""
    return applicable_base(components_text)[0]


def applicable_base(components_text):
    """既存の components.yaml に適用できるか確かめる。

    適用できるのは components と relations がどちらも空（または未定義）の場合だけ。
    リスト以外の値（dict 等）が入っている場合も、壊れたデータを上書きしないよう適用しない。
    Returns: (適用後のモデルバージョン, 既存の内容（未知のキーを残すために使う）)
    """
    from schema_utils import SUPPORTED_SCHEMA_VERSIONS, get_schema_version

    if components_text is None:
        return "1", {}
    try:
        data = yaml.safe_load(components_text) or {}
    except yaml.YAMLError as e:
        raise PreconditionError(f"既存の components.yaml を解析できないため適用できません: {e}")
    if not isinstance(data, dict):
        raise PreconditionError("既存の components.yaml の形式が不正なため適用できません。")
    schema = get_schema_version(data)
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise PreconditionError(
            f"components.yaml の schema_version \"{sanitize(schema)}\" に対応していないため適用できません。"
            "gh architecture-tracker update でツールを更新してください。"
        )
    for key, label in (("components", "要素"), ("relations", "関係")):
        existing = data.get(key)
        if existing is None or existing == []:
            continue
        if not isinstance(existing, list):
            raise PreconditionError(f"既存の components.yaml の {key} がリストではないため適用できません。")
        raise PreconditionError(
            f"既存のモデルに {len(existing)} 件の{label}が定義されているため適用できません"
            "（確定済みのモデルを上書きしないため、適用は要素も関係も1つもない場合に限ります）。"
            "apply を指定せずに実行した生成結果（Job Summary / generated-model.json）を参考に、SPA のエディタで追加してください。"
        )
    return str(_parse_version(data.get("version")) + 1), data


def recorded_prs(mappings_text):
    """モデルが空の間に「影響なし」で記録された PR のキー（PR 番号。外部リポジトリは owner/repo#N）の一覧。

    components が空のエントリだけを対象にする（コンポーネントを選んで確定済みの PR は含めない）。読めなければ空
    """
    try:
        data = json.loads(mappings_text) if mappings_text and mappings_text.strip() else {}
    except json.JSONDecodeError:
        return []
    mappings = data.get("mappings") if isinstance(data, dict) else None
    if not isinstance(mappings, dict):
        return []

    def order(key):
        num = re.search(r"(\d+)$", key)
        return (1 if "#" in key else 0, int(num.group(1)) if num else 0, key)

    empty = [str(k) for k, v in mappings.items()
             if not (isinstance(v, dict) and isinstance(v.get("components"), list) and v["components"])]
    return sorted(empty, key=order)


def require_data_branch(repo, branch):
    """データブランチの最新コミットの SHA。ブランチがなければ GenerateError"""
    try:
        ref = gh_api("GET", f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}")
    except GhApiError as e:
        if "HTTP 404" in e.stderr:
            raise GenerateError(
                f"{repo} に {branch} ブランチがありません。'gh architecture-tracker init' でセットアップしてください。"
            )
        raise
    return ref["object"]["sha"]


def check_applicable(repo, branch):
    """適用の事前条件（モデルが空）を、LLM を呼ぶ前に確かめる。

    Returns: (適用後のモデルバージョン, mappings.json に記録済みの PR の一覧)
    """
    head = require_data_branch(repo, branch)
    version = next_version_for(fetch_data_file(repo, "components.yaml", head))
    return version, recorded_prs(fetch_data_file(repo, "mappings.json", head))


def preview_version(repo, branch):
    """plan の表示に入れるモデルバージョン（適用した場合と同じ値。取得できなければ "1"）"""
    try:
        text = fetch_data_file(repo, "components.yaml", branch)
        data = yaml.safe_load(text) if text else None
    except (GenerateError, yaml.YAMLError):
        return "1"
    if not isinstance(data, dict):
        return "1"
    return str(_parse_version(data.get("version")) + 1)


def build_changelog(changelog_text, model, author):
    if changelog_text and changelog_text.strip():
        try:
            data = json.loads(changelog_text)
        except json.JSONDecodeError as e:
            raise GenerateError(f"model-changelog.json を解析できないため適用できません: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
            raise GenerateError("model-changelog.json の形式が不正なため適用できません。")
    else:
        data = {"version": "1.0", "entries": []}
    changes = [
        {"type": "add", "target": "component", "id": c["id"], "summary": f"{c['name']} を追加（コードベースから自動生成）"}
        for c in model["components"]
    ] + [
        {"type": "add", "target": "relation", "id": f"{r['from']}->{r['to']}", "summary": f"{r['from']} → {r['to']} の関係を追加（コードベースから自動生成）"}
        for r in model["relations"]
    ]
    entry = {
        "model_version": model["version"],
        "timestamp": now_iso(),
        "changes": changes,
        "trigger": {"type": "manual"},
        "author": author,
    }
    data["entries"] = [entry] + list(data.get("entries") or [])
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


MAX_COMMIT_RETRIES = 4


def commit_to_data_branch(repo, branch, prepare, sleep=time.sleep):
    """データブランチの最新コミットを基に、prepare(head_sha) が返すファイルを1コミットで書き込む。

    prepare は (files: {path: content}, message, result) を返す。事前条件の確認も prepare の中で行う
    （書き込みの基にするコミットで確かめるため）。ブランチは fast-forward でのみ更新し、
    他の書き込みと競合した場合は、最新のコミットを読み直して prepare からやり直す。
    Returns: (commit_sha, result)
    """
    ref_path = f"repos/{repo}/git/refs/heads/{quote(branch, safe='')}"
    for attempt in range(MAX_COMMIT_RETRIES + 1):
        head_sha = require_data_branch(repo, branch)
        base_tree = gh_api("GET", f"repos/{repo}/git/commits/{head_sha}")["tree"]["sha"]
        files, message, result = prepare(head_sha)
        tree = gh_api("POST", f"repos/{repo}/git/trees", {
            "base_tree": base_tree,
            "tree": [{"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in files.items()],
        })
        commit = gh_api("POST", f"repos/{repo}/git/commits", {
            "message": message, "tree": tree["sha"], "parents": [head_sha],
        })
        if _update_ref(repo, branch, ref_path, commit["sha"]):
            return commit["sha"], result
        if attempt == MAX_COMMIT_RETRIES:
            raise GenerateError(
                f"{branch} ブランチへの書き込みが他の処理と競合し続けたため中止しました。再実行してください。"
            )
        # 同時に失敗した run どうしが再びぶつからないよう、待ち時間に揺らぎを入れる
        wait = 2 ** attempt + random.uniform(0, 1)
        print(f"⚠ {branch} ブランチが更新されたため、読み直してやり直します（{wait:.1f} 秒後）", file=sys.stderr)
        sleep(wait)
    raise AssertionError("unreachable")


def _update_ref(repo, branch, ref_path, commit_sha):
    """ブランチを fast-forward で commit_sha に進める。競合（fast-forward でない）なら False"""
    try:
        gh_api("PATCH", ref_path, {"sha": commit_sha, "force": False})
        return True
    except GenerateError as e:
        if isinstance(e, GhApiError) and "HTTP 422" in e.stderr and "fast forward" in e.stderr.lower():
            return False
        # 応答がタイムアウト・5xx でも、更新自体は成功していることがある。ブランチが自分のコミットを指しているか、
        # その後に他の処理がブランチを進めていても自分のコミットを含んでいれば、成功とする
        head = require_data_branch(repo, branch)
        if head == commit_sha or _contains_commit(repo, head, commit_sha):
            return True
        raise


def _contains_commit(repo, head, commit_sha):
    """head の履歴に commit_sha が含まれるか（確かめられなければ False）"""
    try:
        compare = gh_api("GET", f"repos/{repo}/compare/{commit_sha}...{head}")
    except GenerateError:
        return False
    return (compare or {}).get("status") in ("ahead", "identical")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_draft(repo, branch, repo_label):
    """保存済みの生成結果（generated-model.json）。ない・読めない・形式が違う・内容が壊れている場合は None。

    データブランチに書ける人が書き換えられるため、生成したときと同じ正規化を通し、
    結果が保存された内容と一致しなければ使わない。
    """
    text = fetch_data_file(repo, DRAFT_FILE, branch)
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"⚠ {DRAFT_FILE} を解析できないため、使いません", file=sys.stderr)
        return None
    if not isinstance(data, dict) or data.get("format_version") != DRAFT_FORMAT_VERSION:
        return None
    if not isinstance(data.get("components"), list) or not isinstance(data.get("relations"), list):
        return None
    if data.get("level") not in ("container", "component") or not COMMIT_SHA_RE.fullmatch(str(data.get("commit"))):
        return None
    components, relations, _ = normalize_model(data, data["level"], repo_label)
    if not components:
        return None
    normalized = build_model(components, relations, "")
    if normalized["components"] != data["components"] or normalized["relations"] != data["relations"]:
        print(f"⚠ {DRAFT_FILE} の内容が正規化の条件を満たさないため、使いません", file=sys.stderr)
        return None
    for key in ("generated_at", "provider", "llm_model"):
        data[key] = sanitize(data.get(key) or "")
    data["warnings"] = [sanitize(w) for w in data.get("warnings") or [] if isinstance(w, str)]
    data["input_files"] = [sanitize(f) for f in data.get("input_files") or [] if isinstance(f, str)]
    if not isinstance(data.get("applied"), dict):
        data["applied"] = None
    elif "recorded_prs" in data["applied"]:
        prs = data["applied"]["recorded_prs"]
        data["applied"]["recorded_prs"] = [sanitize(k) for k in prs if isinstance(k, str)] if isinstance(prs, list) else []
    return data


def draft_reusable(draft, commit, level, provider=None, llm_model=None):
    """保存済みの生成結果を再利用できるか。

    解析したコミットと粒度が同じで、LLM のプロバイダー・モデルも同じ場合に再利用する
    （API キーがなくプロバイダーを決められない場合は、プロバイダー・モデルは比べない）。
    """
    if not draft or draft.get("commit") != commit or draft.get("level") != level:
        return False
    if provider is None:
        return True
    return draft.get("provider") == provider and draft.get("llm_model") == (llm_model or "")


def build_draft(commit, level, components, relations, warnings, files, provider, llm_model, apply_blocked=None):
    model = build_model(components, relations, "")
    return {
        "format_version": DRAFT_FORMAT_VERSION,
        "commit": commit,
        "level": level,
        "generated_at": now_iso(),
        "provider": provider,
        "llm_model": llm_model or "",
        "components": model["components"],
        "relations": model["relations"],
        "warnings": warnings,
        "input_files": files,
        # 生成した時点で apply できない理由（既存のモデルがある等）。表示用で、apply のときは改めて確かめる
        "apply_blocked": apply_blocked,
        "applied": None,
    }


def dump_draft(draft):
    return json.dumps(draft, indent=2, ensure_ascii=False) + "\n"


def save_draft(repo, branch, draft, sleep=time.sleep):
    def prepare(head_sha):
        message = (f"Save generated model ({draft['level']}, commit {draft['commit'][:7]})\n\n"
                   "Generated via gh architecture-tracker generate-model.")
        return {DRAFT_FILE: dump_draft(draft)}, message, None

    return commit_to_data_branch(repo, branch, prepare, sleep)[0]


def apply_draft(repo, branch, draft, author, sleep=time.sleep):
    """生成結果を components.yaml に適用し、model-changelog.json と generated-model.json の applied を同じコミットで書き込む。

    事前条件（モデルが空）は、書き込みの基にするコミットで確かめる（競合でやり直すときも確かめ直す）。
    記録済み PR の一覧も、同じコミットの mappings.json から作る。
    Returns: (model, commit_sha, recorded_prs)
    """
    def prepare(head_sha):
        version, base = applicable_base(fetch_data_file(repo, "components.yaml", head_sha))
        model = build_model(draft["components"], draft["relations"], version, base)
        changelog = build_changelog(fetch_data_file(repo, "model-changelog.json", head_sha), model, author)
        recorded = recorded_prs(fetch_data_file(repo, "mappings.json", head_sha))
        applied_draft = dict(draft, apply_blocked=None,
                             applied={"model_version": version, "applied_at": now_iso(), "applied_by": author,
                                      "recorded_prs": recorded})
        files = {
            "components.yaml": dump_yaml(model),
            "model-changelog.json": changelog,
            DRAFT_FILE: dump_draft(applied_draft),
        }
        message = (f"Generate initial model from codebase (model v{version})\n\n"
                   f"Applied the generated model (commit {draft['commit'][:7]}) "
                   "via gh architecture-tracker generate-model.")
        return files, message, (model, recorded)

    commit_sha, (model, recorded) = commit_to_data_branch(repo, branch, prepare, sleep)
    # バージョンタグ（bump-model-version と同じ命名）。モデル自体は適用済みのため、失敗しても警告のみ
    try:
        gh_api("POST", f"repos/{repo}/git/refs", {"ref": f"refs/tags/model-v{model['version']}", "sha": commit_sha})
    except GenerateError:
        print(f"::warning::タグ model-v{model['version']} を作成できませんでした（モデルは適用済みです）", file=sys.stderr)
    return model, commit_sha, recorded


# --- Job Summary ---


MAX_LISTED_PRS = 20


def _code_block(text, lang=""):
    # 内容に ``` が含まれても閉じないよう、より長いフェンスを使う
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{lang}\n{text}\n{fence}"


def recorded_prs_message(keys):
    # モデルが空の間に記録された PR は「影響なし」のまま残り、catch-up は記録済みの PR を取り直さない
    listed = ", ".join(k if "#" in k else f"#{k}" for k in keys[:MAX_LISTED_PRS])
    more = f" ほか {len(keys) - MAX_LISTED_PRS} 件" if len(keys) > MAX_LISTED_PRS else ""
    return (
        f"mappings.json に記録済みの PR が {len(keys)} 件あります（モデルが空の間の記録は「影響なし」になっています）: "
        f"{listed}{more}\n"
        "catch-up は記録済みの PR を取り直しません。Architecture Tracker のコメントがある PR は、コメントを再生成して"
        "（gh workflow run architecture-tracker.yml -f pr-number=<番号>）チェックボックスで確定してください。"
        "catch-up で記録した PR（コメントなし）では再生成が失敗するため、実行しないでください（取り直すコマンドはありません）。"
    )


def apply_command(level):
    # plan と同じ粒度を指定する（省略すると component で作り直してしまう）
    return f"gh architecture-tracker generate-model --apply --level {level}"


ISO_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MODEL_NAME_RE = re.compile(r"(?!.*://)[A-Za-z0-9._:@/\[\]-]{1,100}")
PROVIDER_RE = re.compile(r"anthropic|openai|claude-code")


def _table_safe(value, pattern):
    """コードブロックの外（表）に入れる値。データブランチ・config.yaml 由来のため、形式が合わなければ表示しない
    （画像・リンク・| による表崩し等の Markdown を注入されないようにする）"""
    text = "" if value is None else str(value)
    return text if pattern.fullmatch(text) else "（不明）"


def build_summary(result):
    """Job Summary（Markdown）。LLM・リポジトリ由来の文字列はコードブロックか Mermaid の中にだけ入れる"""
    from generate_mermaid import build_structure_diagram

    draft = result["draft"]
    model = result["model"]
    mode = "apply" if result["apply"] else "plan"
    model_name = _table_safe(draft["llm_model"], MODEL_NAME_RE) if draft["llm_model"] else ""
    llm_label = f"{_table_safe(draft['provider'], PROVIDER_RE)}{' / ' + model_name if model_name else ''}"
    if result["reused"]:
        generated = (f"前回の生成結果を再利用（{_table_safe(draft['generated_at'], ISO_DATETIME_RE)}、{llm_label}。"
                     "作り直すには regenerate を指定）")
    else:
        generated = f"新しく生成（{llm_label}）"
    if result["applied"]:
        applied = f"✅ 適用しました（モデル v{model['version']}）"
    elif result["apply"]:
        applied = "✖ 適用できませんでした"
    elif result.get("apply_blocked"):
        applied = "適用できません（理由は下の「注意」を参照。生成結果を参考に SPA のエディタで追加してください）"
    else:
        applied = "適用していません（plan）"

    lines = [
        f"## 🏗 Architecture Tracker — モデル生成（{mode}）",
        "",
        "| 項目 | 内容 |",
        "|---|---|",
        f"| 解析したコミット | `{draft['commit'][:7]}` |",
        f"| 粒度 | {draft['level']} |",
        f"| 生成 | {generated} |",
        f"| 結果 | {summarize(model)} |",
        f"| 適用 | {applied} |",
        "",
    ]
    if not result["apply"] and not result.get("apply_blocked"):
        lines += ["この内容を適用するには:", "", _code_block(apply_command(draft["level"]), "bash"), ""]
    if result["applied"]:
        lines += ["> [!IMPORTANT]",
                  "> SPA のエディタを開いている場合は、再読み込みしてから編集してください。"
                  "適用前に開いた画面から保存すると、適用したモデルが上書きされます。", ""]
    notes = list(draft.get("warnings") or [])
    if result.get("error"):
        notes.insert(0, f"エラー: {result['error']}")
    if result.get("apply_blocked") and not result["apply"]:
        notes.insert(0, f"apply できません: {result['apply_blocked']}")
    if result.get("recorded_prs"):
        notes.append(recorded_prs_message(result["recorded_prs"]))
    if notes:
        lines += ["### ⚠ 注意", "", _code_block("\n".join(sanitize_multiline(n) for n in notes)), ""]
    lines += ["### 構成図", "", _code_block(build_structure_diagram(model), "mermaid"), ""]
    lines += [
        "<details><summary>components.yaml（生成結果）</summary>", "",
        _code_block(dump_yaml(model).rstrip("\n"), "yaml"), "", "</details>", "",
    ]
    files = draft.get("input_files") or []
    lines += [
        f"<details><summary>LLM に送ったファイル（{len(files)} 件。ほかにディレクトリ構造を送っています）</summary>", "",
        _code_block("\n".join(sanitize(f) for f in files) or "（なし）"), "", "</details>", "",
    ]
    return "\n".join(lines)


# --- エントリーポイント ---


def head_commit(root):
    code, out, err_text = run_git(root, "rev-parse", "HEAD")
    if code != 0:
        raise GenerateError(f"解析するリポジトリのコミットを取得できませんでした: {err_text}")
    return out.strip()


def generate(args, repo_label, commit, provider, llm_model):
    """コードベースを収集して LLM で生成する。Returns: (components, relations, warnings, files)"""
    collected = collect_codebase(args.root)
    if collected["file_count"] == 0:
        raise GenerateError("Git で管理しているファイルがありません。")
    if provider is None:
        raise GenerateError(
            "LLM の API キーが設定されていません。リポジトリの Secrets に ANTHROPIC_API_KEY / OPENAI_API_KEY / "
            "CLAUDE_CODE_OAUTH_TOKEN のいずれかを設定してください。"
        )
    files = input_files(collected)
    print(f"ℹ {provider}{' / ' + sanitize(llm_model) if llm_model else ''} でモデルを生成します"
          f"（コミット {commit[:7]}、送信するファイル {len(files)} 件）", file=sys.stderr)
    raw = parse_response(call_llm(provider, build_prompt(collected, args.level, repo_label), llm_model))
    components, relations, warnings = normalize_model(raw, args.level, repo_label)
    if not components:
        raise GenerateError("生成結果に有効な要素がありませんでした。regenerate を指定して再実行してください。")
    return components, relations, warnings, files


def apply_blocked_reason(repo, branch):
    """現在の components.yaml に apply できない理由。plan の表示用。

    Returns: (理由（できるなら None）, 確かめられたか)。API の一時的な失敗で確かめられない場合は
    (None, False) を返し、保存済みの値を変えない（失敗のメッセージを「適用できない理由」として保存しない）
    """
    try:
        applicable_base(fetch_data_file(repo, "components.yaml", branch))
    except PreconditionError as e:
        return str(e), True
    except GenerateError as e:
        print(f"⚠ apply できるかを確かめられませんでした: {sanitize(e)}", file=sys.stderr)
        return None, False
    return None, True


def run(args):
    repo_label = args.repo.split("/", 1)[1]
    commit = head_commit(args.root)
    require_data_branch(args.repo, args.data_branch)

    # 適用できない場合は、LLM を呼ぶ前に中止する
    apply_blocked, blocked_known = None, True
    if args.apply:
        expected_version, _ = check_applicable(args.repo, args.data_branch)
    else:
        expected_version = preview_version(args.repo, args.data_branch)
        apply_blocked, blocked_known = apply_blocked_reason(args.repo, args.data_branch)

    provider = load_llm_utils().detect_provider()
    llm_model = resolve_model(args.model, provider, load_config_model(args.repo, args.data_branch)) if provider else None

    draft = None if args.regenerate else load_draft(args.repo, args.data_branch, repo_label)
    # apply では、plan で確認した生成結果をコミットと粒度だけで探す（plan の後に LLM のプロバイダーや
    # モデルの設定が変わっても、確認していない結果で作り直して適用しないため）。
    # plan では、設定が変わっていれば作り直す
    reused = draft_reusable(draft, commit, args.level, None if args.apply else provider, llm_model)
    if reused:
        print(f"ℹ コミット {commit[:7]}（{args.level}）の生成結果を再利用します"
              f"（{sanitize(draft['generated_at'])}）", file=sys.stderr)
        # plan のたびに apply できるかを表示し直すため、変わっていれば保存し直す（CLI が保存内容で判断する）
        if not args.apply and blocked_known and draft.get("apply_blocked") != apply_blocked:
            draft = dict(draft, apply_blocked=apply_blocked)
            save_draft(args.repo, args.data_branch, draft)
    else:
        components, relations, warnings, files = generate(args, repo_label, commit, provider, llm_model)
        draft = build_draft(commit, args.level, components, relations, warnings, files, provider, llm_model,
                            apply_blocked)
        save_draft(args.repo, args.data_branch, draft)

    result = {
        "apply": args.apply, "applied": False, "reused": reused, "draft": draft, "recorded_prs": [],
        "apply_blocked": apply_blocked,
        "model": build_model(draft["components"], draft["relations"], expected_version),
    }
    if args.apply:
        try:
            result["model"], result["commit"], result["recorded_prs"] = apply_draft(
                args.repo, args.data_branch, draft, args.author)
            result["applied"] = True
        except GenerateError as e:
            result["error"] = str(e)
    return result


def write_outputs(args, result):
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as f:
            f.write(build_summary(result) + "\n")
    output = {
        "format_version": DRAFT_FORMAT_VERSION,
        "commit": result["draft"]["commit"],
        "level": result["draft"]["level"],
        "reused": result["reused"],
        "applied": result["applied"],
        "model_version": result["model"]["version"] if result["applied"] else "",
        "summary": summarize(result["model"]),
        "recorded_prs": result["recorded_prs"],
        "error": result.get("error", ""),
    }
    print(json.dumps(output, ensure_ascii=False))


def build_parser():
    parser = argparse.ArgumentParser(description="コードベースから C4 モデルを生成する（plan / apply）")
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--data-branch", default=DATA_BRANCH)
    parser.add_argument("--root", default=".", help="解析するリポジトリ（チェックアウト先）")
    parser.add_argument("--level", choices=("container", "component"), default="component")
    parser.add_argument("--apply", action="store_true", help="生成結果を components.yaml に適用する")
    parser.add_argument("--regenerate", action="store_true", help="保存済みの生成結果を使わずに作り直す")
    parser.add_argument("--model", default="")
    parser.add_argument("--author", default="github-actions[bot]")
    parser.add_argument("--summary-file", default="", help="Job Summary の出力先（$GITHUB_STEP_SUMMARY）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except GenerateError as e:
        # ワークフローコマンドは1行にする（改行が残ると、次の行から別のワークフローコマンドを注入できるため）
        print(f"::error::{sanitize(e)}", file=sys.stderr)
        if args.summary_file:
            with open(args.summary_file, "a", encoding="utf-8") as f:
                f.write("## 🏗 Architecture Tracker — モデル生成\n\n" + _code_block(f"エラー: {sanitize_multiline(e)}") + "\n")
        return 1
    write_outputs(args, result)
    if result.get("error"):
        print(f"::error::{sanitize(result['error'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
