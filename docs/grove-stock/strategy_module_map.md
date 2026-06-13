# grove-stock 戦略モジュールマップ（終わらないバックログ）

> 2026-06-13 Grove vision「システムをモジュール構造化し、毎日1つ選んでloopで進化拡張」。
> R1/R2/R3研究の発見を抜けなく登録。研究で新モジュールが出たら追加（拡張）、艦隊運転で実装したら状態が進む（進化）。
> **北極星: ¥2M・月利20%（現在地=月3.2%、R1実測）**。これは方向であって、各モジュールのship/killは事前登録+実測edgeで判断。
> 状態凡例: ⬜未研究 / 🔵研究済(未実装) / 🟡shadow-paper検証中 / 🟢live候補 / ❌却下 / ✅実装済
>
> **この1枚で完結**: ①下のシステム構造で「どこが何をする部分か」 ②データセットで「何で研究できるか」 ③エリア1-5の状態列で「今どうなってて今日どこをやるか」 が分かる。/tiara はまずこれを読む。

## システム構造（どこが何をする部分か / AGIファンドSTAGE対応）
| 部分(src/) | 役割 | STAGE |
|---|---|---|
| `main.py` | LangGraph scan_graph + paper_multibook(30分cron、全体統括) | 全体 |
| `data/jquants.py` `universe.py` `edinet.py` | 価格API / 1571銘柄ユニバース / 開示情報 | S0-1 収集 |
| `players/p01〜p05.py` | 5指標判定(MA25乖離/RSI/BB/日経/出来高) | S2 予測 |
| `voting.py` | consensus集計(3/5+でBUY、bookごとoverride) | S3 集約 |
| `kelly.py` `sizing/(evs/plt/router/optimizer)` | Tiered Robust Kelly + 10-factor EVS + PLT | S4 サイジング |
| `broker/tachibana.py` | 立花/auカブコムAPI抽象(MockClient=paper) | 執行 |
| `backtest/(engine/runner/regime/shadow_replay)` | バックテスト+walk-forward+反実仮想 | 検証 |
| `measurement/(walk_forward/capital_tracker/discipline_apply)` | 計測+資本追跡+規律適用 | S5 学習 |
| `monitor.py` `news_scanner.py` `report.py` | 監視 / ニュース / レポート | 横断 |

## データセット（研究で使えるもの）
| データ | 中身 | 研究用途 |
|---|---|---|
| `data/grove_stock.duckdb` → positions | closed 467+件(実トレード記録) | 戦略の勝率/PnL/月利効率の主データ |
| `data/grove_stock.duckdb` → decision_shadow | 2191+件(passした判断の反実仮想追跡) | 「見送りが正解だったか」学習(R2が使用) |
| `data/history_cache.duckdb` | 価格履歴キャッシュ | 新戦略のバックテスト |
| `data/edinet_cache.duckdb` | 開示情報キャッシュ | 決算ギャップ戦略(3-2)等 |
| universe(コード内) | 東証1571銘柄(流動性adv≥1億でフィルタ) | 銘柄選択の母集団 |
| 外部API | J-Quants(価格/当日Light) / EDINET(開示) | 日次データ取得 |
> 研究例: 「positions × decision_shadow で月利効率セル分析」(R2実績) / 「history_cache で新高値ブレイクをバックテスト」(3-3)

## エリア1: コア戦略（現BNF逆張りスイング）
| M | モジュール | 状態 | edge/現状(実測) | 次アクション | 指標 |
|---|---|---|---|---|---|
| 1-1 | 3book並列(p2m/p5m/p10m) | ✅稼働 | 2026-06-14 Grove「3本だけ」=最低単元¥2M化でp1m/p30m/p50m削除(運用簡素化・edge最適化でなく整理)。削除book open10件(p30m4/p50m6)はmonitor(book非依存 WHERE status=open)が決済→orphan化なし。444 pytest green | 観察継続 | book別月利効率 |
| 1-2 | **p2m book(¥2M専用)** | ✅paper稼働 2026-06-14 | books.py末尾追加(¥2M/flex/max3/**price_min¥1k=p1m敗因の¥500未満ゴミ帯回避**/cons4=勝book標準)。実paperサイクルでdecision3/position2 open確認・444 pytest green。**edge未証明**(sane default、結果問わずまず回す方針=Grove) | forward paperでrealized蓄積→edge判定。背景: R2「cons4×1k-3k 78.6%」非再現/p1m損失97.9%はcons5変更前=真因はjunk帯(独立opus catch §2.5 gate初仕事) | 月利vs北極星20% |
| 1-3 | consensus=5集中(p1m改造型) | ⬛retired 2026-06-14 | p1m削除に伴い終了(n2のまま)。3本化で全book cons4標準、cons5逸脱bookは無し | — | — |

## エリア2: BNF原典の未実装要素（R3発見、出典付き）
| M | 要素 | 状態 | 内容 | 次 |
|---|---|---|---|---|
| 2-A | 地合い判断の二層構造 | ⬜ | 日経先物で地合い先読み(現P4は当日-2%のみ) | 設計研究 |
| 2-B | 手法スイッチ(レジーム) | ⏸️defer 2026-06-14 | S1ゲートで切替前提が**非支持**(5年backtest): 逆張りは上げで0件(P4が既にブロック)/順張りは上げで勝たず横ばい。詳細 `docs/grove-stock/research/2-B-regime-edge.md` | 再開=個別vol/slope軸+コスト込み再検証の価値が出た時 |
| 2-C | セクター連れ高(出遅れ順張り) | ⬜ | セクター内で上がった銘柄の出遅れ組を買う | 設計研究 |
| 2-D | 撤退速度(反発待たず即損切り) | ⬜ | 反発失敗判定→即日損切り(現stop固定-7%) | 設計研究 |
| 2-E | イベント前売り | ⬜ | 材料出尽くし前の手仕舞い(材料カレンダー連動) | 2-3(決算)と統合検討 |
| 2-F | 逆張り乖離のセクター差分 | 🟡一部実装 | 安定株-5〜-10%/ハイテク-10〜-15%/新興-20%+ | 現セクター閾値の精緻化 |
| 2-G | 資金規模で分散シフト | 🟡一部実装 | 少額=集中/大型=分散(現max_concurrent固定) | book別max_concurrentの動的化 |
| 2-H | 目標値なし原則(trailing exit) | 🟡一部実装 | 固定利確でなくtrailing(現+2%固定) | trailing exit検証 |
| 2-I | **regime-conditional 逆張りsizing(弱気厚張り)** | 🔵2-Bから発見 2026-06-14 | 逆張りedgeは弱気で圧倒的(勝率67%/+3.4% vs もみ合い53%/+0.4%, 5年)。手法切替でなく**sizing**で地合いを使う | **S1=tail-risk検証**(弱気=暴落の左尾を最悪ケースで確認)→OKなら設計。next loop |

## エリア3: 新戦略ファミリー（R3サーベイ、インフラ距離順）
| M | ファミリー | 状態 | edge仮説 | 期待月利(R3) | インフラ距離 |
|---|---|---|---|---|---|
| 3-1 | 五十日・SQアノマリー | ❌KILL 2026-06-14 | gotobi fade本物・**gotobi固有**(非gotobi exit−17bps vs gotobi+51bps符号反転/universe5/6年+)。robust~+6bps小・capturability未確認(MA25大引け確定→same-day open執行不可) | R3「+0.5-1.5%」**非再現**(実測bps級) | KILL+bank。再開=exit-timing T+1執行でモデルし直す価値が出た時 |
| 3-2 | 決算ギャップ逆張り | 🔵研究済 | GU後の機関売り需給。5日保有でプラス(168万件検証) | 月1-3% | 近(J-Quants earnings_calendar要) |
| 3-3 | 新高値ブレイクアウト(順張り) | 🔵研究済 | モメンタム自己強化(CANSLIM) | 月3-10%(非統計) | 近(価格データのみ) |
| 3-4 | 優待・配当先回り | 🔵研究済 | 権利日40営業日前の季節上昇 | 権利月+5-15% | 中(配当カレンダー) |
| 3-5 | ペアトレード(統計裁定) | 🔵研究済 | 高相関銘柄の乖離回帰(学術検証済) | 月1-5%・方向中立 | 遠(空売り実装要) |
| 3-6 | 空売り(逆張り対称化) | 🔵研究済 | 現買い逆張りの鏡像 | 上昇相場で対称edge | 中(立花空売り対応要) |

## エリア4: サイジング/インフラ
| M | モジュール | 状態 | 現状 | 次 |
|---|---|---|---|---|
| 4-1 | Tiered Robust Kelly | ✅ | edge<10%見送り/1ポジ上限 | R1の配分25%要件と整合性検証 |
| 4-2 | PLT(Pattern Lookup Table) | 🟡 | cold5/warm1(closed律速で成長遅い) | bootstrap閾値見直し |
| 4-3 | EVS(10-factor) | ✅ | router主軸+fallback | — |
| 4-4 | walk-forward最適化 | 🟡 | scipy重み最適化Phase1未着手 | n+50到達後 |
| 4-5 | weekly_retrain | ✅ | launchd土3:07自走 | 初回発火観察 |

## エリア5: 計測/検証/リスク
| M | モジュール | 状態 | 現状 | 次 |
|---|---|---|---|---|
| 5-1 | kill_criterion | 🟡 | 6/8判定: G1 FAIL/G3+¥2.32M/K4解釈待ち | **Grove判定待ち** |
| 5-2 | decision_shadow(反実仮想) | ✅ | pass分追跡。見送り正解率高い(R2) | — |
| 5-3 | CLV/ドリフト検出 | ⬜ | funding側にskillあり、stock未移植 | 移植検討 |
| 5-4 | 階段ゲート(月3-5→5-10→10-20%) | ⬜ | 北極星への昇格条件 | Groveと事前登録 |

## R1 数学的制約（全モジュール共通の物理）
月20%には [勝率70%+ × 配分25% × 月15-20回 × 利幅5%] が**同時**に要る。単軸では不可能。現行制約(配分20%上限/max7)では月8-10%が天井→**構造変更が要る**。boat教訓: 勝率5pt過大推定でP(月20%)が42%→24%に半減。実証されたedgeの測定が先、サイジング拡大は後。

## loopの回し方
1. このマップから**今日やる1モジュール**を選ぶ（Grove or Davis提案）
2. idea-rubric.mdで採点 → 艦隊運転(HOW)で実装/研究 → shadow/paper検証
3. 状態列を更新（🔵→🟡→🟢）。新発見はモジュール追加（マップ拡張）
4. 終わらない。北極星(月20%)に近づくモジュールを優先しつつ、抜けは残さない
