# BNF式MA乖離率逆張り — Spec v2（最終版）
> Tier 1 戦略 | スイングトレード | 親: `../../00_master_strategy_tree.md`
> 更新: 2026-04-07（設計議論完了・実装前確定版）

---

## 戦略の一言説明

日足MA25から大きく下落した日本株が、均衡水準に戻る動きで利益を取る。
BNF（ジェイコム男）が資産1億達成まで使っていた逆張りスイングトレードをAIエージェントで自動化する。

---

## 設計上の重要な決定事項（変更禁止）

1. **MA25は日足ベース** — 30分足cronだが、計算するMA25は日足25日移動平均
2. **スイングトレード** — 最大5営業日保有。デイトレ15:15強制決済は廃止
3. **5プレイヤーのみ** — 10指標から独立性を重視して5つに絞った
4. **損切り-5%統一** — セクター問わず、エントリー価格から-5%で即損切り
5. **Q学習・Reflexion廃止** — 既存DBのスキーマに残っているが実装しない

### なぜこの設計か（背景）
- 2週間の稼働実績より: 3/25に中外製薬(4519)-13.3%でシグナル発生 → 4/2に+6.8%回復
- 回復まで8日かかった → デイトレで収益化不可能、スイングが適切と確認
- シグナルの質は正しかった。問題は「配管」（DuckDB未書き込み・注文未接続）のみ

---

## LangGraphノード設計

```
[START: 30分cronトリガー（市場時間のみ）]
   │
   ▼
[Ingestion Node]
  - J-Quants APIまたは立花証券APIで13銘柄の当日価格・出来高を取得
  - 日足MA25を計算して乖離率を算出
  - DuckDB scansテーブルに書き込む
  - P4用に日経225の当日騰落率も取得
   │
   ▼
[Voting Node]  ← 今回の核心
  - 5プレイヤーをasyncio.gather()で並列実行
  - 各プレイヤーはvote(df, sector) -> bool を返す
  - consensus = sum(votes)
  - DuckDB scansテーブルにP1〜P5の結果とconsensusを更新
   │
   ├─ consensus < 3 → ログのみ → [END]
   ├─ 月次ドローダウン-10%超 → ログ + アラート → [END]
   └─ consensus >= 3 → [Kelly Node]
   │
   ▼
[Kelly Node]
  - kelly_size(consensus, balance) でポジションサイズ決定
  - 同銘柄の既存ポジション確認（重複エントリー禁止）
   │
   ├─ スキップ → [END]
   └─ 承認 → [Execution Node]
   │
   ▼
[Execution Node]
  - TachibanaClient.buy(ticker, shares) で現物成行買い
  - 約定確認
  - DuckDB positionsテーブルに記録
   │
   ▼
[Monitor Node]（30分ごとに全オープンポジションをチェック）
  - 各ポジションのエグジット条件を確認（4条件）
    ① 損切り: 現在値がエントリー価格から-5%以下
    ② 最大保有期間: エントリーから5営業日経過で強制決済
    ③ 利確: 現在値がMA25に到達（乖離率 >= 0）
    ④ 反転シグナル: P1〜P5を再実行して過半数（3以上）がFalseに転換
  - いずれか1条件が成立 → [Exit Node]（理由をexit_reasonとして渡す）
  - 全条件未成立 → 次の30分cronまで待機
   │
   ▼
[Exit Node]
  - TachibanaClient.sell(ticker, shares) で現物成行売り
  - 約定確認
  - DuckDB positionsテーブルをclosedに更新・PnL記録
  - exit_reason（stop_loss / max_hold / take_profit / signal_reversal）を記録
  - monthly_pnlを更新
   │
   ▼
[END]
```

---

## 5プレイヤーの実装仕様

全プレイヤー共通インターフェース:
```python
async def vote(df: pd.DataFrame, **kwargs) -> bool:
    """
    BUY条件を満たす場合 True、それ以外・例外時は False。
    例外は内部でキャッチして False を返す（投票棄権扱い）。
    dfは対象銘柄の日足OHLCV DataFrame（少なくとも30日分）。
    """
```

### P1: MA25乖離率
```python
deviation = (df['close'].iloc[-1] - df['close'].rolling(25).mean().iloc[-1]) \
            / df['close'].rolling(25).mean().iloc[-1]
return deviation <= sector_threshold  # 例: 薬品は -0.05
```

### P2: RSI(14)
```python
rsi = ta.rsi(df['close'], length=14).iloc[-1]
return rsi < 35
```

### P3: ボリンジャーバンド(25,2)
```python
bb = ta.bbands(df['close'], length=25, std=2)
return df['close'].iloc[-1] < bb['BBL_25_2.0'].iloc[-1]
```

### P4: 日経225当日騰落フィルター
```python
# 日経225の当日騰落率を取得（事前にIngestion Nodeで取得済みの値を使う）
return nikkei_change_pct > -2.0  # -2%より下落していたらFalse（スキップ）
```

### P5: 出来高収束（売り圧力の収束確認）
```python
vol_3d = df['volume'].iloc[-3:].values  # 直近3日
return vol_3d[0] > vol_3d[1] > vol_3d[2]  # 減少傾向
```

---

## バックテスト計画（実装後に実施）

```
期間: 2020-2024（コロナショック・金融引き締め・コロナ急騰を含む）
データ: J-Quants API日足（13銘柄）
ツール: vectorbt
検証項目:
  □ Sharpe Ratio > 1.5
  □ 最大ドローダウン < -20%
  □ 勝率 > 50%
  □ 閾値の妥当性確認（-5% / -7% / -10%）
  □ コンセンサス3/5の有効性確認
```

---

## ペーパートレード条件（ライブ移行前に4週間連続クリア）

```
□ Sharpe Ratio > 1.5
□ 月次黒字（4週間連続）
□ 全エグジット理由がDuckDBに正しく記録される
□ 月次ドローダウンストッパーが正常動作（手動テスト）
□ 立花証券デモAPIでの注文・約定・決済サイクルが正常完走
□ 5プレイヤーの投票ログが全件scansテーブルに記録される
```

---

## 実装週次スケジュール

```
Week 1: DB・ブローカー・データ基盤
  Issue #1: DuckDB作り直し（旧スキーマ削除・新スキーマ作成）
  Issue #2: 立花証券クライアント（TachibanaClient + Mock）
  Issue #3: J-Quantsで13銘柄の日足データ取得・MA25計算

Week 2: 5プレイヤー実装
  Issue #4: P1〜P5 実装・単体テスト
  Issue #5: Voting Node（asyncio並列・コンセンサス集計・DB記録）

Week 3: パイプライン結合
  Issue #6: Kelly + LangGraphパイプライン（Ingestion→Voting→Kelly→Execution→Monitor→Exit）
  Issue #7: デモAPIで注文テスト（買い→監視→売り の1サイクル）

Week 4+: ペーパートレード
  → 4週間安定 → ライブ移行（初期: 資本の10%）
```
