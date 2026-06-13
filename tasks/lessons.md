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
- 2026-06-14 | per-book/per-cellの成績差を単一config(consensus閾値等)のせいにするな=price帯/資金規模/時期が交絡。config変更の因果主張は**entry_date timingで検証してから**(p1m損失の97.9%がcons5変更5-27より前=cons5は無実、真因はflex最小単元×¥500未満ゴミ帯)。小n(n=33/9日)でconfigフリップ禁止、shadow A/Bで分離検証 | p2m設計を§2.5 plan-gate(独立opus)が却下
- 2026-06-14 | gotobi(五十日)fadeは本物・gotobi固有(非gotobi exit −17bps vs gotobi +51bps=符号反転、universe 5/6年+)。だがrobust~+6bps小・+17bpsは1ヶ月artifact・北極星に極小→3-1 KILL+bank。R3「+0.5-1.5%」は非再現(実測bps級) | /tiara 3-1 loop W1
- 2026-06-14 | exit-timing検証は執行タイミングを正しくモデルせよ: MA25等の大引け確定シグナルは『その日のopen』では執行不可(T+1 open or 大引け)。same-day openでbacktestするとcapturability過大評価 | 3-1 W1方法論ミス
- 2026-06-14 | book削除はDB移行不要=monitorがbook非依存(src/monitor.py:338 WHERE status=open)でBOOKSループ外1回(main.py:837)実行→削除bookの開いた建玉も正常決済(orphan化しない)。config削除=新規建玉が止まるだけ。§2.5 plan-gate(独立opus)が前提を独立再検証+見落とし4テスト捕捉、さらに実読でnegative_free_cash testの5件目を捕捉(self-cert禁止が効いた) | /tiara book 3本化(p1m/p30m/p50m削除→p2m/p5m/p10m)
- 2026-06-14 | book削減はregime_filter A/Bのtreatment arm縮小に直結(treatment{p5m,p30m}→{p5m}単独)。整理系の変更でも測定中の実験への副作用をsurfaceせよ(silent禁止)。削除bookのclosed履歴はhb_learning._ab_summary(BOOKS非フィルタ)のweekly出力に残る | 同上
- 2026-06-14 | S1ゲート(建てる前にbacktestでedge実在確認)が北極星本丸2-Bを薄い前提のまま弾いた: 「もみ合い=逆張り/トレンド=順張り切替」非支持(逆張りは上げでP4が既ブロック=穴が無い/順張りは上げで勝たない)。設計研究の前にcheap-failで方向転換=p2m/3-1と同じ勝利パターン。北極星本丸でもS1で安く殺せる | /tiara 2-B Phase A
- 2026-06-14 | 高勝率regime(逆張り弱気67%)を見たら「厚く張る」前にtail-risk検証必須: 弱気=暴落局面でもあり、平均勝率の裏に致命的左尾(panic買いが反発しない=-50%)が隠れる(boat 87%喪失/R1勝率過大推定)。平均でなく最悪ケースで | 2-B拾った宝(2-I)の安全境界 |
- 2026-06-14 | **backtest sizing比較は現金/証拠金ゲートが無いとレバレッジ交絡で誤誘導**: engine.pyのentry gateは建玉数チェックのみ(:349)、残高チェック皆無→×2 sizingは最大200%展開し「多く張れば勝つ」をedgeと誤認。cross-variant calmarは等展開に正規化orキャッシュゲート追加してから比較せよ。maxDDはmargin call/強制ロスカットを見ていない | /tiara 2-I S1 tail-gate(独立評価opusが俺の見落としを捕捉) |
- 2026-06-14 | **calmar/比率指標は年率化してから解釈**: 5年total/maxDDの生calmarは見かけ優位を~6.6倍に錯覚させた(+0.88→年率+0.04≒ノイズ)。big-number framingに飛びつくな。per-trade tail(-46%)もportfolioで1/10に薄まる=多層(per-trade→portfolio→年率→レバレッジ補正)で疑え | 同上 |
- 2026-06-14 | **「safety装置を外すと成績が上がる」は denominator-swap artifactを疑え**: tail-cap除去で calmar上昇に見えたが、各変種のmaxDDは別々の市場イベント(V0=2025-04/V1b=2024-08/V2a=2022-03)。capは仕事(Aug spike除去)をしたがbinding DDが別暴落に移っただけ。元の破滅仮説は非反発暴落で未検証=反証されてない。independent-evaluatorがself-cert(俺は「厚張れ・capするな」=危険方向に結論しかけ)を救った。3連続cheap-fail gate(p2m/2-B/2-I) | 同上 |

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

## pytest GREEN化（W1 2026-06-13）

- 2026-06-13 | .gitignoreの`data/`パターンが`src/data/`を巻き込み、jquants/universe/sector_thresholds等7ファイルがgit未追跡。worktreeに存在しないためImportError連発。gitignoreは`/data/`(ルート相対)で書かないとsrc/data/も対象になる | W1 pytest ERROR根治
- 2026-06-13 | test_oss_smoke.pyのmax_drawdown baseline(-0.2302)はsrc/data回収後の再計測値(-0.2241)と乖離。baseline陳腐化の場合は根因を調べてから新値でpin更新が正（テストを緩めるのではなくgolden値を現実に合わせる） | W1 baseline更新
