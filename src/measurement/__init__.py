"""計測インフラ（plan S5）。

live hot path（scan/voting/kelly/monitor/engine）に同期書込を足さない。
書込は daily_update 後のバッチ想定。各テーブルは shadow_replay 流儀の
自己完結（CREATE TABLE IF NOT EXISTS をモジュール内）で db.py の
_create_tables（接続毎実行＝F5ロック源）を膨張させない。
"""
