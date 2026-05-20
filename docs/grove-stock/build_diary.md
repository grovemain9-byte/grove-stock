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
