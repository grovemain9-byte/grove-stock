# BNF式MA乖離率逆張り — Spec v2（最新版）
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

### グラフ構成
2つの独立したグラフを30分cronで同時起動する。
スキャンの遅延が監視に影響しないよう分離。

```python
async def run_cycle():
    await asyncio.gather(
        scan_app.ainvoke(ScanState(...)),
        monitor_app.ainvoke(MonitorState(...))
    )
```

---

### scan_graph（新規エントリー探索）

```
[START]
   │
   ▼
[ingestion_node]
  - J-Quants APIで13銘柄の日足OHLCV取得
  - 日足MA25・乖離率を計算
  - P4用に日経225当日騰落率を取得
  - DuckDB scansテーブルに書き込む
   │
   ▼
[voting_node]
  - P1〜P5をasyncio.gather()で並列実行
  - 各プレイヤー: vote(df, **kwargs) -> bool
  - consensus = sum(votes) per ticker
  - DuckDB scansテーブルにP1〜P5とconsensusを更新
   │
   ├─ consensus < 3 → [END]
   ├─ monthly_dd_exceeded == True → [END]
   └─ consensus >= 3 → [kelly_node]
   │
   ▼
[kelly_node]
  - kelly_size(consensus, balance) でポジションサイズ決定
  - DuckDB positionsテーブルで同銘柄の重複確認
   │
   ├─ 重複あり → [END]
   └─ 重複なし → [execution_node]
   │
   ▼
[execution_node]
  - TachibanaClient.buy(ticker, shares) で現物成行買い
  - 約定確認
  - DuckDB positionsテーブルに記録（status: open）
   │
   ▼
[END]
```

**ScanState:**
```python
class ScanState(TypedDict):
    market_data: dict         # ticker → OHLCV DataFrame
    ma25: dict                # ticker → float
    nikkei_change: float      # 日経225当日騰落率
    votes: dict               # ticker → {p1..p5: bool}
    consensus: dict           # ticker → int (0〜5)
    position_size: dict       # ticker → int (株数)
    monthly_dd_exceeded: bool
    errors: list[str]
```

---

### monitor_graph（既存ポジション監視）

```
[START]
   │
   ▼
[monitor_node]
  - DuckDB positionsテーブルからopenポジションを全件取得
  - 各ポジションに4条件を適用:
    ① 損切り: 現在値がエントリー価格から-5%以下
    ② 最大保有期間: エントリーから5営業日経過
    ③ 利確: 現在値がMA25に到達（乖離率 >= 0）
    ④ 反転シグナル: P1〜P5を再実行して3以上がFalse
   │
   ├─ 全条件未成立 → [END]（次の30分cronまで待機）
   └─ いずれか成立 → exit_decisionsにticker・理由を積む → [exit_node]
   │
   ▼
[exit_node]
  - TachibanaClient.sell(ticker, shares) で現物成行売り
  - 約定確認
  - DuckDB positionsテーブルをclosedに更新・PnL記録
  - exit_reason を記録:
    stop_loss / max_hold / take_profit / signal_reversal
  - monthly_pnlを更新
   │
   ▼
[END]
```

**MonitorState:**
```python
class MonitorState(TypedDict):
    open_positions: list      # DuckDB positionsから取得
    exit_decisions: list      # [{ticker, reason}]
    errors: list[str]
```

---

### 共有リソース
両グラフはDuckDBを共有する。同時書き込みの競合を避けるため、
positionsテーブルへの書き込みはexecution_nodeとexit_nodeのみが行う。

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
return nikkei_change_pct > -2.0  # -2%より下落していたらFalse
```

### P5: 出来高収束（売り圧力の収束確認）
```python
vol_3d = df['volume'].iloc[-3:].values
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
  □ 投票の重みづけ（単純多数決の有効性確認）← Phase 2で最適化
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
  Issue #6: Kelly + LangGraphパイプライン（scan_graph・monitor_graph）
  Issue #7: デモAPIで注文テスト（買い→監視→売り の1サイクル）

Week 4+: ペーパートレード
  → 4週間安定 → ライブ移行（初期: 資本の10%）
```
