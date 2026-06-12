# idea-rubric.md — 案の評価器（grove-stock専用・動的）

> 使い方（指示形）: 「**このrubricで評価せよ**」— 戦略変更案・実装案・新機能アイデアに適用。
> workerはplan提示前に自己採点、Leaderはreview時に独立採点（乖離=議論ポイント）。
> **動的rubric**: 静的な合格点で自動ship/killしない。点はGroveとの対話の言語。状況が変われば軸ごとGroveと更新する。

## 共通4軸（Boris式、各1-5+根拠1行）

| 軸 | 問い | 1 | 5 |
|---|------|---|---|
| **Elegance** | 少ない部品で多くを解くか | 過剰設計/既存の焼き直し | 既存部品の再利用で複数問題を一度に解く |
| **Simplicity** | Groveが一読で判断できるか | 理解に専門知識の壁 | 見た瞬間に意図が分かる |
| **Blast Radius** | 失敗時の被害範囲は | 本番/不可逆/複数系統に波及 | 1ファイル・完全可逆・rollback明示 |
| **Staff-Engineer Approval** | top層が見てOKを出すか | 車輪の再発明/self-cert | 測定済み・最小接続・検証可能 |

## BNF固有3軸（トレーディングPJの追加基準、各1-5+根拠1行）

| 軸 | 問い | 根拠の正本 |
|---|------|-----------|
| **Edge Evidence** | 実測エッジの裏付けがあるか（仮説だけ=1、登録窓の実測+統計的裏付け=5） | bnf_base_audit / decision_shadow |
| **Risk Integrity** | kill_criterion・DD制約・Tiered Kellyと矛盾しないか | docs/grove-stock/kill_criterion.md / config/strategy_params.py |
| **Measurement Discipline** | 計測窓・事前登録・shadow先行を守れる設計か | tasks/lessons.md 計測節 |

## 運用ルール

1. スコアは**根拠1行とセット**でのみ意味を持つ。点だけの報告は却下
2. Blast Radius=1（LIVE/実弾/不可逆に触る）→ スコア合計に関わらず**即Grove判断要求**
3. worker採点とLeader採点の乖離≥2の軸 → その軸を議論してから進む
4. 合計点での自動GO/KILLは**しない**（GO/KILLは product-taste.md → Grove専権）
5. 低スコア軸 = Re-plan時の改善ターゲット
