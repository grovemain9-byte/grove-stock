# grove-stock 建築日誌

## 2026-05-16 — Shadow Replay: 勝敗データ生成（Iter 1）

### 何を作ったか
`src/backtest/shadow_replay.py` + `tests/test_shadow_replay.py`。
記録済み `scans` シグナルを過去実価格に再生し、consensus閾値 c=2/3/4 別の
勝敗データを `shadow_trades` テーブルに生成する。liveは一切変更しない。

### なぜ（背景）
測定で真因が判明: liveは27,045スキャン中 consensus≥3 を **978回** 発火した
のに positions は **3件のみ**（全て5/12・全敗 stop_loss）。実行パイプライン
不全で勝敗データが貯まらず「どの厳しさが勝てるか」を検証できなかった。

### 途中で踏んだ/回避した地雷
- 当初「監視銘柄13で狭い」と仮説 → コード(`src/main.py`)を読むと live は
  既にTOPIX500約507銘柄を見ていた。**読まずに書いてたら誤実装**だった。
- 「戦略が厳しすぎてシグナルが出ない」も誤り。978回出ていた。真因は実行部。
- pytest は web3 プラグインがグローバル混入 → `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 必須。
- 出口ルールは自前実装せず engine.py の `STOP_LOSS`/`MAX_HOLD_DAYS`/
  `_compute_indicators` を import 流用（戦略核心の二重定義を回避）。

### 検証結果（数字）
- 全7テスト緑（unit6 + 統合証明1）。既存スイート 131 passed 非破壊。
- 再生結果（決済済のみ）:
  | consensus | 決済済 | 勝率 | 平均リターン/取引 |
  |---|---|---|---|
  | ≥2 | 218 | 63.8% | +0.97% |
  | ≥3 | 73 | **65.8%** | **+1.92%** |
  | ≥4 | 6 | 66.7% | +0.53%（n小） |
- shadow_trades 1,250行保存。再実行で1,250→1,250（**冪等**、DELETE+INSERT）。
- 結論: 戦略自体は c=3 で勝率65.8%。live実取引3件全敗は**戦略でなく実行の問題**。

### 次に同じことをする人への注意
- `still_open` が多い（c=2で625件）= 5月中旬の新しいシグナルはキャッシュ末尾
  までに15営業日経っていないだけ。バグではない。時間経過で決済化する。
- 各シグナルは独立仮想エントリー（資金管理・同時保有上限なし）。これは
  「シグナル品質」測定用の単純化。ポートフォリオ収益率ではない。

## 2026-05-16(2) — ①実価格ペーパー化 + ②DuckDBロック修正

### 真因（978シグナル→3取引）
- cronが `main.py` を `--paper`なしで起動 → 実立花クライアント。
  `if not broker.login(): return` でログイン失敗時サイクル全中断。
- DuckDB「単一RW or 複数RO」制約 + cron重複(scan×3+daily_update)で
  "Could not set lock on file" 頻発、daily_updateも落ち履歴が5/14凍結。
- 100万円シミュ: 期間3.5週/最大保有15日のため決済2件のみ＝数字は無意味。
  信頼できるのは個別シグナル品質 c=3 勝率65.8% +1.92%/取引(73件)。

### 修正（ペーパー限定・本番発注なし）
- `src/data/db.py`: `connect_duckdb()` 追加。lock時のみ指数バックオフ再試行。
  `get_connection` と `cache.py` の全接続を置換。
- `src/main.py`: `--paper` 追加。Mock+立花ログイン不要。`execution_node` で
  Mockに market_data 実終値を注入（ダミー¥1000を排除）。
- `src/monitor.py`: `exit_node` も Mock を実現在値で約定。
- crontab: scan3本に `--paper`、daily_update 14:50→15:30（大引け後・scan後）。
  バックアップ `~/.crontab.backup.*`。

### 検証
- 全142テスト緑（+test_paper_mode 5: lock retry 4 / 実価格約定 1）。
- 変更ファイルの ruff F: 自分が入れた1件のみ修正、既存5件はスコープ外で不変。
- 注意: 全サイクルのライブ実証は次回cron（平日9:00）。`feedback_no_restart_
  without_permission` によりbot手動起動はGrove許可制。

### 基準値確定（3年大標本バックテスト 2026-05-16）
既存 engine.run_backtest を3年(2023-04-24〜05-14)・494銘柄・¥1M・同時保有7・
p4 optional で実行（新規コードなし、`/tmp/bt3y.py`、64s）。

| c | 決着 | 勝率 | 平均/取引 | maxDD | Sharpe/取引 |
|---|---|---|---|---|---|
| ≥2 | 712 | 58.8% | +0.40% | -13.4% | 0.08 |
| **≥3** | **477** | **60.0%** | **+0.90%** | **-11.6%** | **0.14** |
| ≥4 | 398 | 55.5% | +0.55% | -15.1% | 0.09 |

- c=3が全指標最良 → 現行ルール(consensus≥3)は3年データで妥当と裏付け。
- 小標本73件の 65.8%/+1.92% は楽観（生存バイアス）。大標本で 60.0%/+0.90%
  に収束。「測ってから言う」の実証。
- ¥1M概算: ¥100k×477×+0.90% ≈ +¥429k（3年+43%/年率≈+13%）maxDD -11.6%。
  非複利・スリッページ未考慮・1レジームの限界あり。が母数477で信頼可。

### 次の課題（③ 未着手）= この基準値を超えるか
- ペーパーデータ蓄積後、AIエージェントが勝敗を見て閾値/paramを動的調整
  → 継続運用ループ（「botからAIエージェントチーム」本体）。
- ③の評価軸: 上記の固定param基準（c=3 年率≈+13%/DD-11.6%）を、動的調整が
  walk-forwardで上回るか。3年履歴で walk-forward 検証は今すぐ可能。

---

## 2026-05-16 Phase 0: マルチブック・ペーパーエンジン（データ生成不全の根治）

### 何を作ったか
真因 = `kelly.py` の `shares = int(position_value/price/100)*100`。¥1M×c=3配分
10%=¥10万 → ¥3000株は100株=¥30万>予算 → `int(0.33)=0` → 全シグナル0株 →
「注文なし」。実取引3件が全て約¥1000銘柄だったのはこれが理由（境界でギリギリ）。

Grove方針で修正:
- 100株単位は維持（単元未満株にしない）
- ¥1M/¥5M/¥10M/¥30M/¥50M の5ブックを並列稼働（`config/books.py`）。各ブック複利
- 少額(¥1M/¥5M)= `flex=True`: 固定%で0株なら最低1単元保証・**上限なし**
  （集中度/回収タイミングは将来 council が strategy_params 経由で判断する前提）
- 大型(¥10M+)= 固定10/15/20%規律維持（trading-rules準拠）
- `positions.book` 列追加、`book_account()` で equity=初期+実現損益、
  free_cash=equity−拘束(open原価+手数料)。`kelly_node` が cash上限を累積ガード

### 踏んだ地雷
- `kelly_size` のシグネチャ変更は既存テスト破壊リスク → `flex` を keyword-only
  default False にし従来挙動を1ビットも変えず（既存 test_100_unit_rounding が
  バグ挙動 `kelly_size(3,1M,3000)==0` を golden 化していた＝後方互換の証明点）
- `--paper` で5回 run_scan_cycle すると scans が5重記録 → ingestion+voting は
  資金非依存なので1回、kelly+execution のみブック毎ループ（`run_paper_multibook`）
- 本番DB/cron bot手動起動はGrove許可制 → 検証は隔離tmp DBに実キャッシュ価格で実走

### 検証結果（数字）
- 全149テスト緑（回帰0、6 integration deselected）+ 新 test_multibook.py 13件緑
- 実データE2E（隔離tmp・同一実シグナル22件 consensus≥3）:
  修正前 0件 → 修正後 **31件/5ブック**（p1m flex:3件¥99.6万, p5m:7, p10m:7,
  p30m:7, p50m:7）。少額¥1Mも柔軟サイジングでエントリー成立＝複利で育つ素地

### 次に同じことをする人へ
- `flex` は Phase 0 暫定。S1 で `config/strategy_params.py` に統合し council の
  Judge が触れるレバーにする（plan: Judgeのみ書込）。`config/books.py` も同様
- 少額ブックは集中度が高い（p1m=3銘柄に¥1M）。これは Grove 意図（上限なし、
  集中判断はエージェント）。S5 decision_shadow + 本体councilで集中度を評価する
- monitor は無変更でOK（book列は行に付随しUPDATEで保存。exitは資金非依存）

## 2026-05-16 F2: voting scans書込の接続バッチ化（ロック競合の主因除去）

### 何を直したか
本番 scan.log 15:00 に "Could not set lock on file" 49件、ingestion 445/494。
真因確証: `vote_all` が銘柄毎に `get_connection()`（毎回 `_create_tables` の
CREATE×4+ALTER×2+SEQUENCE×2 DDL付）→ `voting_node` 494銘柄ループで
**1スキャン494回 open+DDL+INSERT+close**。cron多重(scan×3+daily_update)で競合爆発。

修正: `vote_all` に optional `con` 追加（`_write_scan` ヘルパー抽出）。
`voting_node` がループ外で接続1回、全銘柄共有、ループ後close。`con=None` 時は
従来通り銘柄毎open/close（monitor:141・test_voting 全件・既存挙動を完全維持）。
接続失敗時は con=None フォールバック（graceful-degradation 準拠）。

### 検証（数字）
- 全153テスト緑（回帰0）= votes結果・scans内容の挙動保存
- 実測 N=60: connect_duckdb `60回(O(N)) → 1回(O(1))`、scans行数60=60・
  consensus内容一致（旧/新パス突合 assert）
- フォールバック実証: voting_node初回接続をlock失敗注入 → `voting:connect:`
  記録しつつ銘柄毎パスで scans 記録継続（votes=1, scans=1）

### 次に同じことをする人へ
- monitor.py:141 の vote_all は con渡さない＝従来通り（open数少=storm源でない、
  monitorのscans副作用も保存）。意図的に手を付けていない
- event loop も銘柄毎 new/close だが本タスク対象外（DB接続が主因）。残課題

## 2026-05-16 M1: config一元化 S0-S3（挙動保存リファクタ）

### 何をやったか（plan 土台フェーズ）
20軸ハードコード分散 → 単一ソース `config/strategy_params.py:DEFAULTS` に集約。
- **S0** `tests/test_characterization.py`: 決定論入力で現挙動を golden 凍結
  （kelly_size表 / _compute_indicators末尾 / vote_all / F1=ticker5桁→-0.07）。
  `run_backtest` 3年c=3 を **integration golden**（total484/closed477/
  win0.59958071278826/avg0.009007905597061546/maxDD-0.11634845631694837）
  ＝ S2受入の exact ゲート
- **S1** `StrategyParams(frozen)` + `DEFAULTS`=現LIVE値。kelly_alloc は
  tuple-of-tuples（mutable default罠回避）+ `kelly_alloc_map()`。未配線証明
- **S2** engine.py 戦略定数8個を DEFAULTS 参照（純粋リネーム・値同一）
- **S3** voting/kelly/monitor/main/p02-p05 を DEFAULTS 参照（p01はS6/Lane B据置）

### 検証（挙動保存の機械的証明）
- S0 golden を凍結 → S2/S3 後も **同 golden が緑のまま** = リファクタが
  挙動を1ビットも変えていない機械的証明
- integration baseline が S2/S3 後も **exact 再現**（誤差0）= portfolio
  レベルの挙動保存証明（plan受入基準2）
- 全173 非integration緑（回帰0）。test_wiring_stage で段階逸脱をガード

### 踏んだ地雷 / 設計判断
- p03 `window_dev=2`(int) → `DEFAULTS.bb_std=2.0`(float)。2*std==2.0*std で
  BB下限不変（characterization vote_all golden で確認済）
- engine `run_backtest` の `p4_required=True` / `max_concurrent=10` 既定は
  **DEFAULTSに配線しない**（既定を変えると default 呼出側の挙動が変わる＝
  挙動保存違反。baseline は明示的に False/7 を渡すので無関係）
- `test_s1_is_unwired` は kelly.py の**コメント**に "strategy_params" 語が
  あり誤検出 → import文限定 grep に修正（段階トラッキングtest化）

### 次に同じことをする人へ
- p01 と Lane B(セクター閾値) は S6 で kσ に意図的変更。そこで初めて
  characterization を golden 更新し旧→新差分を Grove 数値報告する
- 次は S4(OSS: empyrical-reloaded のみ6工程精査・pip導入＝**システム変更**)

## 2026-05-16 S4-S5: OSS土台 + 計測インフラ

### S4 OSS土台
- empyrical-reloaded==0.5.12 のみ6工程精査（記録: docs/oss_integration.md）。
  pip-audit 脆弱性0 / src純計算 / Apache2.0 / 弱点=peewee依存はutils不使用で回避。
  無印 empyrical(dead 2020)は requirements-research.txt にコメントで採用禁止固定。
- 温存: engine.metrics() は ground truth。empyrical は追加レンズ。
  クロスチェック test_oss_smoke.py: empyrical.max_drawdown == engine equity式
  maxDD（合成+ランダムウォーク+実3年、diff 0.0）。stumpy/tsfresh/vectorbt/
  optuna は G2/G6 でデータ充足後送り。

### S5 計測インフラ（src/measurement/）
- capital_tracker.snapshot: 5ブック equity/committed/free を capital_state へ。
  同日 DELETE→INSERT で冪等。book_account 再利用。
- decision_shadow: record_proposal(cf NULL) + backfill_counterfactuals。
  **即日計上禁止規約**: shadow_replay._simulate_one 流用、出口が実バーで
  成立した日のみ cf 確定、still_open は NULL 維持。回帰テストで固定。
- walk_forward: 自前 purged_splits（sklearn不採用）。embargo=DEFAULTS.
  max_hold_days。metrics_from_equity は empyrical+engine式maxDD併記。

### 踏んだ地雷
- decision_shadow テスト初版で「横ばい1000×40 → 出口なし」と誤想定。実は
  MA25収束→deviation=0→**take_profit発火**（コード正しい）。still_open を
  作るにはバー<25(MA25=NaN)・-7%未満・<15営業日が必要。テスト前提を修正。
- shadow_replay 自己完結テーブル流儀を踏襲（plan は db.py 追加と書いたが
  _create_tables は接続毎実行＝F5ロック源。膨張させない方が正しい）。

### 検証
- S4: smoke4+integration1。S5: 9件。全185非integration緑（回帰0、開始149→+36）
- engine.py / live hot path 不変。計測は self-contained・batch 想定

### 次に同じことをする人へ
- **S6 は意図的挙動変更**（p01をkσへ）。S0特性 golden を意図的に更新し、
  旧(一律-0.07)→新(業種別kσ)の live 発火率/基準差分を **Grove に数値報告**
  してから確定（plan受入4）。silent にやらない。S7は基準再凍結=G3ゲート。

## 2026-05-16 S6: Lane B kσ統一（意図的 live 挙動変更）

### 何を変えたか
p01 のセクター閾値ソースを `DEFAULTS.sector_threshold_mode` で分岐:
- "ksigma"(既定): `src/data/sector_thresholds.py` の業種別kσ（backtestと統一）
- "static": 旧 config.sector_config（即ロールバック用に保持）
daily_update 後に kσ を再計算し CACHE_DB の sector_thresholds テーブルへ保存。
F2教訓: live_threshold はプロセス内メモ化（494銘柄を1ロード）。

### Grove数値報告（旧→新の実測差分）
- kσ閾値: 29業種 -0.0647〜-0.0300（平均 -0.0408）。旧は全銘柄一律 -0.0700
- **live P1発火: 52 → 126銘柄（+74、旧のみ0＝全て発火増方向）**
  ＝旧liveは検証済backtest(既にkσ)より厳しすぎて過少発火だった。S6で整合
- 3年backtest基準(484/477/勝率60.0%/maxDD-11.6%)は **exact不変**
  （S6は engine/runner 非接触＝live のみ変更。integration test で実証）

### 設計判断/地雷
- backtest engine は元から runner 経由で kσ を使用。Lane B 不一致は「live
  だけ -0.07 死にコード」だった。S6 は live を backtest に**寄せる**変更で
  あり、3年基準を動かさない（基準が動くのは S7 universe拡張）
- 既存 test_characterization vote_all は合成ticker'45190'（universe非所属）
  → live_threshold が default -0.07 にフォールバック → golden 自然に緑維持。
  意図的変更は TestLaneBS6（mode分岐を monkeypatch で固定）で別途明示
- test_wiring_stage は S6 で p01 配線され期待通り失敗 → S6最終段階に更新

### 検証
- 187 非integration緑（回帰0）+ 3年基準 integration exact 維持
- mode="static" で旧挙動に即復帰可能（DEFAULTS フィールド保持）

### 次（S7 = G3ゲート・Grove判断要）
universe 494→Prime全1,571+ADV。**母集団が変わり3年基準が無効化** →
slippage+commission込みで再backtest→**新c=3基準を凍結**（これが③動的調整の
唯一の比較対象）。fetch/save_universe は orphan(F2)＝新規ビルドエントリ要。
J-Quants ~1,571銘柄 bulk fetch(~10分,network)。重く不可逆的＝要Grove承認。

## 2026-05-16 S7: コストモデル + 堅牢化（universe拡張は G2 で保留）

### やったこと
- `src/measurement/realistic_baseline.py`: engine BacktestResult を後処理し
  slippage(10bps/片道 Grove確定)+立花手数料往復を per-trade 控除。**重大発見:
  旧基準は約2倍楽観**（avg +0.901%→+0.476%、勝率60→58.7%）。
- 初版で per-trade累積の maxDD -67.7% を出した→**手法アーティファクト**と
  自己検証で捕捉（単一notional退化equity）。誤解field除去。
- `engine.run_backtest` に `slippage_bps`/`apply_commission` opt-in 追加。
  既定OFF=3年基準 **exact不変**（integration golden緑＝engine温存）。
  ON=真のポートフォリオ: 勝率58.7%/avg+0.476%/**maxDD-14.62%**（正常値）。
- `scripts/build_universe.py`（F2 orphan修正の build entry）。

### 地雷（J-Quants Light レート制限）
- Grove が build_universe 実行→`get_list` 単発で 429即死。土曜＝cron競合なし
  ＝純粋にJ-Quants Light枠逼迫（敵対G2「大規模化はLightで脆い」的中）。
- **silent-failure発見**: `apply_adv_filter` の `except: continue` が429を
  握り潰し→全~1,800銘柄スキップ→**ほぼ空universeをsave**しかねなかった。
- 堅牢化: `_call_with_backoff`(429指数バックオフ・非429即raise) / ADV
  throttle 0.5s / `MIN_SANE_UNIVERSE=300` 安全網（部分429で494を破壊しない）。

### 判断（Grove承認）
S7保留＝G2データファースト最忠実。真のボトルネックは銘柄数でなく勝敗
データ枯渇で、Phase0修正で解決済・本番cron生成中。S7bコード(191緑)は
完成・堅牢化済で、データ蓄積 or J-Quants枠回復後に build_universe.py
再実行で即再開可。494 universe backup無傷。

### 次に同じことをする人へ
- 自分のテスト/出力を疑え（maxDD -67.7% を「それっぽい」で流さず捕捉できた
  のは CCS検証原則「80%で安心するな」の実践）。
- engine opt-inコストは既定OFF厳守（ONを既定にすると全goldenが動く）。
- S7再開時: J-Quants Lightは平日cron時間帯を避ける。throttle既定0.5s、
  途中429は backoff で耐えるが計~30-45分の重い処理。

## 2026-05-17 S7: ADVバッチ取得を再開可能化（429パンク防止・時差実行）

### Grove方針
「1571を1ルートで投げたらパンク。何ルートかで時差でパンクしないように」
→ バッチ分割＋バッチ間クールダウン＋チェックポイント再開を実装。

### 実装
- `universe.py`: `adv_progress` テーブル（code PK・取得済=行存在・NULL=
  データ無し）。`fetch_adv_progress(batch_size,cooldown_sec,max_batches,
  throttle_sec)`＝バッチ毎に即保存、バッチ間 sleep、`max_batches` で
  打ち切り→**再実行で続きから**。429 backoff 尽きたら保存を保ったまま
  RuntimeError（再開可）。`build_filtered_from_progress` / `reset_adv_progress`。
- `build_universe.py`: ADV完了まで save_universe しない（494を守る）。
  CLI `--max-batches N`（時差再開）/`--batch-size`/`--cooldown`/`--reset-adv`。
- test_universe_batch.py 4件（チェックポイント/再開/max_batches打切/429保存
  維持/filter/reset）モック・無network。195緑回帰0。

### 使い方（時差実行）
`python scripts/build_universe.py --max-batches 3 --no-bulk` を何回か
（J-Quants枠を見ながら）→ ADV完了したら無印で1回（filter+save+bulk）。
batch_size=150・cooldown=90s が既定。途中で止めても再開で続く。

### 次に同じことをする人へ
- save_universe は ADV 100%完了後のみ（部分結果で494を壊さない＝MIN_SANE
  と二重ガード）。bulk_fetch は既cache skip＝元から再開可。

## 2026-05-17 S7完了: universe 494→1,337 + G3新基準凍結

### 実行
- 再設計版 build_universe: Prime全1,571 → bars時差バッチ取得(429ゼロ・
  J-Quants枠回復・~20分/1,049銘柄) → ADVキャッシュ算出(API0) →
  **universe_snapshot 494→1,337**（ADV≥¥1億）。daily_quotes 1,572銘柄/114万行。
- 完了判定バグ修正: 「全コード60本以上」を要求すると新規上場5銘柄が永久
  未達→永久未完。「全pending取得**試行**済＝完了、data-poorはADVで自然除外」
  に修正（truncated=max_batches打切のみ）。test 6件で固定。
- DB破損インシデント(universe_snapshot 53)を backup から復旧（可逆・
  .broken53保全）。原因=旧 silent-failure版 apply_adv_filter（修正済）。

### G3 新基準凍結（③動的調整の唯一の物差し）
| | 旧494手選び(S2 golden) | **新1,337(G3凍結)** |
|---|---|---|
| 決着 | 477 | **548** |
| 勝率(cost) | 58.7% | **52.9%** |
| avg(cost) | +0.476% | **+0.450%** |
| maxDD(cost) | -14.62% | **-28.77%** |
| Sharpe/trade | 0.14 | **0.049** |

**重要**: 旧基準は**二重に楽観**だった — ①コスト無視(S7) ②**手選び494の
生存バイアス**(S7拡張で露見)。Prime全流動性universe の現実は勝率53%
(コイン投げ+α)・maxDD-29%。これがG3で凍結した真の基準（test_
characterization integration golden化・end_date 2026-05-15固定で再現性確保）。

### 5ブック 3年損益（新1,337universe・cost込）
p1m +60.6%(DD-27%) / p5m +25.9%(DD-33%) / p10m +35.0%(DD-26%) /
p30m +38.9%(DD-30%) / p50m +35.9%(DD-31%)。旧494比で**リターン増だが
maxDD大幅悪化**＝universe拡大で機会増・分散効くが現実の荒さも露呈。

### 次に同じことをする人へ
- ③(動的調整/council)はこの**G3新基準(勝率52.9%/maxDD-28.8%)**を超える
  かで評価。旧+13%/年は幻。普遍的教訓: 手選びユニバースの成績は信じるな。
- universe更新したら必ず新universeで基準を測り直す（kσ業種数も29→32と変化）。
- 全201緑+G3 golden exact。backup無傷。実データ蓄積はcron継続中。

## 2026-05-17 継続ループShadow + regime_filter A/B（"動的最適化チーム"の実体化）

### Grove核心問いへの再フレーミング（sequential-thinking 9thought）
「常にデータ取り分析し動的最適で稼ぐAIチーム」に対し、私の「データ貯まる迄
council後送り」は二値的誤り。**起動≠適用を分離**すれば継続ループは今から
Shadowで回せる（記録専用・コスト0・リスク0＝davis Shadow Mode原則と一致）。
ループ=4時計入れ子(日次bot[完成]/週次council[今Shadow可]/月次複利/随時wf門)。
過学習は規律でなく**アーキで殺す**(複数レジームfold必須/1変更/per-stock禁止/
multiple-testing補正)。権限漸増 shadow→advisory→autonomous。

### ランク選別（仮説検証の実例・正直な失敗）
engine に rank_candidates opt-in 追加（辞書順→consensus降/乖離深/流動性高、
重み学習なし＝過学習回避、既定OFF=golden exact）。walk-forward結果 **1/3窓
で不合格**→不採用。仮説「明白なバグ修正」が out-of-sample で崩れた＝**検証
規律が機能した実証**。唯一再現する構造シグナル=regime効果(Y2必ず改善)。

### 継続ループMVP（src/measurement/hypothesis_loop.py）
決定論 Researcher/Judge 骨格。仮説(G3からの単一param差分)を3年次窓 cost込
walk-forward で G3 と比較→3/3窓で構造的のみGO→`hypothesis_shadow`に記録
（**applied全FALSE=strategy_params一切未変更**）。Guardian不可侵: 1変更ルール
強制・per-stock禁止。初サイクル実走結果:
- **regime_filter (p4必須): GO 3/3**（Y1 Sh.024→.260, Y2 -.058→+.025 損失→
  黒字, Y3 .190→.269。3手法[sweep/wf/loop]再現）
- stop_tight_5 / consensus_4: PASS 2/3（Y3落ち＝レジーム依存→却下）
LLM agent層(創造的仮説生成)は実データ＋骨格実証後の次層。

### regime_filter A/B 配備（Grove承認: 一部ブックのみ適用し並走）
config/books.py に per-book `regime_filter`。treatment={p5m,p30m}・
control={p1m,p10m,p50m}。kelly_node が treatment ブックのみ p4(弱気)必須を
上乗せ。**DEFAULTS.p4_required は False 維持＝engine/backtest G3 golden 不変**
（A/Bは LIVE paper の per-book overlay のみ）。実 paper で regime あり/なしを
同時比較しつつ control でデータ生成継続。全209緑（負free_cashテストは fake
vote に p4 追加で A/B 非依存化）。

### 次に同じことをする人へ
- 「起動≠適用」が鍵。仮説は hypothesis_shadow に GO でも applied=False。
  昇格は Grove 判断(advisory) or Brier実績(autonomous)。silent適用厳禁。
- regime_filter は backtest最強シグナルだが単一3年時代。A/B実paper結果が真実。
- DEFAULTS は触らず per-book overlay で A/B＝golden/基準を壊さず実験する型。

## 2026-05-17 自己進化AIエージェント会社 設計 + HB自律学習起動

### エリートチーム設計（sequential-thinking 2パス・計17thought）
Grove「CLIエージェントが個人/チーム/部署/事業部で自律成長するAI会社・MAX
枠内・OSS・HB駆動・銘柄別チーム/業種別部署」。核心結論:
- 会社＝組織図でなく**実証済"代謝"の希少資源配給付き複製＋ガバナンス**。
  第1資産＝勝てる戦略でなく"騙されない規律エンジン"(discipline_core)。
- フル組織: 基層[1337決定論bot・LLM0・不死心拍] → 銘柄別チーム[決定論
  state+履歴・昇格可能単位] → 業種部署[32 S33・Top-Kローテ週次LLM] →
  事業ユニット[domain council] → 会社L4[資本配分board]。
- phantom型死を構造回避: 基層LLM0 + 部署休眠ローテ + 実績ゲート増員 +
  bot心拍LLM非依存。LLM実消費≈週次1ユニット+K部署＝MAX枠内。
- 成長ラダー(全層メトリクス・両方向自然選択): 個人=Brier→autonomy /
  チーム=A/B track→資本+LLM注意 / 部署=業種集計→予算 / 会社=総equity複利。
  判定機構は決定論(LLM不要)。LLMは創造仮説と反証のみ。
- 自律の正しい定義: bot心拍=不死自律、計測/分析/記録/Brier=完全自律、
  実弾化/権限昇格/メタ則普遍化=人間不可逆ゲート。実証で autonomy がデータ
  で拡大。無ゲート自律=自己破壊(phantom/boat実証)。
- ボトムアップ厳守: L4を今作るのはG2の会社版。1ユニット実データ実証→
  テンプレ化→2nd unit の順。組織図は"到達先"、今は種を回す段階。

### HB自律学習 実装（scripts/hb_learning.py + setup_cron_hb.sh）
種(bot基層稼働 / discipline_core / 5ブック / 32業種kσ)を**人手ゼロで常時
回す**自律化＝Groveの「HB設定すればいい」「自律的に育つ」の文字通りの起動:
- `hb_learning.py --daily`: capital_tracker snapshot + decision_shadow
  backfill（資金回転把握・反実仮想を出口日基準で確定）
- `hb_learning.py --weekly`: hypothesis_loop(Shadow記録のみ) + A/B集計
  （regime_filter treatment{p5m,p30m} vs control）
- `setup_cron_hb.sh`: 日次15:45平日 + 週次土08:00（取引cronと独立・grove-hb
  タグ）。全 shadow/記録/read-only・適用ゼロ。
これで 個人Brier/チームA/B/部署業種/会社equity の成長メトリクスが毎週
自動蓄積＝組織が"自律的に育つ"土壌が回り出す。

### 地雷（HB検証で捕捉した実バグ）
capital_tracker.snapshot が raw duckdb.connect で _create_tables(positions.
book 冪等ALTER)を**バイパス**→本番DBに book列無くクラッシュ。HB本番即死級。
get_connection 経由に修正＝migration自己修復も兼ねる。209緑回帰0。
教訓: 計測モジュールは get_connection（schema権威・冪等migration）を通す。
raw connect は schema 前提を満たさない旧DBで死ぬ。

### 次に同じことをする人へ
- cron投入(setup_cron_hb.sh実行)は**Grove起動**（自律recurring=不可逆点・
  KAIROS）。スクリプトは完成・構文OK・--daily本番実走検証済。
- 週次 hb_learning は hypothesis_loop(~10分)を含む＝土08:00は取引cron無し
  時間帯を選択済(衝突/枠競合回避)。
- LLM創造仮説層/業種部署council Top-Kローテ/L4 board は、このHBが実paper
  で track record を産んでから（G2を会社規模で厳守）。

## 2026-05-19 規律エンジン硬化: 監査可能・冪等・人間ゲート付き apply/revert

### 何を作ったか
`src/measurement/discipline_apply.py` ＋ `tests/test_discipline_apply.py`。
GO済仮説（hypothesis_shadow verdict=='GO'）を**人間承認付きで適用/即revert**
する決定論経路。2026 SOTA研究（MAST 2503.13657 / reward-hacking均衡
2603.28063 / Sakana・Google co-scientist 等）が「promptで代替不能・構造的に
必須」と収束証明した転用kernel中核。blueprint §0.5 P1-P4（アイデンティティ層）
＋§8（実装基盤）を Grove と段階定義し、§9/§4 S2→S3昇格ゲート③の証拠を実装。
- GOゲート（一次防壁・提案者が捏造不能）／適用Δはゲート記録値のみ（1変更+
  既知field）／単一active不変条件（別仮説適用中は拒否）／DEFAULTS(frozen)は
  `dataclasses.replace`で非破壊／revertは安全方向／append-only監査台帳。
- generator≠applier＝hypothesis_loop は本モジュールを呼ばない（適用コード無し）。

### 地雷（テストでは緑だが3観点code-reviewが捕捉＝本旨直結）
テスト9/9緑でも、レビューが「自分を騙さない」本旨に直結する実欠陥を捕捉:
- **M1**: `_latest_go` の bare `except Exception` が schema破損を「行なし」と
  **偽装**＝自己欺瞞経路。→ `duckdb.CatalogException` のみ捕捉・他は再送出。
- **M2**: 冪等が by-value で、別仮説 apply が前の active を**silent上書き**。
  → 単一active不変条件（別仮説中は refused・revert必須）。
- **H1**: 不正Δが raise（他guardは refused）＝caller契約不統一。→ refused統一。
- R1/R2: DDL split-loop idiom重複・test手書きschema drift → 単一ソース化。
教訓: **規律エンジンそのものの検証を省くな**。境界テストは通っても、
silent-mask / silent-overwrite は敵対的レビューでしか出ない。

### 検証結果（数字）
新テスト 9/9 緑（guard全網羅+roundtrip+冪等+単一active+非破壊+台帳append-only）。
全回帰 **218 passed・8 deselected・回帰0**（209→218）。engine不変・liveパス
非配線（配備はS1 GOまでゲート凍結）・frozen DEFAULTS非破壊をテストで機械証明。

### 次に同じことをする人へ
- `effective_params()` は get_connection で毎回フルDDL。**per-ticker等ホット
  パスから直接呼ぶな**（配線時は scan 単位で1回・結果キャッシュ）。docstring済。
- 本モジュールは「適用機構」であって**適用の発火ではない**。実適用は S1 GO
  （ab_preregistration 評決・既存週次cron自動）＋ Grove承認後。今は凍結維持。
- S3組織（業種/銘柄専門Agent多重化）は S2→S3昇格ゲート①(S1 GO)②(多仮説汎化)
  未達ゆえ着手不可。③(本実装)のみ証拠成立。焼損回避＝順序厳守。

## 2026-05-19 §9-1 regime深掘り WF: 結果 全PASS（門が機能＝成功）

### 何をやったか
`scripts/regime_deepening_wf.py`。本日 regime_conditional_ab で判明した機構
（alphaは弱気バケット集中・regime_filterのedge=非弱気損失回避）を受け、
regime_filter(p4_required=True, p4_threshold=0.0)から **p4_threshold のみ**
{-0.01,-0.02,-0.03} に締め、床(リスク調整後)が締まるか hypothesis_loop と
同一のWF3窓・同一cost・同一pass基準で検証。**事前登録ゲートをscript docstring
に結果取得"前"固定**（rank_validate教訓＋trace-before-alarm hook）。

### 検証結果（数字・全window trace）
3候補すべて **2/3窓=PASS（GOゼロ・不採用）**:
- 締めるほどY1/Y2(下げ・損失年)単調改善（-0.03: Y1 Sh+0.260→+0.477/
  DD-5.2→-2.2%, Y2 +0.025→+0.140/DD-22.3→-18.8%）
- Y3(強気年)は3候補とも一貫fail（決着47→19へ枯れSharpe低下）

### 地雷／教訓（むしろ規律の成功例）
「下げ年で映える改善が強気年でOOS崩壊」＝rank_validate過学習クラスの再演。
**事前登録3/3門が結果を見る前に固定されていたから正しくPASS判定でき、
curve-fit(最良閾値拾い)に陥らなかった**。binary p4(thr=0.0)は静的締めでは
改善不可と確定。No-Goを成功として出す（feedback_hype_resistance準拠）。
trace-before-alarm hook 初稼働下で「締まった」と断定せず全窓数値提示→PASS明示。

### 次に同じことをする人へ
- 動的regime化（静的閾値でなく、レジーム状態でgate/sizeを動かす）は別アプローチ。
  だが per-stock同様 過学習フロンティア＝必ず事前登録WF門の下で。
- 「2/3窓で強烈改善」は採用シグナルでなく**過学習警報**。門は先に宣言し動かすな。
- 適用は一切していない（PASS＝不採用。discipline_apply もGO以外は拒否）。

## 2026-05-19 Stage 0 リサーチ: 5分足ピボット No-Go / 本田フィルタ Go / 目標は北極星

### 文脈
本田くんとのMTGで「月30%目標(まず20%)・日足だと億単位資金で動かない→5分足
デイトレで回数稼ぐ・本田案(SMA25→EMA21+EMA900上昇トレンドゲート)」が提案。
Grove が Stage0リサーチ＋本田ハーネス構築の両方にGo。研究3本を並列委譲。

### 検証結果（証拠・収束・厳しい）
1. **短時間軸でMRエッジは生存しない**: 短期リバーサル＝流動性"供給"対価
   (Nagel, Evaporating Liquidity RFS2012)。日足は供給側になれるが5分足は
   HFT/MMが取り我々はspreadを払う需要側に反転＝エッジ反転。往復~5bps>1取引
   シグナル＝純期待ゼロ〜負。リバーサルは全アノマリー中最高回転・最小キャパ
   (Frazzini/Israel/Moskowitz)。「短足=取引増=利益増」は命名された罠。
2. **JP分足データ入手不能**: J-Quants全プラン日足のみ／立花・kabu API履歴
   なし(GH#864)／yfinance 60日上限／機関ベンダー¥10万〜100万+/年。
   容量でなく取得が決定的ブロッカー。即backtest不可。
3. **月20-30%は<1%テール**: 台湾デイトレ黒字19-20%/年・確実スキル<1%、
   ブラジル97%損失。リターン目標への最適化＝選択バイアス破綻
   (Bailey/López de Prado)。「安定>ピーク」と両立不能＝本セッション規律の
   正しさを外部文献が独立追認。
4. **本田トレンドフィルタはカテゴリPROVEN**（標準形=200日SMAレジーム
   フィルタ）。効果は主にDD/裏目削減=安定方向（増益でない）。EMA21/EMA900
   slope具体値は文献裏付けなし=researcher-degrees-of-freedom=事前登録
   ゲート必須・21vs25/800vs900で激変なら不採用。

### 判断（記録）
- 5分足デイトレ転換（同シグナル高速化）= **No-Go（今）**。罠＋データ不能。
- 月20-30% = 最適化目標にしない。逆算の北極星＋ゲート式マイルストーンのみ。
- 本田トレンドフィルタ(H2) = **Go**（承認済ハーネス・安定整合・証拠支持）。
- 将来intradayの唯一防御角度 = maker型(受動指値・spread取る側)、未証明
  フロンティア＝遥か後段・自前shadow要。spread-crossing高速化ではない。

### 次に同じことをする人へ
- 「回数を増やせば儲かる」は最強の直感的罠。Nagel/Frazzini を先に読め。
- ピボット提案はまず Stage0(エッジ生存・データ・容量)研究→Goは規律的に正しかった
  （焦って分足ハーネスを作る前に文献が罠と教えた＝研究先行の価値実証）。
- 本田ハーネスは日足のまま（EMA21/EMA900も日足指標）。intradayと混同しない。

## 2026-05-19 本田戦略ハーネス: 自己欺瞞ガード発火 → a1(5年backfill)決定

### 何をやったか
本田くん確定スペック（EMA21乖離バンド×EMA900上昇ゲート・非BNF・損切=乖離拡大
−stop_thr）で `scripts/honda_strategy_wf.py` 構築（standalone・engine不変・日足・
事前登録ゲートdocstring固定・新戦略ゆえG3非転移の自前絶対基準）。

### 検証結果（ガードが正しく発火＝成功）
`EMA900履歴十分性: 有効 0 / 不足除外 1337 / universe 1337`。
EMA900成立に~925日(≈3.7年)warmup要、3年cache(~730日)では全銘柄不足。
**ハーネスは半端EMA900で偽backtestを作るのを拒否**＝§9-1/Stage-0と同型の
自己欺瞞ガード成功。戦略の良否でなく「現データで検証不能」が正直な結論。

### 判断（Grove決定）
- 本田方向を曲げない＝EMA200代理(b)却下（別戦略検証→頭で転移=自己欺瞞）。
- **a1: J-Quants Light(無料)で日足cache 3→5年backfill**。但し正直な制約:
  5年でもEMA900は各窓前に~3.7年warmup要→**実質Y3窓のみ検証可・3/3頑健は
  構造上不可**。3窓フルは~10年=Standard¥3,300/月。Y3有望時のみ段階escalate。
  検証強度の限界を「EMA900は信じるが証明されない成分」と正直ラベル。

### 地雷／教訓（前セッション「494 stale」と同型・読んで回避）
`fetch_and_cache` の incremental skip は MAX(date)のみ見て**前方追記専用**。
naiveな years=5 は fresh銘柄をskip＝**silent no-op**（backfillしたつもりが
旧史掘れず＝「494」と同型の自己欺瞞）。**force=True 必須**（start=今-5年・
INSERT OR IGNOREで旧史のみ冪等追加）。仮定せず実コードを読んで回避＝
trace-before-alarm/read-before-implementの価値実証。daily_update(cron)は
不変（前方維持は別関心事）。`scripts/backfill_5yr.py` 実装・bg実走中。

### 次に同じことをする人へ
- backfill完了後 `scripts.honda_strategy_wf` 再実行＝有効銘柄数/窓を再確認。
  Y3のみ有効想定。3/3頑健は主張するな（構造的に不可・低信頼と明示）。
- EMA900(~3.6年)は retail入手可能履歴ではWF検証力が本質的に弱い
  ＝research#3「EMA900は文献裏付け薄・evidenced標準は200日」をデータが追認。
- 「方向を曲げない」と「検証強度の限界を正直表示」は両立する。代理で曲げない。

## 2026-05-19 本田戦略 WF再実行（5年cache）: 0/3 No-Go・破滅DD・"大炎上"実証

### 経緯
a1 backfill成功（date_min 2023-04→2021-05・rows 1.15M→1.78M・err0・
EMA900成立 0→1,303銘柄）。harness再実行で日付型バグ（datetime64 vs date・
3年時は0銘柄で未踏だったコードパスがデータ充足で初露呈）→`pd.Timestamp`比較に
修正→再実行。

### 検証結果（trace済・全8config×3窓）
**8config 全て PASS(0/3)。破滅的DD −73〜−100% 全config全窓。**
Y2(損失年)は全configでSharpe負。取引数 8千〜2.7万/窓。事前登録ゲート
(3/3窓 sharpe>0 & maxDD>−35%)を1つも満たさず＝決定的不合格。

### 結論（No-Goを成功として・正直）
- **本田スペック as-is は不採用**。方向を曲げた結果でなく、本田方向を忠実検証
  した結果**欠けピース＝硬いリスク制御（絶対ハードストップ/サイジング/同時
  保有上限）が露呈**。＝本田くんが議論冒頭で恐れた「相場が死んでるとき大炎上」
  が、EMA900ゲート＋EMA21乖離stopだけでは実際に起きると実証。
- **harness magnitude の正直な限界**: 単一保有/銘柄・サイジングなし・cumprod
  equity ＝ −100%の精密値はharness構成アーティファクト（実portは全シグナルに
  逐次フルベットしない）。**頑健なのは方向**（0/3・Y2負・最有効Y3窓でも自壊）、
  精密な数値ではない。trace-before-alarm両刃＝負も過度に断定しない。

### 地雷／教訓
- 「データが出来て初めて露呈する未踏コードパス」: 3年時0銘柄で`_trades`未実行
  →backfillで初実行し型バグ発覚。**ガード成功で空振りした分岐は未検証のまま
  ＝後で必ず通る前提でテストせよ**（discipline_apply M1/M2と同類）。
- 専門家spec(本田)も忠実検証→事前登録ゲートが客観的にNo-Go。物語でなくゲート。
  「方向を曲げず検証→欠けピースを実証」は忖度と規律の両立の実例。

### 次に同じことをする人へ
- 次は (a)硬いリスク制御層を足し事前登録し再ゲート / (b)spec見直し のGrove/
  本田判断待ち。harnessを「数値が出た」と逆手に取るな（0/3＝不採用が結論）。
- crude harnessの破滅DDは「精密な損失額」でなく「自壊の方向シグナル」。
  magnitudeを引用するな、方向（No-Go）を引用せよ。


## 2026-05-20 本田MTG bridge 実装 → F+G audit → BNF base EDGE_NULL 判定

### 経緯
前回ハンドオフ後、Grove「本田MTGの部分が生きないかな」を起点に bridge設計。
3 Honda 由来 single-axis hypothesis を hypothesis_loop に登録:
- `extended_hold` (max_hold_days 15→25): 「持つ時間長く」
- `asymmetric_tp` (take_profit_dev 0→+0.02): 「1%入→2%で逃げ」
- `ema900_uptrend` (per_stock_uptrend_required True): 「EMA900右肩上がり」

実装: engine.py に conditional kwarg として overlay（default で挙動完全保存・
G3 golden 不変）、strategy_params.py に 1 field 追加、hypothesis_loop.py に
G3 拡張・registry 追加・insufficient_warmup sentinel 経路追加。
discipline/strategy_params/hypothesis_loop tests 20/20 緑。

### bridge walk-forward verdict（hypothesis_loop 1 job）
6 candidate 全 verdict 記録。Gate② GO累計 = 1 (regime_filter のみ未変・既存)。
本田由来3候補 全 PASS（1/3 or 0/3 窓）。Y1 bullish ★・Y2/Y3 flat or 悪化の
非対称 pattern。

### F+G audit（gemini deep_research + Plan agent 敵対的レビューが収束）
2独立ソースが同じ結論: 「BNF base はコスト後にノイズと統計的区別不可能」。
- N=503・mean +0.60%・Sharpe/trade 0.0645
- **Bootstrap 95% CI on Sharpe: [-0.024, +0.148]** = ★ゼロを含む
- PSR = 0.932 < 0.95 / DSR (k=6) = 0.576 << 0.95 = **fail**
- ∴ verdict **EDGE_NULL**

副産物（重要）: consensus stratification が calibration を示す。
consensus=3 (388 trades, 77%) Sh 0.009 = ノイズ / **consensus=4 (109 trades, 22%) Sh 0.145 = edge あり** /
consensus=5 (6 trades) Sh 0.750（sample 不足だが強）。
**信号強度→期待リターンの単調関係**＝base nullの原因は consensus=3 の希薄化。

G (regime-stratified Δ): Honda 3候補とも uniform-improvement なし。
`ema900_uptrend` は Plan agent 予測通り **pure regime artifact**（bullish のみ +）。

### 実装ファイル
- `src/backtest/engine.py` (kwarg + L266 take_profit_dev 配線 + L116-134 ema900列 + L194 warmup guard + L311 entry_ok AND句)
- `config/strategy_params.py` (`per_stock_uptrend_required` field)
- `src/measurement/hypothesis_loop.py` (G3拡張・registry 3追加・_metrics 拡張・insufficient_warmup sentinel)
- `scripts/fg_audit.py` (新規・F+G 統一監査・bootstrap CI・DSR・regime stratification)
- `docs/grove-stock/bnf_base_audit_2026-05-20.md` (kill-gate + consensus-tighten 事前登録)

### 教訓
- **「engine immutable + conditional overlay」パターンは安全**: p4_required の前例通り、default で挙動保存して overlay を 3つ追加できた。回帰0
- **本田MTG bridge という frame 自体が論点を見誤らせていた**: 「Gate② velocity の追加 hypothesis」と思って始めたが、F audit で「BNF base 自体が null」が露呈。論点は「どの hypothesis を試すか」でなく「base に edge があるか」だった
- **gemini deep_research + Plan agent の 2独立敵対的レビューが同じ結論に収束した時の信頼性は高い**: 単独source なら overconfident かもしれない判定を、独立収束が裏付ける。今後 frontier-design 領域で標準パターンに
- **Lopez de Prado の DSR は multi-test 補正に必須**: k=6 でも閾値が大きく inflate。registry が 12+ になれば false-discovery risk が ~18% に達する（Plan agent quantified）。Guardian の per-hypothesis surrogate prevention は registry-level HARKing を防がない
- **`ema900_slope_up` 警戒期 guard の bug**: pandas で `NaN > NaN = False` なので bool 列が False で埋まり `.notna().any()` が常に True で発火せず。Y1 が「no_data」になり「insufficient_warmup」明示 sentinel に至らなかった。Fix: guard を `df["ema900"].notna().any()` に。verdict は変わらない（Y1 でも entry 0 で PASS）

### 次に同じことをする人へ
- **新 hypothesis 追加前に F audit を再走せよ** (k 値が増えるたび DSR 閾値が
  inflate する。multiplicity を意識せず registry を膨らませると HARKing に
  陥る)
- **base の有意性を測らずに上に hypothesis を積むな**: null base への lift は
  null+lift で結局 null になりやすい。F audit (~30min) は新 hypothesis 1本の
  cost より安く・dispositive
- **kill verdict は code change のtrigger ではない**: discipline_apply の
  Guardian が GO-gated apply を守る。kill は「default は edge を持たない」
  という interpretation の凍結であり、強制 flip ではない
- **Grove の「何が最適だと思うか」には sequential-thinking + deep_research +
  Plan agent の 3並列で対応せよ**: フロンティア設計領域では menu や即答でなく
  研究接地が常に勝つ（feedback_frontier_design_research）

---

## 2026-05-20: decision_shadow 配線実装（LIVE A/B 記録機構稼働開始）

### 経緯
前回 session 終盤に「`decision_shadow` table = 0 records」を発見。regime_filter LIVE A/B（hypothesis_shadow id=7, verdict=GO, applied=false）の評価記録が物理的に accumulate されていない可能性。今 session でこれを最優先で解明。

### 真因（3並列 Explore で断定）
- `decision_shadow.record_proposal()` の **本番コード上の呼び出しが完全にゼロ**。テスト（test_measurement.py）にしか呼び出しがない。
- `backfill_counterfactuals()` は cron 15:45 hb_learning --daily で正常稼働しているが、UPDATE 対象行が無い（INSERT が無いため）。
- 結果: kelly_node の go/pass 判定が DB に残らず、kill-gate（2026-09-30）も consensus-tighten LIVE A/B（2026-11-30）も評価不能。

### 実装
- `src/main.py` kelly_node:
  - 同時保有上限 early return の **前** に consensus≥3 全 ticker を `max_concurrent_full` reason で pass 記録（A/B評価のため）
  - ループ内: regime_filter_skip / already_open / no_market_data / kelly_ok / shares_zero の 5 経路で record_proposal を発火
  - slots_full break は記録しない（A/B 比較と無関係）
- `src/voting.py`: vote_all の返り値に `votes["dev"] = ma25_dev` を追加。kelly_node 側で `edge` フィールドに使用
- `tests/test_scan_graph.py`: `TestDecisionShadowWiring` クラス 5 ケース追加（go / regime_filter_pass / multibook 同 ticker / max_concurrent_full / consensus<3 not recorded）

### 検証結果（実測）
| 指標 | Before | After |
|---|---|---|
| decision_shadow 総件数 | 0 | 990 (= 198 シグナル × 5 books) |
| edge 埋まり率 | N/A | 100% (990/990) |
| consensus 分布 (go予備) | N/A | c=3: 590 / c=4: 340 / c=5: 60 |
| pytest test_voting/test_scan_graph/test_measurement | 33/33 | 38/38（5 ケース増） |
| Golden test_c3_baseline_frozen_g3 | 901 vs 555 fail | 同（pre-existing fragility・本変更と無関係） |

### 想定外の発見と訂正
- **当初仮説「monitor exit 未発火で 35件 stuck」は誤りだった**。実測で:
  - 過去30日 exit 27件（signal_reversal 24 + stop_loss 3、avg pnl +¥28,855、平均 hold 1.7日）
  - 5/17 entry 17件 → 既に 14件 closed（残 3）/ 5/18 entry 14件 → 既に 10件 closed（残 4）
  - 本日 5/20 で 27件 fresh entry = システムは active に取引
  - **monitor は完全に正常稼働。35件は直近3-4営業日の正常な蓄積（hold 1.7日 × 5book × 7枠 ≈ 35）**
- **decision_shadow 0 records の真因も別だった**: max_concurrent_full は本日午後の手動 paper run でだけ発生。本日 9時 cron では旧コード（record_proposal 未配線）で 27件 entry されたため記録ゼロ。私の修正が 12:30 過ぎに deploy されたので、それ以降の手動 paper run では既に枠埋まりで max_concurrent_full のみ記録された
- **明日朝 9時 cron 以降が真の稼働開始**: 新コードで枠空き状態から kelly_node に到達 → kelly_ok / shares_zero / regime_filter_skip / already_open / no_market_data の全 reason で記録が始まる
- **feedback_no_hypothesis_as_fact_discovery 違反**: 前 session で「decision_shadow=0 → LIVE A/B 未稼働」と書き、本 session で「max_concurrent_full → monitor exit 未発火」と更に断定した。両方とも仮説を事実として fixate していた。各 step で `positions WHERE status='open'` の entry_date 分布と `exit_reason` 分布を grep する trace を最初にやるべきだった

### 教訓
- **LIVE A/B「未稼働」と「未配線」は別問題**: hypothesis_shadow が GO/applied=false でも、それは「DEFAULTS を flip しないという規律」を意味するだけで、**判断記録の配線抜け** を含意しない。前回 session で「decision_shadow 0 = LIVE A/B が動いてない」と書いたが、実態は「配線が無い」だった。検査の解像度を「テーブル空 → 何が呼ばれていないか grep」まで落とすべきだった
- **早期 return パスも記録対象**: 当初の Plan では「max_concurrent (枠制約) は A/B 無関係なので記録しない」と書いた。しかし実態は **全 book が枠埋まり** → 早期 return → ループに到達せず → 全シグナル記録漏れ。「枠制約は無関係」は理論上正しいが、現状の system state では「枠制約しか効いていない」ため、ここを記録しないと A/B が走り出すまで decision_shadow が空のままになる。実測が plan を上書きした例
- **voting 出力に `dev` を追加するだけで A/B stratification の解像度が大きく上がる**: 既存 scans テーブルには ma25_dev が記録されていたが kelly_node には伝達されておらず edge=None になっていた。1 行追加で 990 行すべての edge が埋まり、consensus stratification と組み合わせて F audit 副産物（c=3 noise / c=4 edge / c=5 strong）の検証が可能になる

### 次に同じことをする人へ
- decision_shadow に行が入り始めたので、明日以降の hb_learning --daily cron が cf_* を埋め始める。**1 週間ほど待ってから集計を回せ**（cf_exit_date が exit_rule で確定するまで時間が必要）
- 集計時は `(ticker, decided_date, consensus)` で control（p1m/p10m/p50m）と treatment（p5m/p30m）を JOIN し、`regime_filter_skip` と `kelly_ok/shares_zero/max_concurrent_full` の cf_pnl_pct 分布を比較せよ
- **monitor exit 発火問題 を先に解決すべし**（前 session Next #1 の派生）: 35 件 stuck positions が close しない限り全 book が max_concurrent_full のままになり、本来の go / regime_filter_pass の比較ができない
- **冪等性なし**: 同日2回 main.py 実行で 2 倍記録される。cron 1日1回前提だが、将来 UNIQUE 制約を別 Issue で追加すべき

---

## 2026-05-20 (続): ema900 guard fix + G3 fragility partial fix

### ema900 warmup guard bug fix（完了）
- `src/backtest/engine.py:219` — `df["ema900_slope_up"].notna()` → `df["ema900"].notna()`。
  pandas が NaN比較を False に解決する仕様で、警戒期中の slope 列が bool で埋まり
  guard が発火しない bug を修正。
- `tests/test_backtest.py` — `test_ema900_slope_up_returns_bool_not_nan_in_warmup`
  追加（regression test として pandas semantics と guard 意図を pin）
- 検証: hypothesis_loop 再走で **Y1 = "insufficient_warmup"** sentinel が正しく
  発火（前回 "no_data" だったのが明示記録に）。regime_filter は 3/3 GO で前回と
  整合（前回 verdict 不変）

### G3 golden test fragility fix（**partial・root cause 残り**）
- `src/backtest/runner.py:22` — `compute_sector_thresholds_from_cache` に
  `as_of_date` 引数を追加。`load_bars(end=as_of_date)` で過去固定
- `tests/test_characterization.py:178` — `as_of_date=self.BASELINE_END` を渡す
- **検証結果**: 依然 901 vs 555 で fail。sector_thresholds 部分は drift から
  保護されたが、trade-count drift の **真の root cause は別**だった
- 想定 root cause:
  - `universe_snapshot` table が時系列で再生成されている可能性
  - `daily_quotes` 過去 bar が後日 update（株式分割訂正等）されている可能性
  - `_load_nikkei_bars()` が end_date を受けず Nikkei MA25 が drift する可能性
- 深掘りには cache audit が必要で別セッション。Out of Scope に降格

### 教訓
- **fragility fix は「容疑1つを潰して PASS にならなければ、その容疑は主因ではない」**: sector_thresholds drift を疑って fix したが test は依然 fail → 主因は別所。確信度の低い修正は「PASS を試金石にして容疑を消す」プロセス
- **as_of_date 引数の追加自体は害は無い**（既存呼び出しに影響しない default=None）。次セッションで root cause が判明したらこの引数も使い続ければよい
- **Plan handoff §9 の「5min」見積もりは過小**だった。前 session で「sector_thresholds drift」と原因を断定していたが、それは仮説に過ぎなかった（feedback_no_hypothesis_as_fact_discovery）

---

## 2026-05-20 (続2): G3 root cause 実測ベース究明（feedback_debug_trace_before_root_cause 適用）

### 経緯
新規 feedback memory `feedback_debug_trace_before_root_cause` に従い、G3 trade-count drift の真の root cause を実測ファーストで究明。「事実→仮説→検証→断定」の順を厳守。

### 仮説 4 候補と実測検証
| # | 仮説 | 実測 | 判定 |
|---|------|------|------|
| 1 | universe_snapshot が時系列で増加 → trades 増 | `SELECT count(*) FROM universe_snapshot` = 1337 (安定) | **deny** |
| 2 | daily_quotes の過去 bar 後日 update (mutation) | 5/15時点 1773177 rows / 現在 1775850 rows (差分 4営業日分のみ) | minor |
| 3 | `_load_nikkei_bars()` の `period="5y"` が moving window で reproducibility なし | コード読了で確認、yfinance に依存 | **strong suspect** |
| 4 | sector_thresholds 修正が無効化されてる | as_of_date あり vs なしを比較、30/32 sectors で diff < 0.01% | **minor impact** |

### Nikkei fix 試行 → ロールバック
- `_load_nikkei_bars(end_date)` を実装し `t.history(start="2021-01-01", end=end_date)` で固定 window 化
- 検証実行で yfinance が `^N225: possibly delisted; no price data found` エラー → fetch failure
- **ロールバック**: 本番 cron での Nikkei 取得を壊さないため moving window に戻した（コメントで「真の解決には DuckDB 永続 cache が必要」と明示）

### 暫定状態
- `as_of_date` 引数（runner.py + test_characterization.py）は **keep**: 副作用なく minor improvement
- G3 test は依然 `901 vs 555` で fail
- **真の root cause は仮説 3 (Nikkei moving window)** が最有力だが、yfinance API 制約で固定 window 化できず

### Out of Scope（別セッション）
- Nikkei bar の DuckDB 永続 cache 化（`nikkei_bars` table 新規作成 → `_load_nikkei_bars(end_date)` で SELECT）
- 仮説 3 が解消された後も fail なら、daily_quotes 過去 bar mutation の audit が次容疑

### 教訓
- **feedback_debug_trace_before_root_cause が早速効いた**: 仮説 4 つを列挙し並列で実測 → 主因の絞り込みに成功（前 session のように「sector_thresholds が原因」と即断定しなかった）
- **fix 試行で本番を壊しかけたら即ロールバック**: yfinance fetch failure を見た瞬間に「reproducibility 改善目的の修正が本番 cron 障害を引き起こす」と判断、即ロールバックした
- **「修正で PASS にならなければ容疑を消す」プロセスが機能した**: sector_thresholds → as_of_date 修正後も fail → 主因ではない、と確定できた

---

## 2026-05-20 (続3): Nikkei DuckDB 永続 cache 化 → 仮説3 deny

### 実装
- `src/data/cache.py` — `nikkei_bars (date PRIMARY KEY, close DOUBLE)` テーブル追加、`fetch_and_cache_nikkei(years=5)` で yfinance→ INSERT OR REPLACE、`load_nikkei_bars(end_date)` で SELECT
- `src/backtest/engine.py:141` — `_load_nikkei_bars(end_date)` が cache 優先で読む。cache miss or 14日以上古ければ yfinance fallback。型ミスマッチ (Timestamp vs date) も pd.Timestamp で揃えて修正
- 初回 backfill: **1222 rows** (2021-05-20 〜 2026-05-20)

### G3 仮説3 検証結果: **deny**
- Nikkei cache 経由で `end_date="2026-05-15"` 固定 window 化したが test_c3_baseline_frozen_g3 は依然 **901 vs 555**
- **Nikkei moving window は drift の主因ではなかった**
- 残る strong suspect: 仮説2 (`daily_quotes` 過去 bar mutation) — 直接検知には過去 cache の snapshot との比較が必要で本 session scope を超える

### 副次的 benefit (keep する理由)
- yfinance API 障害時の reproducibility 確保（cache 経由で安定）
- 本番 cron の Nikkei fetch 回数削減（cache hit で yfinance 呼び出し回避）
- 将来 daily_quotes mutation の真相究明時に Nikkei 部分は固定済みなので測定が容易

### Regression test
- pytest test_voting + test_scan_graph + test_measurement + test_backtest: **41/41 全 PASS**
- Nikkei cache 化が既存テストの挙動を変えていないことを確認

### 教訓
- **「修正で PASS にならなければ容疑を消す」プロセスが再度機能**: Nikkei cache 化しても 901 のまま → 仮説3 deny。これで残る容疑は1つに絞れた
- **副作用なしの fix は keep**: 主因究明には繋がらなくても、reproducibility 改善や本番安定化に寄与するなら keep する（Nikkei cache 化が好例）
- **次セッションで daily_quotes mutation audit**: 5/15 baseline 時点の bar 状態を再現する snapshot が無いので、現時点 cache を「snapshot ファイル」として dump → 後日比較できるようにしておくべき

---

## 2026-05-20 (続4): G3 test 901 vs 555 の **真の root cause 確定** （commit 8af3315 内の p4_required semantics 変更）

### 発見
仮説 1-4 全部 deny → 仮説 5「test 設計時 (5/17) と現在 (5/20) の間の git commit 確認」で answer 確定。

git log で 5/15 以降 backtest engine への commit は **8af3315 のみ**。commit 内 diff:

```diff
- if consensus >= consensus_min and p4:
+ entry_ok = (consensus >= consensus_min
+             and (p4 or not p4_required)
+             and (not per_stock_uptrend_required or bool(row.get("ema900_slope_up", False))))
+ if not entry_ok: continue
```

### Semantics 変化
| 呼び出し | 旧 entry 条件 | 新 entry 条件 |
|---|---|---|
| 既定 | `consensus≥3 AND p4` | `consensus≥3 AND p4` (p4_required default=True で同じ) |
| **test_c3 (`p4_required=False`)** | `consensus≥3 AND p4` (旧は引数不在) | **`consensus≥3` のみ** (p4 が無視される) |

→ test_c3 が `p4_required=False` で呼ぶことで、entry 条件から p4 が消え trade 数が急増 (555 → 901)。

### 前 session の誤認
handoff §6/§8 で「pre-existing test fail: test_c3_baseline_frozen_g3 901 vs 555 (cache drift)」とラベル → **誤り**。cache drift ではなく commit 8af3315 内の意図しない semantics 変更だった。前 session 自体が commit でこれを起こしたが、handoff 時点で「conditional overlay 安全パターン」「golden を壊さなかった」と書いており、test_c3 への影響を見落としていた。

### 仮説検証の旅 (まとめ)
- 仮説1 (universe): deny（1337 安定）
- 仮説2 (daily_quotes mutation): mutation 0（snapshot vs cache 全一致）
- 仮説3 (Nikkei moving window): deny（cache 化しても 901 のまま）
- 仮説4 (sector_thresholds 修正効いてない): minor impact（30/32 sectors diff < 0.01%）
- **仮説5 (commit semantics 変更): CONFIRM**

### 判断保留 (要 Grove)
3つの選択肢:
A. test 期待値を新 baseline (901 trades) に更新 — 「semantics 変更は intentional だった、旧値は obsolete」
B. commit 8af3315 を partial revert — `p4_required` 引数を削除し旧挙動に戻す（影響範囲大・honda bridge 機能損失）
C. test 呼び出しを `p4_required=True` に変更 — 旧 semantics を再現（test の意図に合致？）

Grove に状況報告して判断を仰ぐべき。

### feedback_debug_trace_before_root_cause の価値
仮説4つを実測で deny した後、最後の仮説5「git log」で 1 commit を発見。**「修正で PASS にならなければ容疑を消す」プロセスを最後まで貫いて、真因に到達できた**。前 session のように「cache drift」で fixate していたら永久に主因を見逃していた。

---

## 2026-05-20 (続5): G3 baseline 新値で pin (901 trades) — Grove 選択肢 A 採用

### 変更
- `tests/test_characterization.py:170-198` — test_c3_baseline_frozen_g3 の全 expected を新 baseline で pin:
  - total_trades: 555 → **901**
  - closed: 548 → **894**
  - wins/losses: 290/258 → **482/412**
  - win_rate: 0.5291... → 0.5391...
  - avg_return: 0.0045... → 0.00241...
  - sharpe_per_trade: 0.0492 → **0.0287**（コスト後・新 entry 条件下）
  - max_drawdown: -0.2877 → **-0.2649**
  - exit_reasons: {tp:246, sl:169, hold:133} → {tp:412, sl:267, hold:215}
- `tests/test_characterization.py:107-122` — test_bearish/bullish_regime_golden で `votes["dev"]` schema 拡張に対応（dev を separately 検証、コア assertion は dict 比較から除外）
- BASELINE_END=2026-05-15 + as_of_date 経由で sector_thresholds と Nikkei cache の drift 保護を keep

### 検証
- `pytest tests/test_characterization.py`: **17/17 PASS**
- `pytest tests/`: 230/232 PASS（2件 fail は cron で走行中の `main.py --paper` (PID 8356) との DuckDB lock 競合、私の修正と無関係）

### 経済的意味（参考）
新 baseline (p4_required=False = p4 を緩和) では:
- trades 量は +62%（555 → 901、p4 無視で entry 機会増）
- win_rate は微増（52.9% → 53.9%）
- **avg_return は -46%**（0.45% → 0.24%/trade、p4 緩和で質の悪い trade も混入）
- sharpe_per_trade は -42%（0.049 → 0.029）
→ p4_required=False は「量増えても質落ちる」を確認。本田 MTG bridge の改善余地と整合。

### 教訓
- **「pre-existing fragility」と diagnose した時こそ、自分の commit を疑う**: 前 session で「cache drift」とラベルしていたが、実際は自分の commit 8af3315 内の semantics 変更。Plan handoff §11 の成長スコア 12/12 中、ema900 warmup guard の bug を「verdict 不変だから影響なし」と判断したが、**同じ commit 内で test_c3 を壊す変更も入っていた**ことに気付かなかった。次回からは「conditional overlay 完了」と言う前に、既存の characterization test 全部を `p4_required=False` 等の non-default 引数で確実に通すこと
- **A 選択 (test 更新) が正解だった理由**: B (revert) では honda bridge の機能損失、C (`p4_required=True`) では旧 semantics と不一致で test の意図がぶれる。A は「新 semantics 下で realistic baseline を pin」という characterization test の本来の意義に合致

---

## 2026-05-20 (続6): decision_shadow UNIQUE 制約導入（冪等性確保）

### 実測（実装前）
- 総 1991 行 / unique 1001 → **990 行の重複**
- 12:50 (手動 paper run) + 15:00 (cron 15時 scan) で同 (ticker, book, decided_date) ペアが 2 回記録
- 1日 3 回 scan (9時/12時/15時) で最大 3 倍記録される設計欠陥

### 実装
- `src/measurement/decision_shadow.py:30-49` — schema に `UNIQUE (ticker, book, decided_date)` 追加
- `src/measurement/decision_shadow.py:record_proposal()` — INSERT → **ON CONFLICT DO UPDATE**。
  cf_* 系列は backfill_counterfactuals が別途埋めるため UPDATE 対象外
- `scripts/migrate_decision_shadow_unique.py` — 既存 DB の 1回限り migration:
  1. 重複行 dedupe (最新 id を残す)
  2. 新 table 作成 (UNIQUE 制約付き) → INSERT SELECT → DROP/RENAME
  3. id seq を max_id+1 に reset
  4. 冪等: 既に UNIQUE があれば skip
- `tests/test_measurement.py` — 2 ケース追加:
  - `test_record_proposal_is_idempotent_on_same_key`: 同 key 2 回呼びで 1 行のみ、最新値が残る
  - `test_record_proposal_different_book_separately_stored`: 同 ticker × 同 date でも book 違えば別行

### 検証
- migration 実行: **1991 → 1001 rows (990 重複削除)** + UNIQUE 制約 enabled
- pytest test_measurement + test_scan_graph: **29/29 PASS** (新規 2 件含む regression なし)
- DB query で UNIQUE constraint 検出: `UNIQUE(ticker, book, decided_date)` 有効

### 意味論（同日内の判断遷移）
ON CONFLICT DO UPDATE で「最新の判断」を残す設計。同日内で position 状態が変わる場合の例:
- 9時 scan: kelly_ok で entry → `decision=go, council_reason=kelly_ok`
- 12時 scan: 既に open position → `decision=pass, council_reason=already_open` (新値)
- → 最終的に DB に残るのは 12時の判断 (実態と一致)

### 教訓
- **「冪等性」を後付けすると migration が必要になる**: 最初から UNIQUE 制約を入れていれば row id seq の reset 等の手間は不要。新規 table 設計時に「同日複数回呼ばれる前提」を考えて key 設計すべき
- **schema 変更 + migration script + idempotent flag は組合せる**: migration を `_connect()` で毎回走らせるとコスト高 → 別 script + `duckdb_constraints()` で既に存在チェック (冪等)

---

## 2026-05-21: Phase 0+ EVS/PLT サイジング刷新（Grove刷新後）

### Grove要求の根本転換
旧 EVS 設計 (4 factors / 固定 cap / Phase 1 先送り) は「保守的すぎ・最大効率探索してない」と批判 → **「動的にテスト・成績表で動く・1-10段階」** で再設計

### v2 アーキテクチャ (5 layers)
1. **10 factor EVS** (F1-F10): consensus / deviation / RSI / BB / volume / sector-winrate / market regime / vol filter / concentration / liquidity
2. **Pattern Lookup Table (PLT)**: 6軸 × 432 セル成績表。Beta-Bernoulli shrinkage + Quarter Kelly fraction
3. **Router**: PLT 主軸 / EVS cold-cell fallback / ε-greedy exploration (20% → 10%)
4. **decision_shadow v2**: 10 features + cell_id + cap_pct + sizing_source 全保存
5. **kelly_node 統合**: 7 record_proposal 経路すべて router 経由

### ファイル変更 (8 files / +2,800 行)
- `src/sizing/evs.py` 新規 (350行) — 10-factor scoring + cap function
- `src/sizing/plt.py` 新規 (310行) — bin化 / aggregate / persistence
- `src/sizing/router.py` 新規 (180行) — PLT lookup + ε-greedy dispatcher
- `src/main.py` +200行 — kelly_node router統合 + helper関数群
- `src/measurement/decision_shadow.py` +30行 — schema v2 (9 新規カラム)
- `scripts/bootstrap_plt.py` 新規 (140行) — 既存25 trades から初期PLT構築
- `scripts/migrate_decision_shadow_v2.py` 新規 (75行) — 冪等migration
- `tests/test_evs.py` 新規 (340行 / 57 tests), `test_plt.py` 新規 (220行 / 36 tests), `test_router.py` 新規 (180行 / 10 tests)

### 数字（実測）
- **Test pass**: 152/152 (test_multibook + test_scan_graph + test_measurement + 新3 suites)
- **Bootstrap PLT**: 25 closed trades → **6 unique cells** (拡張前は 4 cells)
- **End-to-end dry-run**: 69200 entry で EVS=0.217, cell="c4_d1_r1_b0_bear_tech", cap=22.99%, position=800株
- **TICKER_SECTORS 拡張**: 13 → 50+ ticker mapping (J-Quants 5桁形式対応)

### 反直感的発見 (PLT bootstrap)
- `c3_d1_r1_b0_bear_other` (consensus=3 で大幅乖離) n=8, win率=25%, avg_pnl=-2.1%
- **「強シグナル = 高勝率」の仮説は実データで否定された**
- Beta(5,3) shrinkage で Kelly fraction = 0 → cap=floor(10%) で empirical自己防衛機構が機能
- Phase 1 で scipy 重み最適化を回すと F2(deviation_depth) が負の重みになる可能性

### 教訓
1. **「設計時の仮説を必ず実データで検証する」**: 既存 27 closed が「強いシグナル ≠ 勝つ」を証明 → 仮説固定だったら本番で重い損失
2. **「PLT主軸 + EVS fallback」の Grove 判断は正解**: 旧 EVS 単独だと「強信号→高 cap」で逆方向に大量資金投入。PLT が成績表で自動制御
3. **「データ生成パイプライン稼働 > 機構の完成」**: 本番に乗らないコードはデータを生まない＝学習しない。火曜朝 cron 投入を最優先

### Next Step
- 火曜朝 cron で稼働 → 5book × 数十候補/日のデータ生成開始
- 来週金曜 (n+50) で初回 scipy 重み最適化試行 (Phase 1)
- weekly_retrain cron 設定
- test_oss_smoke の max_drawdown baseline 更新 (前 session 漏れ)
