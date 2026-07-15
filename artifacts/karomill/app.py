"""
カロミル食事スクショ → Googleスプレッドシート自動記録アプリ
"""
import os
import json
import re
import base64
from datetime import datetime, timedelta, date

from google import genai
from google.genai import types
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, render_template, request, jsonify

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
app = Flask(__name__)

# Gemini APIキー（Replit Secrets: GEMINI_API_KEY）
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# モデルフォールバックチェーン（先頭から順に試す）
# 無料枠のクォータはモデルごと独立 → 1つが枯渇しても次へ自動切り替え
# GEMINI_MODEL 環境変数でカスタム先頭モデルを指定可能
_default_first = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_MODEL_CHAIN = [
    _default_first,
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]
# 重複除去（先頭を維持）
seen = set()
GEMINI_MODEL_CHAIN = [m for m in GEMINI_MODEL_CHAIN if not (m in seen or seen.add(m))]

# Google サービスアカウント JSON の内容（Replit Secrets: GOOGLE_SERVICE_ACCOUNT_JSON）
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# 対象スプレッドシートURL
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1z9PX6D_zbd1fhDDATzOOZNlLd3Ux1eMchoDBrMi7qCA/edit"

# 1週目の開始月曜日（必要に応じてここを書き換えてください）
START_DATE = date(2026, 3, 31)  # 2週目が4/7のため、1週目開始=3/31

# ─────────────────────────────────────────────
# Gemini プロンプト
# ─────────────────────────────────────────────
GEMINI_PROMPT = """以下の処理手順（①〜⑥）に従って情報を処理し、最終的に「出力フォーマット」に完全一致する形で結果のみを返却してください。それ以外の内部計算の過程や余計な説明は一切出力しないでください。

【処理手順】
① OCR抽出（最優先）
・画像内のテキストを一言一句抽出する。
・※食物繊維や塩分の欄の下部にあるメモ書き（睡眠時間、体重、運動内容など）も漏らさず正確に抽出すること。
・推測・補完は禁止。不明箇所は「不明」と記載する。
② 抽出内容の整理
・以下を箇条書きで列挙：食品名、表示されている数値（g・kcal・mlなど）、表示されている％
・※この段階では計算しない。
③ 数値計算
・「総重量 × ％」で実際の摂取量を算出（例：150g（50%）→ 75g）。
・最終出力の分量欄には、計算後の「実際に摂取した総量（数値と単位のみ）」を記載すること。
・「小さじ1杯4.6g」「大さじ1」「◯%」といった計算前の表記や、テキストの重複表記はすべて排除し、最終的な実摂取量（g、ml、個など）のみに集約すること。根拠が不明な場合は「算出不可」。
④ メニュー整形
・「食品名 分量」の形式に統一する。
・ブランド名・販売元（例：by Amazon、みなさまのお墨付き など）を削除すること。
・食材名に含まれる不要な修飾語・シリーズ名・原産地・パッケージ形状（例：「毎日」「ENRGY BOOSTER」「イタリア産5種の」「国産」「リーフパック」など）は削除すること。
・ただし、食材の重要な特徴である「味の種類（例：ストロベリー風味、カフェラテ味）」や、「食材の状態（例：生、ゆで、乾、根・皮あり）」に関する記述は絶対に削除せず、そのまま残すこと。
・一般的な単語（「オイル」「ごはん」「チキン」など）へ過剰に丸める（省略する）ことは禁止。
・単位は可能な限りそのまま使用（g / ml / 個 / 本 / 枚 など）。
⑤ 栄養計算
・カロリー（kcal）、P（たんぱく質）、F（脂質）、C（炭水化物）を合計。不明は「不明」。
⑥ 各食事ごとのPFC合計計算（※1枚目の画像内の各食事のPFC数値と一致させること）
・朝、昼、夜および間食のPFCをそれぞれ計算する。
・夜と間食のPFC数値は合算し、1つの結果としてまとめる。

【出力フォーマット】
【各食事のPFC合計】
朝食 [P:◯g F:◯g C:◯g]
昼食 [P:◯g F:◯g C:◯g]
夜・間食 [P:◯g F:◯g C:◯g]
M月D日(曜日)
睡眠 ◯◯:◯◯～◯◯:◯◯
体重 ◯◯kg (前日比 kg)
【合計摂取栄養素】
カロリー： kcal
P（たんぱく質）： g
F（脂質）： g
C（炭水化物）： g
1回目 朝食（HH:MM）
メニュー名 分量
2回目 昼食（HH:MM）
メニュー名 分量
3回目 間食（HH:MM）
メニュー名 分量
4回目 夕食（HH:MM）
メニュー名 分量
【自己評価】
（※ユーザー記載をそのまま使用）
【運動】
（※画像から抽出した運動メモを改行を維持してそのまま記載）

【フォーマット厳守ルール】
・出力は上記の【出力フォーマット】の内容のみとする。
・日付は「4月29日(水)」形式（ゼロ埋め禁止）。
・【各食事のPFC合計】は【合計摂取栄養素】より前に配置。
・食事回数は「1回目 朝食」「2回目 昼食」形式。
・食事と時間は同一行に記載（例：1回目 朝食（09:00））。
・メニューは改行のみで箇条書き記号（「・」など）は使用禁止。
・体重は「体重 ◯◯kg (前日比 kg)」の形式で出力し、画像から抽出した数値を◯◯に入れ、「(前日比 kg)」という文字列はそのまま残すこと。
・自己評価はユーザー記載をそのまま使用（改変禁止）。
・運動メモは【自己評価】の下に配置し、画像内の改行をそのまま維持して出力すること。
・日本語・事務的・簡潔に記述。推測・補完は禁止。情報不足は必ず「不明」とする。
・過去データ参照禁止。ハルシネーションは避ける。
・睡眠、体重、運動メモは画像に記載のものをフォーマットに従ってそのまま出力する。

さらに、以下のJSONも出力の末尾に追加してください（スプレッドシート書き込み用）。他の文字は一切付けないこと。

<<<JSON_START>>>
{
  "date": "M/D形式（例: 5/26）",
  "weight": 数値のみ（kgを除く、例: 68.5）,
  "breakfast": {"P": 数値, "F": 数値, "C": 数値},
  "lunch": {"P": 数値, "F": 数値, "C": 数値},
  "dinner_snack": {"P": 数値, "F": 数値, "C": 数値},
  "exercise": [
    {"menu": "メニュー名", "reps": "回数や時間", "sets": "セット数（不明なら空文字）"}
  ]
}
<<<JSON_END>>>"""


# ─────────────────────────────────────────────
# ヘルパー関数
# ─────────────────────────────────────────────

def get_week_number(record_date: date) -> int:
    """基準日からの週番号を返す（1始まり）"""
    delta = (record_date - START_DATE).days
    return delta // 7 + 1


def parse_record_date(date_str: str) -> date:
    """'M/D' 形式の文字列を date オブジェクトに変換（現在年を使用）"""
    parts = date_str.strip().split("/")
    month = int(parts[0])
    day = int(parts[1])
    year = datetime.now().year
    # 年またぎ対応（1〜3月の記録を年末に処理するケース）
    today = date.today()
    candidate = date(year, month, day)
    if abs((candidate - today).days) > 180:
        candidate = date(year - 1, month, day)
    return candidate


def _is_daily_quota_error(err_str: str) -> bool:
    """日次クォータ枯渇かどうかを判定（分次ではなく日次 = モデル変更が必要）"""
    return "PerDay" in err_str or "limit: 0" in err_str


def _parse_retry_delay(err_str: str) -> int:
    """エラー文字列から retryDelay 秒数を抽出（見つからなければ 60）"""
    m = re.search(r"retry[_ ]?(?:in|delay)[^\d]*(\d+)", err_str, re.IGNORECASE)
    return int(m.group(1)) + 2 if m else 60  # 少し余裕を持たせる


def analyze_images_with_gemini(image_list: list) -> dict:
    """Gemini API に複数画像をまとめて1回で送って解析結果を返す。

    image_list: [(bytes, mime_type), ...]

    フォールバック戦略:
      - 日次クォータ枯渇 → 即次モデルへ切り替え
      - 分次レート制限  → エラーで指定された秒数だけ待ってリトライ（同モデルで1回）
      - 全モデル枯渇    → エラー詳細を返す
    """
    import time

    client = genai.Client(api_key=GEMINI_API_KEY)
    parts = [types.Part.from_bytes(data=b, mime_type=m) for b, m in image_list]
    parts.append(GEMINI_PROMPT)

    last_error = None
    tried_models = []

    for model in GEMINI_MODEL_CHAIN:
        tried_models.append(model)
        per_minute_retried = False  # 分次制限は1回だけリトライ

        for attempt in range(2):  # attempt 0: 初回, attempt 1: 分次待ちリトライ
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=parts,
                )
                # 成功
                full_text = response.text
                json_match = re.search(r"<<<JSON_START>>>(.*?)<<<JSON_END>>>", full_text, re.DOTALL)
                if not json_match:
                    raise ValueError("Gemini の応答から JSON を抽出できませんでした。\n\n" + full_text)
                extracted = json.loads(json_match.group(1).strip())
                gemini_text = full_text[:full_text.find("<<<JSON_START>>>")].strip()
                extracted["_gemini_text"] = gemini_text
                extracted["_model_used"] = model  # デバッグ用
                return extracted

            except Exception as e:
                err_str = str(e)
                last_error = e

                is_quota = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                if not is_quota:
                    raise  # クォータ以外のエラーはそのまま送出

                if _is_daily_quota_error(err_str):
                    # 日次枯渇 → このモデルは諦めて次へ
                    break

                # 分次制限 → 1回だけ待ってリトライ
                if not per_minute_retried:
                    per_minute_retried = True
                    wait = _parse_retry_delay(err_str)
                    time.sleep(wait)
                    continue  # attempt 1 へ
                else:
                    # 待ってもまだ制限 → 次モデルへ
                    break

    # 全モデルが失敗
    tried = ", ".join(tried_models)
    raise RuntimeError(
        f"すべてのモデルでクォータが枯渇しています（試行: {tried}）。"
        f"しばらく時間をおいて再度お試しください。\n詳細: {last_error}"
    )


def write_to_spreadsheet(data: dict) -> str:
    """解析データをスプレッドシートの適切なタブ・セルへ書き込む"""
    # サービスアカウント認証
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

    # 週番号を計算
    record_date = parse_record_date(data["date"])
    week_num = get_week_number(record_date)
    tab_name = f"{week_num}週目"

    # タブを取得（見つからなければエラー）
    try:
        worksheet = spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        raise ValueError(f"タブ「{tab_name}」が見つかりませんでした。スプレッドシートに追加してください。")

    # Q列を検索して日付行を特定
    date_str = data["date"]  # 例: "5/26"
    q_col_values = worksheet.col_values(17)  # Q列 = 17番目

    row_n = None
    for i, cell_val in enumerate(q_col_values, start=1):
        if cell_val.strip() == date_str:
            row_n = i
            break

    if row_n is None:
        raise ValueError(
            f"タブ「{tab_name}」のQ列に「{date_str}」が見つかりませんでした。"
        )

    # セル書き込み（バッチ）
    updates = []

    def cell(col_letter: str, row: int) -> str:
        return f"{col_letter}{row}"

    # 日付 Q(N)
    updates.append({"range": cell("Q", row_n), "values": [[date_str]]})
    # 体重 S(N)
    updates.append({"range": cell("S", row_n), "values": [[data.get("weight", "")]]})

    # 朝食 S(N+3), U(N+3), W(N+3)
    bf = data.get("breakfast", {})
    updates.append({"range": cell("S", row_n + 3), "values": [[bf.get("P", "")]]})
    updates.append({"range": cell("U", row_n + 3), "values": [[bf.get("F", "")]]})
    updates.append({"range": cell("W", row_n + 3), "values": [[bf.get("C", "")]]})

    # 昼食 S(N+4), U(N+4), W(N+4)
    lu = data.get("lunch", {})
    updates.append({"range": cell("S", row_n + 4), "values": [[lu.get("P", "")]]})
    updates.append({"range": cell("U", row_n + 4), "values": [[lu.get("F", "")]]})
    updates.append({"range": cell("W", row_n + 4), "values": [[lu.get("C", "")]]})

    # 夜・間食 S(N+5), U(N+5), W(N+5)
    ds = data.get("dinner_snack", {})
    updates.append({"range": cell("S", row_n + 5), "values": [[ds.get("P", "")]]})
    updates.append({"range": cell("U", row_n + 5), "values": [[ds.get("F", "")]]})
    updates.append({"range": cell("W", row_n + 5), "values": [[ds.get("C", "")]]})

    # 運動 Z(N+2), AA(N+2), AB(N+2) から1行ずつ
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

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    # 複数ファイル（images[]）または単一ファイル（image）を受け付ける
    files = request.files.getlist("images") or request.files.getlist("image")
    files = [f for f in files if f and f.filename != ""]

    if not files:
        return jsonify({"success": False, "error": "画像ファイルが選択されていません。"}), 400

    if not GEMINI_API_KEY:
        return jsonify({"success": False, "error": "GEMINI_API_KEY が設定されていません。Secretsを確認してください。"}), 500

    image_list = [(f.read(), f.content_type or "image/jpeg") for f in files]

    # 全画像を1回のGemini APIコールで解析（レート制限対策）
    try:
        data = analyze_images_with_gemini(image_list)
    except Exception as e:
        return jsonify({"success": False, "error": f"AI解析エラー: {str(e)}"}), 500

    # スプレッドシート書き込み
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
def test_connection():
    """スプレッドシート接続テスト＆サービスアカウントメール確認用"""
    result = {}

    # サービスアカウントのメールを取得
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return jsonify({"success": False, "error": "GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。"}), 500
    try:
        sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        result["service_account_email"] = sa_info.get("client_email", "取得できませんでした")
    except Exception as e:
        return jsonify({"success": False, "error": f"JSON解析エラー: {e}"}), 500

    # Gemini APIキーの存在確認
    result["gemini_api_key_set"] = bool(GEMINI_API_KEY)

    # スプレッドシートへの接続テスト
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(sa_info, scope)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_url(SPREADSHEET_URL)
        sheet_titles = [ws.title for ws in spreadsheet.worksheets()]
        result["success"] = True
        result["spreadsheet_title"] = spreadsheet.title
        result["tabs"] = sheet_titles
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
