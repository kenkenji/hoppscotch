"""
LLM API呼び出しの共通ユーティリティ。

extract_components.py、detect_changes.py、analyze_impact.py で共有する関数群。
"""

import json
import os
import subprocess
import sys
import time

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"


def detect_provider():
    """環境変数からLLMプロバイダーを自動検出する。
    優先順位: ANTHROPIC_API_KEY > OPENAI_API_KEY > CLAUDE_CODE_OAUTH_TOKEN"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "claude-code"
    return None


def call_anthropic(prompt, model=None, max_tokens=1024, max_retries=2, timeout=60.0):
    """Anthropic Claude APIを呼び出す。レート制限時にリトライする。"""
    import anthropic

    model = model or DEFAULT_ANTHROPIC_MODEL
    client = anthropic.Anthropic(timeout=timeout)
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"Rate limited, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except anthropic.APITimeoutError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"Timeout, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise


def call_openai(prompt, model=None, max_tokens=1024, max_retries=2, timeout=60.0):
    """OpenAI APIを呼び出す。レート制限時にリトライする。"""
    import openai

    model = model or DEFAULT_OPENAI_MODEL
    client = openai.OpenAI(timeout=timeout)
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        except openai.RateLimitError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"Rate limited, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except openai.APITimeoutError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"Timeout, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise


def call_claude_code(prompt, model=None, max_retries=2, timeout=300):
    """Claude Code CLIを呼び出す。CLAUDE_CODE_OAUTH_TOKENで認証する。
    プロンプトはstdin経由で渡す（コマンドライン引数長制限の回避）。"""
    model = model or DEFAULT_ANTHROPIC_MODEL
    cmd = ["claude", "-p", "--output-format", "text", "--model", model, "--max-turns", "3"]

    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                error_detail = result.stderr.strip() or result.stdout.strip()
                if attempt < max_retries:
                    wait = 2 ** (attempt + 1)
                    print(
                        f"CLI exited with code {result.returncode}, retrying in {wait}s...\n"
                        f"  detail: {error_detail[:500]}",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"claude CLI exited with code {result.returncode}: {error_detail[:2000]}"
                )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"CLI timeout, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise RuntimeError("claude CLI timed out after all retries")


def parse_llm_response(response_text):
    """LLMレスポンスからJSONを抽出してパースする。
    raw_decode()で最初のJSONオブジェクトのみをパースし、
    後続のテキストやJSONブロックは無視する。"""
    text = response_text.strip()

    decoder = json.JSONDecoder()
    idx = text.find("{")
    if idx != -1:
        result, _ = decoder.raw_decode(text, idx)
        return result

    return json.loads(text)


def call_llm(provider, prompt, model=None, max_tokens=1024, timeout=None):
    """プロバイダーに応じたLLM APIを呼び出す。
    max_tokensはclaude-codeプロバイダーでは無視される（CLIに該当フラグなし）。
    timeoutを指定すると各プロバイダーのデフォルト値を上書きする。"""
    if provider == "anthropic":
        kwargs = {"max_tokens": max_tokens}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return call_anthropic(prompt, model, **kwargs)
    elif provider == "openai":
        kwargs = {"max_tokens": max_tokens}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return call_openai(prompt, model, **kwargs)
    elif provider == "claude-code":
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return call_claude_code(prompt, model, **kwargs)
    else:
        raise ValueError(f"Unknown provider: {provider}")
