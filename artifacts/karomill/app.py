"""
カロミル食事スクショ → Googleスプレッドシート自動記録アプリ
"""
import concurrent.futures
import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import date, datetime
from functools import wraps

import gspread
from flask import Flask, Response, jsonify, render_template, request
from google import genai
from google.genai import types
from oauth2client.service_account import ServiceAccountCredentials

# ──────────────────────────────────────────────────────────────
# アプリ・環境変数
# ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB（動画対応）

BASIC_AUTH_USERNAME         = os.environ.get("BASIC_AUTH_USERNAME", "")
BASIC_AUTH_PASSWORD         = os.environ.get("BASIC_AUTH_PASSWORD", "")
GEMINI_API_KEY              = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SPREADSHEET_URL             = "https://docs.google.com/spreadsheets/d/1z9PX6D_zbd1fhDDATzOOZNlLd3Ux1eMchoDBrMi7qCA/edit"
START_DATE                  = date(2026, 3, 31)
WEEKDAYS                    = ["月", "火", "水", "木", "金", "土", "日"]
COUNT_UNITS                 = {"個", "本", "枚", "株", "杯", "缶", "食", "袋", "切", "片"}

# ──────────────────────────────────────────────────────────────
# 動画ジョブキュー（in-memory）
# ──────────────────────────────────────────────────────────────
# job_id -> {"status": "processing"|"done"|"error", ...result fields}
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _job_set(job_id: str, payload: dict) -> None:
    with _JOBS_LOCK:
        _JOBS[job_id] = payload


def _job_get(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _run_video_job(job_id: str, video_path: str, mime_type: str, user_date: str) -> None:
    """バックグラウンドスレッドで動画解析→スプレッドシート書き込みを実行する"""
    try:
        data = analyze_video(video_path, mime_type, override_date=user_date)
        try:
            tab_name = write_to_spreadsheet(data)
            _job_set(job_id, {
                "status":      "done",
                "success":     True,
                "tab":         tab_name,
                "date":        data.get("date"),
                "weight":      data.get("weight"),
                "gemini_text": data.get("_gemini_text", ""),
                "data":        {k: v for k, v in data.items() if k != "_gemini_text"},
            })
        except Exception as e:
            _job_set(job_id, {
                "status":      "done",
                "success":     False,
                "error":       f"スプレッドシート書き込みエラー: {e}",
                "gemini_text": data.get("_gemini_text", ""),
                "data":        {k: v for k, v in data.items() if k != "_gemini_text"},
            })
    except Exception as e:
        _job_set(job_id, {
            "status":  "done",
            "success": False,
            "error":   f"AI解析エラー: {e}",
        })
    finally:
        try:
            os.unlink(video_path)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# Basic 認証
# ──────────────────────────────────────────────────────────────
def basic_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != BASIC_AUTH_USERNAME or auth.password != BASIC_AUTH_PASSWORD:
            return Response(
                "認証が必要です。",
                401,
                {"WWW-Authenticate": 'Basic realm="Karomill"'},
            )
        return f(*args, **kwargs)
    return decorated

# ──────────────────────────────────────────────────────────────
# Gemini モデルチェーン（起動時に利用可能モデルを自動検出）
# ──────────────────────────────────────────────────────────────
_PREFERRED_MODELS: list = [
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.0-flash-lite",
]


def _build_model_chain(api_key: str) -> list:
    """APIで利用可能な flash モデルを検索し優先順で返す。失敗時はフォールバック。"""
    env_model = os.environ.get("GEMINI_MODEL", "")
    candidates = ([env_model] + _PREFERRED_MODELS) if env_model else list(_PREFERRED_MODELS)
    # 重複除去（順序を保持）
    seen: set = set()
    candidates = [m for m in candidates if not (m in seen or seen.add(m))]

    if not api_key:
        return candidates
    try:
        client = genai.Client(api_key=api_key)
        available: set = {
            m.name.replace("models/", "")
            for m in client.models.list()
            if "generateContent" in str(
                getattr(m, "supported_generation_methods", None)
                or getattr(m, "supported_actions", None)
                or ""
            )
        }
        chain = [m for m in candidates if m in available]
        chain += sorted(n for n in available if "flash" in n and n not in chain)
        if chain:
            print(f"[Karomill] 利用可能モデル: {chain}", flush=True)
            return chain
    except Exception as e:
        print(f"[Karomill] モデル検索失敗、フォールバック使用: {e}", flush=True)
    return candidates


GEMINI_MODEL_CHAIN: list = _build_model_chain(GEMINI_API_KEY)

# Gemini API 呼び出し1回あたりのタイムアウト（秒）
# Gunicorn の --timeout より短くすることで、タイムアウト時に
# ワーカーが強制終了される前にクリーンなエラーレスポンスを返せる。
_GEMINI_CALL_TIMEOUT = 180  # 3分


def _make_client() -> genai.Client:
    """Gemini クライアントを返す"""
    return genai.Client(api_key=GEMINI_API_KEY)

# ──────────────────────────────────────────────────────────────
# プロンプト
# ──────────────────────────────────────────────────────────────
SUMMARY_PROMPT = """これはカロミルアプリの「帳尻合わせ」画面のスクリーンショットです。
下記の手順に沿って情報を抽出し、最後にJSONのみを出力してください。説明文は不要です。

【カロミル帳尻合わせ画面の構造（参考）】
┌──────────────────────────────────┐
│  昨日 M/D（曜日）     ← 日付表示  │
│  ┌─ 栄養サマリー ────────────────┐ │
│  │  カロリー    実績 / 目標 kcal │ │
│  │  たんぱく質  実績 / 目標 g   │ │
│  │  脂質        実績 / 目標 g   │ │
│  │  炭水化物    実績 / 目標 g   │ │
│  │  睡眠  HH:MM〜HH:MM         │ │
│  │  体重  実績XX.X / 目標XX.X   │ │
│  └───────────────────────────── ┘ │
│  P:XX% / F:XX% / C:XX%  ← %のみ  │
└──────────────────────────────────┘

【抽出手順 — 必ずSTEP順に確認する】

STEP1: 日付
  画面上部「昨日 M/D（曜日）」または「M月D日（曜日）」から月と日を読む。
  出力形式: "M/D"（例: "7/16"、"12/3"）

STEP2: 体重
  「体重」行には「実績 / 目標」の2つの数値がある。
  必ず「/」の左側（実績）を使う。例: 76.2 / 75.0 → 76.2
  小数点1桁の数値。読み取れなければ null。

STEP3: 睡眠
  「HH:MM〜HH:MM」形式をそのまま抽出（例: "22:30〜7:00"）。
  表示がなければ空文字 ""。

STEP4: カロリー合計（total_kcal）
  「カロリー」行の「実績 / 目標 kcal」のうち「/」左の実績値のみ。
  目安: 1000〜5000 の整数。

STEP5: たんぱく質（total_P）
  「たんぱく質」行の「/」左の実績グラム数のみ。
  ⚠ 画面下部の「P:XX%」パーセンテージは絶対に使わない。
  目安: 30〜300 g。

STEP6: 脂質（total_F）
  「脂質」行の「/」左の実績グラム数のみ。
  ⚠ 「F:XX%」は絶対に使わない。目安: 10〜200 g。

STEP7: 炭水化物（total_C）
  「炭水化物」行の「/」左の実績グラム数のみ。
  ⚠ 「C:XX%」は絶対に使わない。目安: 50〜500 g。

STEP8: 自己評価・運動
  画面内に自己評価テキストがあれば抽出（なければ ""）。
  運動メモがあれば原文を抽出し exercise 配列に構造化（なければ空配列）。

【絶対にしないこと】
❌ 「/」右側の目標値をどのフィールドにも使わない
❌ 「P:XX%」「F:XX%」「C:XX%」などの%値をグラム数として使わない
❌ 読み取れない数値を推測・補完しない（null を返す）

<<<JSON_START>>>
{
  "date": "M/D形式（例: 7/16）",
  "weight": 数値のみ（例: 76.2）,
  "sleep": "HH:MM〜HH:MM（例: 22:30〜7:00）、なければ空文字",
  "total_kcal": 数値のみ,
  "total_P": 数値のみ,
  "total_F": 数値のみ,
  "total_C": 数値のみ,
  "self_evaluation": "自己評価テキスト（なければ空文字）",
  "exercise_notes": "運動メモ原文（なければ空文字）",
  "exercise": [
    {"menu": "種目名", "reps": "回数や時間", "sets": "セット数（不明なら空文字）"}
  ]
}
<<<JSON_END>>>"""


DETAIL_PROMPT = """これはカロミルアプリの食事詳細スクリーンショットです。
画像に表示されているすべての食事セクションの情報を正確に抽出し、下記JSONのみを出力してください。

【OCR最重要ルール】
・食品名のひらがな・カタカナ・漢字を一字一句そのまま書き起こすこと
・特に以下の文字を混同しないよう注意：こ↔つ、ん↔じ、い↔り、あ↔お、ぬ↔め
・ブランド名・販売元（by Amazon、みなさまのお墨付き 等）は削除
・不要な修飾語・原産地・パッケージ形状（国産、毎日、ENRGY BOOSTER、リーフパック 等）は削除
・食材の状態（生・ゆで・乾・根・皮あり等）と味の種類は必ず残すこと

【数値抽出ルール】
・「食品名 100g（50%）」→ base_amount=100, base_unit="g", percentage=50
・「食品名 小さじ1杯4.6g（100%）」→ base_amount=4.6, base_unit="g", percentage=100
・「食品名 小さじ1杯4.6g（50%）」→ base_amount=4.6, base_unit="g", percentage=50
・「食品名 大さじ1杯13.5g（30%）」→ base_amount=13.5, base_unit="g", percentage=30
・「食品名 Xml（50%）」→ base_amount=X, base_unit="ml", percentage=50
・「食品名 1個（100%）」→ base_amount=1, base_unit="個", percentage=100
・「食品名 1株（100%）」→ base_amount=1, base_unit="株", percentage=100
・「食品名（◯%）」で基準量の記載なし → base_amount=null, base_unit="pct_only", percentage=◯
・重要：「小さじN杯Xg」「大さじN杯Xg」のようにg値が明記されている場合は必ずbase_amount=そのg値、base_unit="g"で抽出すること
・P・F・C値：画像に表示されている数値をそのまま抽出（%適用前の値）
・数値が読み取れない場合は0

<<<JSON_START>>>
{
  "meals": [
    {
      "type": "朝食|昼食|夕食|間食",
      "time": "HH:MM（読み取れない場合は空文字）",
      "items": [
        {
          "name": "整形済み食品名",
          "base_amount": 数値またはnull,
          "base_unit": "g|ml|個|本|枚|株|杯|缶|食|袋|pct_only",
          "percentage": 数値（0〜100、省略なし100%の場合も100）,
          "P": たんぱく質g（数値のみ、%適用前）,
          "F": 脂質g（数値のみ、%適用前）,
          "C": 炭水化物g（数値のみ、%適用前）
        }
      ]
    }
  ]
}
<<<JSON_END>>>"""


VIDEO_PROMPT = """これはカロミルアプリの画面録画動画です。
ユーザーが帳尻合わせ画面・朝食・昼食・夕食・間食の画面をスクロールしながら録画しています。
動画全体を通じてすべての画面から情報を正確に抽出し、下記JSONのみを出力してください。説明文は一切不要です。

【絶対厳守】出力は必ず <<<JSON_START>>> で始まり <<<JSON_END>>> で終わること。マーカーを省略しないこと。

【サマリー抽出ルール（帳尻合わせ画面）— STEP順に確認すること】

帳尻合わせ画面の構造:
  上部: 昨日 M/D（曜日）← 日付
  カード内: カロリー/たんぱく質/脂質/炭水化物 それぞれ「実績 / 目標」の2値
  カード内: 睡眠 HH:MM〜HH:MM
  カード内: 体重 実績XX.X / 目標XX.X
  カード下部: P:XX% F:XX% C:XX% ← これはパーセンテージのみ、グラム数ではない

STEP1 日付: 「昨日 M/D（曜日）」から "M/D" 形式で抽出（例: "7/16"）
STEP2 体重: 「体重」行の「/」左の実績値のみ（例: 76.2）。読めなければnull
STEP3 睡眠: 「HH:MM〜HH:MM」形式をそのまま抽出。なければ ""
STEP4 total_kcal: 「カロリー」行の「/」左の実績kcalのみ（目安1000〜5000）
STEP5 total_P: 「たんぱく質」行の「/」左の実績g ⚠「P:XX%」は絶対使わない（目安30〜300g）
STEP6 total_F: 「脂質」行の「/」左の実績g ⚠「F:XX%」は絶対使わない（目安10〜200g）
STEP7 total_C: 「炭水化物」行の「/」左の実績g ⚠「C:XX%」は絶対使わない（目安50〜500g）

❌ 「/」右側の目標値をどのフィールドにも使わない
❌ 「P/F/C:XX%」などのパーセンテージをグラム数として使わない
❌ 読み取れない数値を推測・補完しない（nullを返す）

【食事詳細抽出ルール（朝食・昼食・夕食・間食画面）】
・食品名のひらがな・カタカナ・漢字を一字一句そのまま書き起こすこと
・特に以下の文字を混同しないよう注意：こ↔つ、ん↔じ、い↔り、あ↔お、ぬ↔め
・ブランド名・販売元（by Amazon、みなさまのお墨付き 等）は削除
・食材の状態（生・ゆで・乾・根・皮あり等）と味の種類は必ず残すこと
・「食品名 100g（50%）」→ base_amount=100, base_unit="g", percentage=50
・「食品名 小さじ1杯4.6g（50%）」→ base_amount=4.6, base_unit="g", percentage=50
・「食品名 大さじ1杯13.5g（30%）」→ base_amount=13.5, base_unit="g", percentage=30
・「食品名（◯%）」で基準量の記載なし → base_amount=null, base_unit="pct_only", percentage=◯
・P・F・C値：画像に表示されている数値をそのまま抽出（%適用前の値）

<<<JSON_START>>>
{
  "date": "M/D形式（例: 7/16）",
  "weight": 数値のみ（例: 76.2）,
  "sleep": "HH:MM〜HH:MM（例: 2:00〜8:00）、なければ空文字",
  "total_kcal": 数値のみ,
  "total_P": 数値のみ,
  "total_F": 数値のみ,
  "total_C": 数値のみ,
  "self_evaluation": "自己評価テキスト（なければ空文字）",
  "exercise_notes": "運動メモ原文（なければ空文字）",
  "exercise": [
    {"menu": "種目名", "reps": "回数や時間", "sets": "セット数（不明なら空文字）"}
  ],
  "meals": [
    {
      "type": "朝食|昼食|夕食|間食",
      "time": "HH:MM（読み取れない場合は空文字）",
      "items": [
        {
          "name": "整形済み食品名",
          "base_amount": 数値またはnull,
          "base_unit": "g|ml|個|本|枚|株|杯|缶|食|袋|pct_only",
          "percentage": 数値（0〜100）,
          "P": たんぱく質g（数値のみ、%適用前）,
          "F": 脂質g（数値のみ、%適用前）,
          "C": 炭水化物g（数値のみ、%適用前）
        }
      ]
    }
  ]
}
<<<JSON_END>>>"""

# ──────────────────────────────────────────────────────────────
# Gemini API — フォールバックチェーン
# ──────────────────────────────────────────────────────────────
def _is_skip_model_error(err_str: str) -> bool:
    """404・廃止モデルなど、次のモデルにスキップすべきエラーか判定する"""
    return any(k in err_str for k in (
        "404", "NOT_FOUND", "no longer available", "PerDay", "limit: 0", "not found",
    ))


def _is_retryable_error(err_str: str) -> bool:
    """リトライで回復が期待できる一時的エラーか判定する。

    - 429 / RESOURCE_EXHAUSTED : クォータ枯渇（推奨待機時間あり）
    - 503 / UNAVAILABLE        : モデル高負荷（短時間待機で回復することが多い）
    - 500 / INTERNAL           : 一時的なサーバーエラー
    """
    return any(k in err_str for k in (
        "429", "RESOURCE_EXHAUSTED",
        "503", "UNAVAILABLE",
        "500", "INTERNAL",
    ))


def _parse_retry_delay(err_str: str) -> int:
    """エラーから推奨待機秒数を読み取る。
    - 429 : Retry-After ヘッダ相当の値が含まれている場合はそれを使う。なければ 60 秒。
    - 503 / 500 : 高負荷・一時エラーなので短め (15 秒) で十分なことが多い。
    """
    m = re.search(r"retry[_ ]?(?:in|delay)[^\d]*(\d+)", err_str, re.IGNORECASE)
    if m:
        return int(m.group(1)) + 2
    # 503/500 は推奨待機秒が含まれないので短めのデフォルト
    if "503" in err_str or "UNAVAILABLE" in err_str or "500" in err_str:
        return 15
    return 60  # 429 のデフォルト


def _call_model_once(client: genai.Client, model: str, contents: list) -> str:
    """1モデルへの generate_content を _GEMINI_CALL_TIMEOUT 秒で打ち切って返す。

    SDK の HttpOptions.timeout はバージョンによって効かないケースがあるため、
    ThreadPoolExecutor で確実にタイムアウトさせる。
    タイムアウト時は RuntimeError を送出する（_generate_with_chain が次モデルへ進む）。
    executor.shutdown(wait=False) でバックグラウンドスレッドを切り離し、
    呼び出し元スレッド（Gunicorn ワーカー）がすぐに返れるようにする。
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = executor.submit(client.models.generate_content, model=model, contents=contents)
    try:
        return fut.result(timeout=_GEMINI_CALL_TIMEOUT).text
    except concurrent.futures.TimeoutError:
        raise RuntimeError(
            f"モデル {model} が {_GEMINI_CALL_TIMEOUT} 秒以内に応答しませんでした"
        )
    finally:
        # wait=False でバックグラウンドスレッドを切り離す（ハングを引き継がない）
        executor.shutdown(wait=False)


def _generate_with_chain(client: genai.Client, contents: list) -> str:
    """モデルフォールバックチェーン付きで Gemini に問い合わせ、テキストを返す。

    各モデルで最大2回試行する（1回はレート制限時のリトライ）。
    タイムアウト・404・廃止エラーは次のモデルへスキップ。
    全モデル失敗時は RuntimeError を送出。
    """
    last_error = None
    tried: list = []

    for model in GEMINI_MODEL_CHAIN:
        tried.append(model)
        retried = False
        for _ in range(2):
            try:
                return _call_model_once(client, model, contents)
            except RuntimeError as e:
                # タイムアウト → 次のモデルへ
                last_error = e
                break
            except Exception as e:
                err_str = str(e)
                last_error = e
                if _is_skip_model_error(err_str):
                    break  # このモデルはスキップ → 次へ
                if not _is_retryable_error(err_str):
                    raise  # 予期しないエラーはそのまま送出
                if not retried:
                    retried = True
                    time.sleep(_parse_retry_delay(err_str))
                else:
                    break  # 1回リトライ済み → 次のモデルへ

    raise RuntimeError(
        f"すべてのモデルで失敗（試行: {', '.join(tried)}）。"
        f"しばらく時間をおいて再試行してください。\n詳細: {last_error}"
    )


def call_gemini(image_data: tuple, prompt: str) -> str:
    """画像1枚 + プロンプトで Gemini を呼び出しテキストを返す"""
    image_bytes, mime_type = image_data
    contents = [types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt]
    return _generate_with_chain(_make_client(), contents)

# ──────────────────────────────────────────────────────────────
# JSON 抽出・パース
# ──────────────────────────────────────────────────────────────
def _sanitize_json_string(s: str) -> str:
    """JSON文字列値内の未エスケープ制御文字（改行・タブ等）をエスケープする"""
    result = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            if ch == "\n":
                result.append("\\n")
                continue
            if ch == "\r":
                result.append("\\r")
                continue
            if ch == "\t":
                result.append("\\t")
                continue
        result.append(ch)
    return "".join(result)


def _remove_trailing_commas(s: str) -> str:
    """JSON末尾カンマを除去する（例: [1, 2,] → [1, 2]）"""
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _parse_json(raw: str) -> dict:
    """json.loads を段階的に修復しながら試みる"""
    last_err = None
    for attempt in (
        raw,
        _remove_trailing_commas(raw),
        _remove_trailing_commas(_sanitize_json_string(raw)),
    ):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError as e:
            last_err = e
    raise last_err


def extract_json(text: str) -> dict:
    """Geminiレスポンスから JSON を抽出してパースする。

    抽出パターン（優先順）:
    ① <<<JSON_START>>>...<<<JSON_END>>> マーカー
    ② ```json ... ``` コードブロック
    ③ テキスト全体から { ... } を抽出（フォールバック）
    """
    # ① マーカーあり（通常ケース）
    m = re.search(r"<<<JSON_START>>>(.*?)<<<JSON_END>>>", text, re.DOTALL)
    if m:
        return _parse_json(m.group(1).strip())

    # ② コードブロック
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return _parse_json(m.group(1).strip())

    # ③ 生JSON フォールバック
    m = re.search(r"(\{.*\})", text, re.DOTALL)
    if m:
        try:
            return _parse_json(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"JSONを抽出できませんでした。Gemini応答:\n{text[:500]}")

# ──────────────────────────────────────────────────────────────
# 画像・動画解析
# ──────────────────────────────────────────────────────────────
def analyze_summary(image_data: tuple) -> dict:
    """帳尻合わせ画面（サマリー）を解析する"""
    return extract_json(call_gemini(image_data, SUMMARY_PROMPT))


def analyze_detail(image_data: tuple) -> dict:
    """食事詳細画像1枚を解析する"""
    return extract_json(call_gemini(image_data, DETAIL_PROMPT))


# 15 MB 以下の動画はインライン送信（Files API アップロード＋ポーリングをスキップ）
# Gemini の inline data 上限は 20 MB。余裕を持って 15 MB を境界とする。
_INLINE_VIDEO_LIMIT = 15 * 1024 * 1024  # 15 MB


def _build_video_contents(client: genai.Client, video_path: str, mime_type: str):
    """動画サイズに応じてコンテンツリストを返す。

    ≤ 15 MB → Part.from_bytes でインライン送信（Files API 不要 → 大幅に速い）
    > 15 MB → Files API upload → PROCESSING 完了待ち → file object で送信
    戻り値: (contents_list, video_file_or_None)
             video_file_or_None は後で delete するために返す（インライン時は None）
    """
    file_size = os.path.getsize(video_path)

    if file_size <= _INLINE_VIDEO_LIMIT:
        # ── インライン送信（Upload + Polling の2〜3分を完全スキップ）
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        contents = [
            types.Part.from_bytes(data=video_bytes, mime_type=mime_type),
            VIDEO_PROMPT,
        ]
        return contents, None

    # ── Files API 経由（15 MB 超の大きい動画）
    video_file = client.files.upload(file=video_path)
    # 処理完了を待機（最大 90 秒、1秒おきにポーリング）
    for _ in range(90):
        if "PROCESSING" not in str(video_file.state):
            break
        time.sleep(1)
        video_file = client.files.get(name=video_file.name)
    if "FAILED" in str(video_file.state):
        raise RuntimeError("動画のGemini処理に失敗しました。別の動画で試してください。")
    return [video_file, VIDEO_PROMPT], video_file


def analyze_video(video_path: str, mime_type: str, override_date: str = "") -> dict:
    """動画ファイルを Gemini で解析して merge_results 済み dict を返す。

    ≤ 15 MB: インライン送信（Files API スキップ）で高速化
    > 15 MB: Files API upload → polling → generate_content
    """
    client = _make_client()
    video_file = None
    try:
        contents, video_file = _build_video_contents(client, video_path, mime_type)
        response_text = _generate_with_chain(client, contents)
        raw = extract_json(response_text)

        summary = {
            "date":            raw.get("date", ""),
            "weight":          raw.get("weight"),
            "sleep":           raw.get("sleep", ""),
            "total_kcal":      raw.get("total_kcal"),
            "total_P":         raw.get("total_P"),
            "total_F":         raw.get("total_F"),
            "total_C":         raw.get("total_C"),
            "self_evaluation": raw.get("self_evaluation", ""),
            "exercise_notes":  raw.get("exercise_notes", ""),
            "exercise":        raw.get("exercise", []),
        }
        detail_list = [{"meals": raw.get("meals", [])}]
        return merge_results(summary, detail_list, override_date=override_date)

    finally:
        if video_file:
            try:
                client.files.delete(name=video_file.name)
            except Exception:
                pass

# ──────────────────────────────────────────────────────────────
# 数値変換・表示ユーティリティ
# ──────────────────────────────────────────────────────────────
def _to_float(val, default: float = 0.0) -> float:
    """Gemini出力を安全に float へ変換する。None・単位付き文字列に対応。"""
    if val is None:
        return default
    try:
        cleaned = re.sub(r"[^\d.\-]", "", str(val))
        return float(cleaned) if cleaned else default
    except (ValueError, TypeError):
        return default


def _gs_val(val):
    """gspread に渡す値を None-safe にする（None → ""）"""
    return "" if val is None else val


def calc_actual_pfc(item: dict) -> tuple:
    """食品1つの実際の P/F/C（%適用後）を返す"""
    pct = _to_float(item.get("percentage"), 100) / 100
    P = round(_to_float(item.get("P")) * pct, 1)
    F = round(_to_float(item.get("F")) * pct, 1)
    C = round(_to_float(item.get("C")) * pct, 1)
    return P, F, C


def format_amount(base_amount, base_unit: str, percentage: float) -> str:
    """表示用の分量文字列を生成する"""
    if base_unit == "pct_only" or base_amount is None:
        return ""
    if base_unit in COUNT_UNITS:
        disp = int(base_amount) if float(base_amount) == int(float(base_amount)) else base_amount
    else:
        actual = float(base_amount) * percentage / 100
        disp = int(actual) if actual == int(actual) else round(actual, 1)
    return f"{disp}{base_unit}"


def sum_pfc(meals: list) -> dict:
    """食事リストから PFC を合計する"""
    P = F = C = 0.0
    for meal in meals:
        for item in meal.get("items", []):
            p, f, c = calc_actual_pfc(item)
            P += p
            F += f
            C += c
    return {"P": round(P, 1), "F": round(F, 1), "C": round(C, 1)}

# ──────────────────────────────────────────────────────────────
# 日付・週番号
# ──────────────────────────────────────────────────────────────
def parse_record_date(date_str: str) -> date:
    """'M/D' 形式の文字列を date オブジェクトに変換する"""
    parts = date_str.strip().split("/")
    month, day = int(parts[0]), int(parts[1])
    year = datetime.now().year
    candidate = date(year, month, day)
    # 半年以上ズレていれば前年と判断
    if abs((candidate - date.today()).days) > 180:
        candidate = date(year - 1, month, day)
    return candidate


def get_week_number(record_date: date) -> int:
    return (record_date - START_DATE).days // 7 + 1

# ──────────────────────────────────────────────────────────────
# 表示テキスト生成・データ統合
# ──────────────────────────────────────────────────────────────
def build_gemini_text(summary: dict, meal_map: dict, pfc_map: dict, ds_pfc: dict) -> str:
    """ユーザー向け表示テキストを構築する"""
    bf = pfc_map.get("朝食", {"P": 0, "F": 0, "C": 0})
    lu = pfc_map.get("昼食", {"P": 0, "F": 0, "C": 0})
    lines = [
        "【各食事のPFC合計】",
        f"朝食 [P:{bf['P']}g F:{bf['F']}g C:{bf['C']}g]",
        f"昼食 [P:{lu['P']}g F:{lu['F']}g C:{lu['C']}g]",
        f"夜・間食 [P:{ds_pfc['P']}g F:{ds_pfc['F']}g C:{ds_pfc['C']}g]",
    ]

    # 日付・睡眠・体重
    date_str = summary.get("date", "不明")
    try:
        d = parse_record_date(date_str)
        lines.append(f"{d.month}月{d.day}日({WEEKDAYS[d.weekday()]})")
    except Exception:
        lines.append(date_str)
    lines.append(f"睡眠 {summary.get('sleep') or '不明'}")
    lines.append(f"体重 {summary.get('weight', '不明')}kg (前日比 kg)")

    # 合計栄養素
    lines += [
        "【合計摂取栄養素】",
        f"カロリー： {summary.get('total_kcal', '不明')}kcal",
        f"P（たんぱく質）： {summary.get('total_P', '不明')}g",
        f"F（脂質）： {summary.get('total_F', '不明')}g",
        f"C（炭水化物）： {summary.get('total_C', '不明')}g",
    ]

    # 食事詳細（朝食→昼食→間食→夕食の順）
    counter = 1
    for meal_type in ["朝食", "昼食", "間食", "夕食"]:
        for meal in meal_map.get(meal_type, []):
            t = meal.get("time", "") or ""
            lines.append(f"{counter}回目 {meal_type}{'（' + t + '）' if t else ''}")
            for item in meal.get("items", []):
                amt = format_amount(
                    item.get("base_amount"),
                    item.get("base_unit", ""),
                    _to_float(item.get("percentage"), 100),
                )
                lines.append(f"{item['name']} {amt}".strip())
            counter += 1

    lines += [
        "【自己評価】",
        summary.get("self_evaluation") or "不明",
        "【運動】",
        summary.get("exercise_notes") or "不明",
    ]
    return "\n".join(lines)


def merge_results(summary: dict, detail_list: list, override_date: str = "") -> dict:
    """サマリーと詳細を統合してスプレッドシート書き込み用 dict を返す。

    override_date が指定された場合、AIが読んだ日付より優先して使用する。
    表示テキスト（_gemini_text）にも正しい日付が反映される。
    """
    # 食事種別ごとにまとめる
    meal_map: dict = {}
    for detail in detail_list:
        for meal in detail.get("meals", []):
            mt = meal.get("type", "不明")
            meal_map.setdefault(mt, []).append(meal)

    pfc_map = {mt: sum_pfc(meals) for mt, meals in meal_map.items()}

    # 夜・間食を逆算（合計 - 朝食 - 昼食）
    total_P = _to_float(summary.get("total_P"))
    total_F = _to_float(summary.get("total_F"))
    total_C = _to_float(summary.get("total_C"))
    bf = pfc_map.get("朝食", {"P": 0, "F": 0, "C": 0})
    lu = pfc_map.get("昼食", {"P": 0, "F": 0, "C": 0})
    ds_pfc = {
        "P": round(total_P - bf["P"] - lu["P"], 1),
        "F": round(total_F - bf["F"] - lu["F"], 1),
        "C": round(total_C - bf["C"] - lu["C"], 1),
    }

    # override_date をテキスト生成前に適用することで事後パッチ不要
    effective_summary = dict(summary)
    if override_date:
        effective_summary["date"] = override_date

    gemini_text = build_gemini_text(effective_summary, meal_map, pfc_map, ds_pfc)

    # exercise は必ずリストに正規化
    exercises = summary.get("exercise") or []
    if not isinstance(exercises, list):
        exercises = []

    return {
        "date":         override_date or summary.get("date") or "",
        "weight":       _to_float(summary.get("weight")) or "",
        "total_kcal":   _to_float(summary.get("total_kcal")) or "",
        "total_P":      total_P,
        "total_F":      total_F,
        "total_C":      total_C,
        "breakfast":    {"P": bf["P"], "F": bf["F"], "C": bf["C"], "kcal": 0},
        "lunch":        {"P": lu["P"], "F": lu["F"], "C": lu["C"], "kcal": 0},
        "dinner_snack": ds_pfc,
        "exercise":     exercises,
        "_gemini_text": gemini_text,
    }

# ──────────────────────────────────────────────────────────────
# スプレッドシート書き込み
# ──────────────────────────────────────────────────────────────
def write_to_spreadsheet(data: dict) -> str:
    """data をスプレッドシートに書き込み、タブ名を返す"""
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")

    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    gc = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(sa_info, scope))
    spreadsheet = gc.open_by_url(SPREADSHEET_URL)

    record_date = parse_record_date(data["date"])
    tab_name = f"{get_week_number(record_date)}週目"

    try:
        worksheet = spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        raise ValueError(f"タブ「{tab_name}」が見つかりません。スプレッドシートに追加してください。")

    date_str = data["date"]
    q_col = worksheet.col_values(17)
    row_n = next(
        (i for i, v in enumerate(q_col, start=1) if v.strip() == date_str),
        None,
    )
    if row_n is None:
        raise ValueError(f"タブ「{tab_name}」のQ列に「{date_str}」が見つかりませんでした。")

    def col(letter: str, row: int) -> str:
        return f"{letter}{row}"

    bf = data.get("breakfast")    or {}
    lu = data.get("lunch")        or {}
    ds = data.get("dinner_snack") or {}

    updates = [
        {"range": col("Q", row_n),     "values": [[_gs_val(date_str)]]},
        {"range": col("S", row_n),     "values": [[_gs_val(data.get("weight"))]]},
        {"range": col("S", row_n + 3), "values": [[_gs_val(bf.get("P"))]]},
        {"range": col("U", row_n + 3), "values": [[_gs_val(bf.get("F"))]]},
        {"range": col("W", row_n + 3), "values": [[_gs_val(bf.get("C"))]]},
        {"range": col("S", row_n + 4), "values": [[_gs_val(lu.get("P"))]]},
        {"range": col("U", row_n + 4), "values": [[_gs_val(lu.get("F"))]]},
        {"range": col("W", row_n + 4), "values": [[_gs_val(lu.get("C"))]]},
        {"range": col("S", row_n + 5), "values": [[_gs_val(ds.get("P"))]]},
        {"range": col("U", row_n + 5), "values": [[_gs_val(ds.get("F"))]]},
        {"range": col("W", row_n + 5), "values": [[_gs_val(ds.get("C"))]]},
    ]

    exercises = data.get("exercise") or []
    if isinstance(exercises, list):
        for idx, ex in enumerate(exercises):
            r = row_n + 2 + idx
            updates += [
                {"range": col("Z",  r), "values": [[_gs_val(ex.get("menu"))]]},
                {"range": col("AA", r), "values": [[_gs_val(ex.get("reps"))]]},
                {"range": col("AB", r), "values": [[_gs_val(ex.get("sets"))]]},
            ]

    worksheet.batch_update(updates)
    return tab_name

# ──────────────────────────────────────────────────────────────
# レスポンス生成ヘルパー
# ──────────────────────────────────────────────────────────────
def _success_response(data: dict, tab_name: str):
    return jsonify({
        "success":     True,
        "tab":         tab_name,
        "date":        data.get("date"),
        "weight":      data.get("weight"),
        "gemini_text": data.get("_gemini_text", ""),
        "data":        {k: v for k, v in data.items() if k != "_gemini_text"},
    })


def _error_response(msg: str, data: dict = None, status: int = 500):
    payload = {"success": False, "error": msg}
    if data:
        payload["gemini_text"] = data.get("_gemini_text", "")
        payload["data"] = {k: v for k, v in data.items() if k != "_gemini_text"}
    return jsonify(payload), status

# ──────────────────────────────────────────────────────────────
# ルート
# ──────────────────────────────────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"success": False, "error": f"サーバーエラー: {str(e)}"}), 500


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"success": False, "error": "ページが見つかりません。"}), 404


@app.route("/")
@basic_auth_required
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
@basic_auth_required
def upload():
    files = [f for f in (request.files.getlist("images") or request.files.getlist("image"))
             if f and f.filename]
    user_date = request.form.get("date", "").strip()

    if not files:
        return _error_response("画像ファイルが選択されていません。", status=400)
    if not GEMINI_API_KEY:
        return _error_response("GEMINI_API_KEY が設定されていません。")

    image_list = [(f.read(), f.content_type or "image/jpeg") for f in files]

    try:
        summary  = analyze_summary(image_list[0])
        details  = [analyze_detail(img) for img in image_list[1:]]
        data     = merge_results(summary, details, override_date=user_date)
    except Exception as e:
        return _error_response(f"AI解析エラー: {e}")

    try:
        tab_name = write_to_spreadsheet(data)
    except Exception as e:
        return _error_response(f"スプレッドシート書き込みエラー: {e}", data=data)

    return _success_response(data, tab_name)


@app.route("/upload-video", methods=["POST"])
@basic_auth_required
def upload_video():
    """動画ファイルを受け取り、バックグラウンドで処理して job_id を即返す。

    クライアントは /job-status/<job_id> をポーリングして結果を取得する。
    これにより、Gunicorn の worker timeout / TCP 切断の問題を完全に回避する。
    """
    f = request.files.get("video")
    user_date = request.form.get("date", "").strip()

    if not f or not f.filename:
        return _error_response("動画ファイルが選択されていません。", status=400)
    if not GEMINI_API_KEY:
        return _error_response("GEMINI_API_KEY が設定されていません。")

    # ディスクへ直接ストリーム保存（OOM対策）
    mime_type = f.content_type or "video/mp4"
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        f.save(tmp)
        tmp_path = tmp.name

    job_id = str(uuid.uuid4())
    _job_set(job_id, {"status": "processing"})

    t = threading.Thread(
        target=_run_video_job,
        args=(job_id, tmp_path, mime_type, user_date),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "status": "processing"})


@app.route("/job-status/<job_id>")
@basic_auth_required
def job_status(job_id: str):
    """ジョブの現在状態を返す。

    status: "processing" → まだ実行中
    status: "done"       → 完了（success=True/False を確認）
    """
    job = _job_get(job_id)
    if job is None:
        return jsonify({"status": "not_found", "success": False,
                        "error": "ジョブが見つかりません。"}), 404
    return jsonify(job)


@app.route("/test-connection")
@basic_auth_required
def test_connection():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return jsonify({"success": False, "error": "GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。"}), 500
    try:
        sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    except Exception as e:
        return jsonify({"success": False, "error": f"JSON解析エラー: {e}"}), 500

    result = {
        "service_account_email": sa_info.get("client_email", "取得できませんでした"),
        "gemini_api_key_set":    bool(GEMINI_API_KEY),
    }
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        gc = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(sa_info, scope))
        spreadsheet = gc.open_by_url(SPREADSHEET_URL)
        result["success"]           = True
        result["spreadsheet_title"] = spreadsheet.title
        result["tabs"]              = [ws.title for ws in spreadsheet.worksheets()]
    except gspread.exceptions.APIError as e:
        result["success"]           = False
        result["spreadsheet_error"] = f"APIError: {e.response.status_code} - {e.response.json()}"
    except Exception as e:
        import traceback
        result["success"]           = False
        result["spreadsheet_error"] = traceback.format_exc()

    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
