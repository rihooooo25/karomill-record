"""gunicorn.conf.py — Gunicorn が自動読み込みする設定ファイル。

Render ダッシュボードの startCommand より確実に適用される。
gthread ワーカーを使うことで:
  - リクエストはスレッドプールで処理される（main thread とは別）
  - Gunicorn の WORKER TIMEOUT signal は main thread に届くため
    リクエスト処理スレッドに SystemExit が伝播しない
  - 長時間 I/O（Gemini API 呼び出し）中もハートビートが生き続ける
"""
import os

worker_class = "gthread"
workers = 1
threads = 4
timeout = 300            # バックストップ（アプリ内で 180s のタイムアウトが先に発火する）
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
