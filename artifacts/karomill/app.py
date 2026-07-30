"""
カロミル食事スクショ → Googleスプレッドシート自動記録アプリ
"""
import os
import json
import re
import time
import tempfile
from datetime import datetime, date

from google import genai
from google.genai import types
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, render_template, request, jsonify, Response
from functools import wraps

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────
# Basic認証
# ─────────────────────────────────────────────
BASIC_AUTH_USERNAME = os.environ.get("BASIC_AUTH_USERNAME", "")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")

def check_auth(username: str, password: str) -> bool:
    return username == BASIC_AUTH_USERNAME and password == BASIC_AUTH_PASSWORD

def require_auth():
    return Response(
        "認証が必要です。ユーザー名とパスワードを入力してください。",
        401,
        {"WWW-Authenticate": 'Basic realm="Karomill"'},
    )

def basic_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return require_auth()
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────
# Gemini設定
# ─────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

_default_first = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_MODEL_CHAIN = [
    _default_first,
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash-001",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
]
seen: set = set()
GEMINI_MODEL_CHAIN = [m for m in GEMINI_MODEL_CHAIN if not (m in seen or seen.add(m))]

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1z9PX6D_zbd1fhDDATzOOZNlLd3Ux1eMchoDBrMi7qCA/edit"
START_DATE = date(2026, 3, 31)

# ─────────────────────────────────────────────
# Gemini プロンプト（サマリー用・詳細用に分割）
# ─────────────────────────────────────────────

SUMMARY_PROMPT = """これはカロミルアプリの「帳尻合わせ」画面のスクリーンショットです。
画像から以下の情報を正確に抽出し、下記JSONのみを出力してください。説明文は一切不要です。

【重要な抽出ルール】
・日付：画面上部の「昨日 M/D (曜日)」または「M月D日(曜日)」から「M/D」形式で抽出
・体重：「栄養サマリー」カードの下部左側に単独で表示されている小数点1桁の数値（例：76.2）が体重(kg)。
　　　　「/」の右側の目標値ではなく必ず左側の実績値を読み取ること。絶対に見落とさないこと。
・睡眠：カード内の「HH:MM〜HH:MM」形式の時刻をそのまま抽出（なければ空文字）
・合計栄養素：「カロリー ◯/◯kcal」の左の実績値を total_kcal に。たんぱく質・脂質・炭水化物も左の実績値のみ
・下部の「P:31/24%」などのパーセンテージはグラム数ではないため total_P 等に絶対使用しないこと
・自己評価・運動メモ：画面内に記載があれば抽出、なければ空文字
・exercise配列：運動メモから種目・回数・セット数を構造化。読み取れない場合は空配列

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

【サマリー抽出ルール（帳尻合わせ画面）】
・日付：画面上部の「昨日 M/D (曜日)」または「M月D日(曜日)」から「M/D」形式で抽出
・体重：「栄養サマリー」カードの下部左側に単独で表示されている小数点1桁の数値（例：76.2）が体重(kg)。
　　　　「/」の右側の目標値ではなく必ず左側の実績値を読み取ること。
・睡眠：カード内の「HH:MM〜HH:MM」形式の時刻をそのまま抽出
・合計栄養素：「カロリー ◯/◯kcal」の左の実績値を total_kcal に。たんぱく質・脂質・炭水化物も左の実績値のみ
・下部の「P:31/24%」などのパーセンテージはグラム数ではないため total_P 等に絶対使用しないこと

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


# ─────────────────────────────────────────────
# Gemini APIヘルパー
# ─────────────────────────────────────────────

def _is_skip_model_error(err_str: str) -> bool:
    if "404" in err_str or "NOT_FOUND" in err_str or "no longer available" in err_str:
        return True
    if "PerDay" in err_str or "limit: 0" in err_str:
        return True
    return False


def _parse_retry_delay(err_str: str) -> int:
    m = re.search(r"retry[_ ]?(?:in|delay)[^\d]*(\d+)", err_str, re.IGNORECASE)
    return int(m.group(1)) + 2 if m else 60


def call_gemini(image_data: tuple, prompt: str) -> str:
    """1枚の画像と指定プロンプトでGeminiを呼び出しテキストを返す。フォールバックチェーン付き。"""
    image_bytes, mime_type = image_data
    client = genai.Client(api_key=GEMINI_API_KEY)
    parts = [types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt]

    last_error = None
    tried_models = []

    for model in GEMINI_MODEL_CHAIN:
        tried_models.append(model)
        per_minute_retried = False

        for _ in range(2):
            try:
                response = client.models.generate_content(model=model, contents=parts)
                return response.text
            except Exception as e:
                err_str = str(e)
                last_error = e
                is_quota = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                if not is_quota:
                    raise
                if _is_skip_model_error(err_str):
                    break
                if not per_minute_retried:
                    per_minute_retried = True
                    time.sleep(_parse_retry_delay(err_str))
                    continue
                else:
                    break

    tried = ", ".join(tried_models)
    raise RuntimeError(
        f"すべてのモデルでクォータが枯渇（試行: {tried}）。しばらく時間をおいて再試行してください。\n詳細: {last_error}"
    )


def extract_json(text: str) -> dict:
    """<<<JSON_START>>>...<<<JSON_END>>> からJSONを抽出してdictを返す"""
    m = re.search(r"<<<JSON_START>>>(.*?)<<<JSON_END>>>", text, re.DOTALL)
    if not m:
        raise ValueError(f"JSONを抽出できませんでした。Gemini応答:\n{text[:500]}")
    return json.loads(m.group(1).strip())


def analyze_summary(image_data: tuple) -> dict:
    """サマリー画像（帳尻合わせ）を解析"""
    return extract_json(call_gemini(image_data, SUMMARY_PROMPT))


def analyze_detail(image_data: tuple) -> dict:
    """食事詳細画像1枚を解析"""
    return extract_json(call_gemini(image_data, DETAIL_PROMPT))


def analyze_video(video_bytes: bytes, mime_type: str) -> dict:
    """MP4動画をGemini Files APIで解析してmerge_results済みdictを返す"""
    client = genai.Client(api_key=GEMINI_API_KEY)

    # 一時ファイルに書き出してアップロード
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(video_bytes)
        tmp_path = f.name

    video_file = None
    try:
        video_file = client.files.upload(file=tmp_path)

        # 処理完了を待機（最大90秒）
        waited = 0
        while waited < 90:
            state_str = str(video_file.state)
            if "PROCESSING" not in state_str:
                break
            time.sleep(3)
            waited += 3
            video_file = client.files.get(name=video_file.name)

        if "FAILED" in str(video_file.state):
            raise RuntimeError("動画のGemini処理に失敗しました。別の動画で試してください。")

        response = client.models.generate_content(
            model=GEMINI_MODEL_CHAIN[0],
            contents=[video_file, VIDEO_PROMPT],
        )
        raw = extract_json(response.text)

        # サマリー部分とメール詳細部分に分割してmerge_resultsへ渡す
        summary = {
            "date": raw.get("date", ""),
            "weight": raw.get("weight"),
            "sleep": raw.get("sleep", ""),
            "total_kcal": raw.get("total_kcal"),
            "total_P": raw.get("total_P"),
            "total_F": raw.get("total_F"),
            "total_C": raw.get("total_C"),
            "self_evaluation": raw.get("self_evaluation", ""),
            "exercise_notes": raw.get("exercise_notes", ""),
            "exercise": raw.get("exercise", []),
        }
        detail_list = [{"meals": raw.get("meals", [])}]
        return merge_results(summary, detail_list)

    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        if video_file:
            try:
                client.files.delete(name=video_file.name)
            except Exception:
                pass


# ─────────────────────────────────────────────
# PFC計算・表示テキスト生成
# ─────────────────────────────────────────────

# 個数系の単位（%を摂取量に適用しない）
COUNT_UNITS = {"個", "本", "枚", "株", "杯", "缶", "食", "袋", "切", "片"}
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def calc_actual_pfc(item: dict) -> tuple:
    """食品1つの実際のP/F/C（%適用後）を返す"""
    pct = float(item.get("percentage") or 100) / 100
    P = round(float(item.get("P") or 0) * pct, 1)
    F = round(float(item.get("F") or 0) * pct, 1)
    C = round(float(item.get("C") or 0) * pct, 1)
    return P, F, C


def format_amount(base_amount, base_unit: str, percentage: float) -> str:
    """表示用の分量文字列を生成"""
    if base_unit == "pct_only" or base_amount is None:
        # 基準量なし → グラム計算不可のため表示なし
        return ""
    if base_unit in COUNT_UNITS:
        amt = int(base_amount) if float(base_amount) == int(float(base_amount)) else base_amount
        return f"{amt}{base_unit}"
    else:
        # 基準量 × % = 実際の摂取量
        actual = float(base_amount) * percentage / 100
        amt = int(actual) if actual == int(actual) else round(actual, 1)
        return f"{amt}{base_unit}"


def sum_pfc(meals: list) -> dict:
    """mealリストからPFCを合計"""
    P, F, C = 0.0, 0.0, 0.0
    for meal in meals:
        for item in meal.get("items", []):
            p, f, c = calc_actual_pfc(item)
            P += p; F += f; C += c
    return {"P": round(P, 1), "F": round(F, 1), "C": round(C, 1)}


def build_gemini_text(summary: dict, meal_map: dict, pfc_map: dict, ds_pfc: dict) -> str:
    """ユーザー向け表示テキストを構築"""
    lines = []

    # PFC合計
    bf = pfc_map.get("朝食", {"P": 0, "F": 0, "C": 0})
    lu = pfc_map.get("昼食", {"P": 0, "F": 0, "C": 0})
    lines.append("【各食事のPFC合計】")
    lines.append(f"朝食 [P:{bf['P']}g F:{bf['F']}g C:{bf['C']}g]")
    lines.append(f"昼食 [P:{lu['P']}g F:{lu['F']}g C:{lu['C']}g]")
    lines.append(f"夜・間食 [P:{ds_pfc['P']}g F:{ds_pfc['F']}g C:{ds_pfc['C']}g]")

    # 日付・睡眠・体重
    date_str = summary.get("date", "不明")
    try:
        d = parse_record_date(date_str)
        lines.append(f"{d.month}月{d.day}日({WEEKDAYS[d.weekday()]})")
    except Exception:
        lines.append(date_str)

    lines.append(f"睡眠 {summary.get('sleep') or '不明'}")
    w = summary.get("weight", "不明")
    lines.append(f"体重 {w}kg (前日比 kg)")

    # 合計摂取栄養素
    lines.append("【合計摂取栄養素】")
    lines.append(f"カロリー： {summary.get('total_kcal', '不明')}kcal")
    lines.append(f"P（たんぱく質）： {summary.get('total_P', '不明')}g")
    lines.append(f"F（脂質）： {summary.get('total_F', '不明')}g")
    lines.append(f"C（炭水化物）： {summary.get('total_C', '不明')}g")

    # 食事詳細（朝食→昼食→間食→夕食の順）
    counter = 1
    for meal_type in ["朝食", "昼食", "間食", "夕食"]:
        for meal in meal_map.get(meal_type, []):
            t = meal.get("time", "") or ""
            time_part = f"（{t}）" if t else ""
            lines.append(f"{counter}回目 {meal_type}{time_part}")
            for item in meal.get("items", []):
                amt = format_amount(
                    item.get("base_amount"),
                    item.get("base_unit", ""),
                    float(item.get("percentage") or 100),
                )
                lines.append(f"{item['name']} {amt}".strip())
            counter += 1

    # 自己評価・運動
    lines.append("【自己評価】")
    lines.append(summary.get("self_evaluation") or "不明")
    lines.append("【運動】")
    lines.append(summary.get("exercise_notes") or "不明")

    return "\n".join(lines)


def merge_results(summary: dict, detail_list: list) -> dict:
    """サマリーと詳細を統合してスプレッドシート書き込み用dictを返す"""
    # 食事種別ごとにまとめる
    meal_map: dict = {}
    for detail in detail_list:
        for meal in detail.get("meals", []):
            mt = meal.get("type", "不明")
            meal_map.setdefault(mt, []).append(meal)

    # PFCをPythonで計算（食事種別ごと）
    pfc_map = {mt: sum_pfc(meals) for mt, meals in meal_map.items()}

    # 夜・間食を逆算（合計 - 朝食 - 昼食）
    total_P = float(summary.get("total_P") or 0)
    total_F = float(summary.get("total_F") or 0)
    total_C = float(summary.get("total_C") or 0)
    bf = pfc_map.get("朝食", {"P": 0, "F": 0, "C": 0})
    lu = pfc_map.get("昼食", {"P": 0, "F": 0, "C": 0})
    ds_pfc = {
        "P": round(total_P - bf["P"] - lu["P"], 1),
        "F": round(total_F - bf["F"] - lu["F"], 1),
        "C": round(total_C - bf["C"] - lu["C"], 1),
    }

    gemini_text = build_gemini_text(summary, meal_map, pfc_map, ds_pfc)

    return {
        "date": summary.get("date", ""),
        "weight": summary.get("weight", ""),
        "total_kcal": summary.get("total_kcal"),
        "total_P": total_P,
        "total_F": total_F,
        "total_C": total_C,
        "breakfast": {"P": bf["P"], "F": bf["F"], "C": bf["C"], "kcal": 0},
        "lunch": {"P": lu["P"], "F": lu["F"], "C": lu["C"], "kcal": 0},
        "dinner_snack": ds_pfc,
        "exercise": summary.get("exercise", []),
        "_gemini_text": gemini_text,
    }


# ─────────────────────────────────────────────
# スプレッドシート書き込み
# ─────────────────────────────────────────────

def get_week_number(record_date: date) -> int:
    delta = (record_date - START_DATE).days
    return delta // 7 + 1


def parse_record_date(date_str: str) -> date:
    parts = date_str.strip().split("/")
    month = int(parts[0])
    day = int(parts[1])
    year = datetime.now().year
    today = date.today()
    candidate = date(year, month, day)
    if abs((candidate - today).days) > 180:
        candidate = date(year - 1, month, day)
    return candidate


def write_to_spreadsheet(data: dict) -> str:
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")

    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(sa_info, scope)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_url(SPREADSHEET_URL)

    record_date = parse_record_date(data["date"])
    week_num = get_week_number(record_date)
    tab_name = f"{week_num}週目"

    try:
        worksheet = spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        raise ValueError(f"タブ「{tab_name}」が見つかりません。スプレッドシートに追加してください。")

    date_str = data["date"]
    q_col_values = worksheet.col_values(17)

    row_n = None
    for i, cell_val in enumerate(q_col_values, start=1):
        if cell_val.strip() == date_str:
            row_n = i
            break

    if row_n is None:
        raise ValueError(f"タブ「{tab_name}」のQ列に「{date_str}」が見つかりませんでした。")

    def cell(col_letter: str, row: int) -> str:
        return f"{col_letter}{row}"

    updates = []
    updates.append({"range": cell("Q", row_n), "values": [[date_str]]})
    updates.append({"range": cell("S", row_n), "values": [[data.get("weight", "")]]})

    bf = data.get("breakfast", {})
    updates.append({"range": cell("S", row_n + 3), "values": [[bf.get("P", "")]]})
    updates.append({"range": cell("U", row_n + 3), "values": [[bf.get("F", "")]]})
    updates.append({"range": cell("W", row_n + 3), "values": [[bf.get("C", "")]]})

    lu = data.get("lunch", {})
    updates.append({"range": cell("S", row_n + 4), "values": [[lu.get("P", "")]]})
    updates.append({"range": cell("U", row_n + 4), "values": [[lu.get("F", "")]]})
    updates.append({"range": cell("W", row_n + 4), "values": [[lu.get("C", "")]]})

    ds = data.get("dinner_snack", {})
    updates.append({"range": cell("S", row_n + 5), "values": [[ds.get("P", "")]]})
    updates.append({"range": cell("U", row_n + 5), "values": [[ds.get("F", "")]]})
    updates.append({"range": cell("W", row_n + 5), "values": [[ds.get("C", "")]]})

    exercises = data.get("exercise", [])
    for idx, ex in enumerate(exercises):
        r = row_n + 2 + idx
        updates.append({"range": cell("Z", r), "values": [[ex.get("menu", "")]]})
        updates.append({"range": cell("AA", r), "values": [[ex.get("reps", "")]]})
        updates.append({"range": cell("AB", r), "values": [[ex.get("sets", "")]]})

    worksheet.batch_update(updates)
    return tab_name


# ─────────────────────────────────────────────
# ルート
# ─────────────────────────────────────────────

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
    files = request.files.getlist("images") or request.files.getlist("image")
    files = [f for f in files if f and f.filename != ""]

    if not files:
        return jsonify({"success": False, "error": "画像ファイルが選択されていません。"}), 400
    if not GEMINI_API_KEY:
        return jsonify({"success": False, "error": "GEMINI_API_KEY が設定されていません。"}), 500

    image_list = [(f.read(), f.content_type or "image/jpeg") for f in files]

    try:
        if len(image_list) == 1:
            # 1枚のみ：サマリー画像として処理（詳細なし）
            summary = analyze_summary(image_list[0])
            data = merge_results(summary, [])
        else:
            # 1枚目：サマリー、2枚目以降：食事詳細を1枚ずつ個別解析
            summary = analyze_summary(image_list[0])
            detail_list = [analyze_detail(img) for img in image_list[1:]]
            data = merge_results(summary, detail_list)
    except Exception as e:
        return jsonify({"success": False, "error": f"AI解析エラー: {str(e)}"}), 500

    try:
        tab_name = write_to_spreadsheet(data)
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"スプレッドシート書き込みエラー: {str(e)}",
            "gemini_text": data.get("_gemini_text", ""),
            "data": {k: v for k, v in data.items() if k != "_gemini_text"},
        }), 500

    return jsonify({
        "success": True,
        "tab": tab_name,
        "date": data.get("date"),
        "weight": data.get("weight"),
        "gemini_text": data.get("_gemini_text", ""),
        "data": {k: v for k, v in data.items() if k != "_gemini_text"},
    })


@app.route("/upload-video", methods=["POST"])
@basic_auth_required
def upload_video():
    f = request.files.get("video")
    if not f or f.filename == "":
        return jsonify({"success": False, "error": "動画ファイルが選択されていません。"}), 400
    if not GEMINI_API_KEY:
        return jsonify({"success": False, "error": "GEMINI_API_KEY が設定されていません。"}), 500

    try:
        data = analyze_video(f.read(), f.content_type or "video/mp4")
    except Exception as e:
        return jsonify({"success": False, "error": f"AI解析エラー: {str(e)}"}), 500

    try:
        tab_name = write_to_spreadsheet(data)
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"スプレッドシート書き込みエラー: {str(e)}",
            "gemini_text": data.get("_gemini_text", ""),
            "data": {k: v for k, v in data.items() if k != "_gemini_text"},
        }), 500

    return jsonify({
        "success": True,
        "tab": tab_name,
        "date": data.get("date"),
        "weight": data.get("weight"),
        "gemini_text": data.get("_gemini_text", ""),
        "data": {k: v for k, v in data.items() if k != "_gemini_text"},
    })


@app.route("/test-connection")
@basic_auth_required
def test_connection():
    result = {}
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return jsonify({"success": False, "error": "GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。"}), 500
    try:
        sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        result["service_account_email"] = sa_info.get("client_email", "取得できませんでした")
    except Exception as e:
        return jsonify({"success": False, "error": f"JSON解析エラー: {e}"}), 500

    result["gemini_api_key_set"] = bool(GEMINI_API_KEY)

    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(sa_info, scope)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_url(SPREADSHEET_URL)
        result["success"] = True
        result["spreadsheet_title"] = spreadsheet.title
        result["tabs"] = [ws.title for ws in spreadsheet.worksheets()]
    except gspread.exceptions.APIError as e:
        result["success"] = False
        result["spreadsheet_error"] = f"APIError: {e.response.status_code} - {e.response.json()}"
    except Exception as e:
        import traceback
        result["success"] = False
        result["spreadsheet_error"] = traceback.format_exc()

    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
