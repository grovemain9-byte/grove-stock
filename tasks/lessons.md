# tasks/lessons.md — 失敗→即ルール化（生きた脳の追記面）

> 運用: ミス/想定外が起きた**その場で**1行追記（セッション末まで溜めない）。
> 繰り返し効くものはCLAUDE.md本文 or Skill化に昇格。形式: `日付 | lesson | 由来`

## 環境・ツール（旧CLAUDE.md「これまでの失敗の記録」より移管 2026-06-13）

- pandas-taはpipで取得不可。taライブラリ（pip install ta）を使う
- spec.mdのpandas-ta記載はtaライブラリと読み替えること
- J-Quants Freeプランは当日データ取得不可。Lightプラン（¥1,650/月）以上が必要
- yfinanceは当日未確定データでNaN行を返す。dropna(subset=["close"])が必要
- pytestはweb3プラグインがグローバルに入っており、PYTEST_DISABLE_PLUGIN_AUTOLOAD=1が必須

## 計測・検証

- 2026-05-30 | forward観察は「観察対象を固定し、計測窓を登録基準に厳密一致」。観察期中のbranch開発と計測窓ズレは両方とも誤判定を生む | kill_criterion中間レビュー（verify script窓bug: SINCE=5/17が登録窓5/25を上書き→K5/K1誤発火）
- 2026-06-13 | .gitignore無しで__pycache__が混ざるとgit statusが汚れ、コア実装86件が未コミットのまま放置される。リポジトリ衛生は並列運転の物理前提 | S0実測

## cron・スクリプト設計

- 2026-06-13 | bash算術でゼロ埋め数字（09, 00）はデフォルトでoctal扱いになり「invalid number」エラー。`10#$VAR`プレフィックスで強制10進数に。date出力を算術で使うときは必須 | weekly_retrain.sh市場時間チェック実装
- 2026-06-13 | シェルcronラッパーに推奨crontab行をコメントで埋め込む（正時±7分ずらし・衝突チェック）。`--dry-run`フラグはPythonへ`--skip-plt --skip-optimizer`で渡す設計が安全 | weekly_retrain.sh

## 運転（Boris式ループ）

- 2026-06-13 | planの単位は「単発タスク」でなく「運転体制」。タスク1個のplanを繰り返すのは弱い提案 | Grove訂正（Davis側feedbackにも焼く）

## ドキュメント同期（W3 docs-sync 2026-06-13）

- 2026-06-13 | docs同期の「TODO注記」は **設計当時のdoc書き換え禁止**、注記（comment/追記）で現状を示す。書き換えると「なぜそう設計したか」の文脈が消える | W3 ops-sync
- 2026-06-13 | PLT cell 成長は closed positions 数に律速される。数週間稼働してもboot strapに閾値件数が無ければ期待曲線に到達しない。operations.md の「1週間後期待値」は楽観的 | W3実測（cold:5 warm:1, 3週間超稼働）
- 2026-06-13 | docs内の「次セッションTODO」は実装後に状態注記を入れないと永遠に未完に見える。build_diary末尾との突合が必須 | W3 agent_architecture_plan.md突合
- 2026-06-13 | macOS非TTY環境(Claude Code)からのcrontab書込はハングする(3連続実証)。launchd StartCalendarInterval+WorkingDirectoryが正解(cd忘れ問題も構造的に消える) | weekly_retrain cron化
- 2026-06-13 | workerの「dry-run完走」主張がログ上は失敗してた(worktree内に無い.venvを叩いてDONE宣言)。Leaderの独立再実行が偽緑を捕捉=再実行は儀式でなく実利 | W2検証
