# カロミル自動記録システム

カロミルの食事・体重スクショをアップロードすると、Gemini AIが解析してGoogleスプレッドシートへ自動書き込みするWebアプリ。

## Run & Operate

- `python3 artifacts/karomill/app.py` — Flask アプリ起動（ポート5000）
- ワークフロー名: `カロミル記録アプリ`

## Stack

- Python 3.11 + Flask
- Gemini API (`google-genai`) — 画像解析
- gspread + oauth2client — Google Sheets書き込み

## Where things live

- `artifacts/karomill/app.py` — メインFlaskアプリ（ルート・解析・書き込みロジック）
- `artifacts/karomill/templates/index.html` — モバイル向けUI（シングルページ）
- `artifacts/karomill/requirements.txt` — Python依存パッケージ

## Architecture decisions

- Gemini 1.5 Flash で画像→構造化JSON抽出（プロンプト末尾に `<<<JSON_START>>>` ブロック付与）
- 基準日（`START_DATE`）から週番号を計算してタブを自動特定
- Q列を全行スキャンして日付行を特定、相対オフセットでセル書き込み
- サービスアカウントJSONはReplit Secretsに文字列として格納

## Configuration

`artifacts/karomill/app.py` の先頭で変更可能な変数:
- `START_DATE = date(2026, 2, 16)` — 1週目の月曜日（基準日）
- `SPREADSHEET_URL` — 対象スプレッドシートURL

## Required Secrets

- `GEMINI_API_KEY` — Google AI Studio で発行
- `GOOGLE_SERVICE_ACCOUNT_JSON` — サービスアカウントJSONの中身（テキスト全体）

## User preferences

- モバイルファーストのシンプルUI（1画面完結）
- Python Flask ベース
