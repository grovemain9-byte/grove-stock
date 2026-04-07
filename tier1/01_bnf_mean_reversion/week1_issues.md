# GitHub Issues — Week 1〜3（テスト環境完成まで）

> Claude Codeへの渡し方: **1枚ずつ渡す。まとめて渡さない。**
> 実装前に必ず CLAUDE.md を読むこと。

---

## Issue #1: DuckDB 作り直し

**やること**
既存の stock_davis.duckdb を新スキーマで作り直す。
旧テーブル（q_table / reflexion_episodes / market_context）は削除する。

**完了条件**
- [ ] `python src/data/db.py --init` で新テーブルが作成される
- [ ] 旧テーブルが存在しない
- [ ] 各テーブルにテストレコードを1件ずつ書き込み・読み出しができる
- [ ] DBパスは `.env` から読む（ハードコードしない）

**作成するテーブル**（スキーマはCLAUDE.md参照）
- `scans`
- `positions`
- `monthly_pnl`

---

## Issue #2: 立花証券クライアント実装

**やること**
`src/broker/tachibana.py` に `TachibanaClient` と `MockTachibanaClient` を実装する。

**TachibanaClient の要件**
- `.env` から `TACHIBANA_USER_ID`, `TACHIBANA_PASSWORD`, `TACHIBANA_ENV` を読む
- `TACHIBANA_ENV=demo` のときデモURLを使う
- ログイン → 仮想URL取得 → セッション保持 → 各操作 → ログアウト
- 公式サンプル（https://github.com/e-shiten-jp）の構造を参考にする

**MockTachibanaClient の要件**
- 実APIを叩かずに全メソッドが動く
- buy() は固定の約定レスポンスを返す
- get_balance() は100万円を返す
- テスト用にログ出力する

**完了条件**
- [ ] `python src/broker/tachibana.py --test` でデモAPIにログイン・残高取得・ログアウトが成功する
- [ ] `MockTachibanaClient` を使った `tests/test_broker_mock.py` が全グリーン
- [ ] 認証情報がコードに含まれていない

---

## Issue #3: J-Quants 日足データ取得・MA25計算

**やること**
`src/data/jquants.py` で13銘柄の日足データを取得し、日足MA25と乖離率を計算する。

**要件**
- `config/sector_config.py` の SECTOR_CONFIG から銘柄コードを読む
- 少なくとも30日分の日足OHLCVを取得する（MA25計算に必要）
- 日経225（コード: 0000）の当日騰落率も取得する（P4用）
- 出力フォーマット:

```python
{
    "4519": {
        "ticker": "4519",
        "sector": "薬品",
        "close": 9198.0,
        "ma25": 10234.5,
        "deviation_pct": -10.1,  # (close - ma25) / ma25 * 100
        "volume_3d": [1200000, 980000, 750000],  # 直近3日（古い順）
        "ohlcv_df": pd.DataFrame(...)  # 30日分（P2・P3用）
    },
    ...
}
```

**完了条件**
- [ ] 13銘柄全ての乖離率が出力される
- [ ] 日経225当日騰落率が出力される
- [ ] `tests/test_jquants.py` でモックデータを使った単体テストが通る

---

## Issue #4: 5プレイヤー実装

**やること**
`src/players/` に5ファイルを実装する。
全プレイヤーのインターフェースはCLAUDE.mdに定義済み。

**ファイル一覧**
- `p01_ma_deviation.py` — セクター閾値との比較
- `p02_rsi.py` — RSI(14) < 35
- `p03_bollinger.py` — close < BBL(25,2)
- `p04_nikkei_filter.py` — 日経225騰落 > -2%
- `p05_volume_convergence.py` — 直近3日出来高が減少傾向

**完了条件**
- [ ] 各プレイヤーの単体テストが `tests/test_players.py` で通る
- [ ] BUY条件を満たすケース・満たさないケースの両方をテスト
- [ ] 例外発生時に `False` を返すことをテスト（パイプラインを止めない）
- [ ] pandas-ta を使う（ta-lib禁止・CLAUDE.md参照）

---

## Issue #5: Voting Node 実装

**やること**
`src/voting.py` で5プレイヤーを並列実行し、コンセンサスを集計してDuckDBに保存する。

**実装仕様**
```python
async def run_voting(ticker_data: dict, sector: str) -> dict:
    votes = await asyncio.gather(
        p01.vote(ticker_data['ohlcv_df'], sector=sector),
        p02.vote(ticker_data['ohlcv_df']),
        p03.vote(ticker_data['ohlcv_df']),
        p04.vote(ticker_data['nikkei_change']),
        p05.vote(ticker_data['ohlcv_df']),
        return_exceptions=True,  # 例外でも他プレイヤーを止めない
    )
    # return_exceptions=Trueの場合、例外オブジェクトはFalseとして扱う
    bool_votes = [v if isinstance(v, bool) else False for v in votes]
    consensus = sum(bool_votes)
    return {
        "p1": bool_votes[0], "p2": bool_votes[1], "p3": bool_votes[2],
        "p4": bool_votes[3], "p5": bool_votes[4],
        "consensus": consensus,
        "triggered": consensus >= 3,
    }
```

**完了条件**
- [ ] `tests/test_voting.py` で全グリーン
- [ ] コンセンサス結果がDuckDB `scans` テーブルに記録される
- [ ] 1プレイヤーが例外を投げても他4人の結果で続行する

---

## Issue #6: LangGraphパイプライン結合 + ドライラン

**やること**
`src/main.py` で全ノードをLangGraphで結合し、`--dry-run` モードで1サイクル動かす。

**ノード構成**（spec.md参照）
Ingestion → Voting → Kelly → Execution → Monitor → Exit

**--dry-run モードの動作**
- 全ノードを実行するが、Execution NodeはMockTachibanaClientを使う
- 「注文するはずだった内容」をログ出力する
- DuckDBへの書き込みは実行する（dry-runでも記録する）

**完了条件**
- [ ] `python src/main.py --dry-run` が1サイクル完走する
- [ ] DuckDB `scans` テーブルにレコードが追加される
- [ ] BUYシグナルが発生した場合、Kellyサイズが計算されてログに出る
- [ ] エラー時のログが分かりやすい

---

## Issue #7: デモAPIで注文テスト（買い→監視→売り）

**やること**
立花証券デモAPIで実際に注文を出し、買い→監視→売りの1サイクルを確認する。

**テスト手順**
1. `python src/main.py --demo` でデモモード起動（MockではなくデモAPIを使う）
2. 手動で閾値を下げて強制的にBUYシグナルを発生させる
3. デモAPIで成行買い注文が出ることを確認
4. 利確条件を手動で満たして成行売りが出ることを確認

**完了条件**
- [ ] デモAPIで成行買い注文が受け付けられる
- [ ] `positions` テーブルに open レコードが作成される
- [ ] 成行売りが実行される
- [ ] `positions` テーブルが closed に更新され、PnLが記録される
- [ ] `monthly_pnl` テーブルが更新される

**注意**: 本番APIは絶対に使わない。`.env` の `TACHIBANA_ENV=demo` を確認してから実行。
