# 2-I regime-conditional 逆張りsizing（弱気で厚張り）— S1 tail-risk ゲート（2026-06-14）

## 結論: **2-I（弱気で素朴に厚張り）は defer**（S1ゲート RED）。条件付き再開。

Grove判定 2026-06-14: defer + 条件付き再開。go/no-go は Grove専権（CLAUDE.md 安全境界）。

## 問い
2-B Phase A の拾った宝＝「逆張りは弱気で圧倒的（勝率67%/+3.4% vs もみ合い53%/+0.4%、5年）」。
→ 手法切替でなく **sizing で地合いを使う**: 弱気regimeで逆張りポジションを厚く張る。
S1ゲート（建てる前にtail-riskを確認）: **「弱気で厚張り」は最悪ケースを生き残るか？**
弱気=暴落局面でもあり、67%勝率の裏に致命的左尾が隠れてないか（boat 87%喪失 / R1勝率過大推定の教訓）。

## 方法（既存infra reuse・本番非変更）
- **W1 per-trade tail probe**（既存 /tmp/2b_rev.json, read-only）: 弱気逆張り209件の左尾を読む。
  `docs/grove-stock/research/2i_tail_probe.py`（/tmp に置いた版と同等）。
- **W2 portfolio経路sim**: engine に additive param（`regime_size_multipliers` / `bearish_max_concurrent`、default-off、444テスト不変）を足し、フラット vs 厚張り vs 尾抑え厚張り で equity曲線の最大DD/Calmarを比較。
  `docs/grove-stock/research/2i_sizing_gate.py`。
- **robustness**: コストON（片道10bps slippage + 立花手数料）+ 2024-08暴落依存度。
  `docs/grove-stock/research/2i_gate_robust.py`。
- **独立検証**: independent-evaluator(opus) がエンジンロジック監査 + scorecard独立再計算（bit-exact）+ 結論の敵対的検証。

## 結果

### W1 per-trade tail（弱気逆張り 209件、勝率67.5%/avg+3.4%）
| | worst | p5 | CVaR5 | std | stop突き抜け |
|---|---|---|---|---|---|
| 弱気 | -23.0% | -17.1% | -19.3% | 12.4% | stop 59件 **100%が-5%突抜**, 平均 -11.6% |
| もみ合い | -16.9% | -9.8% | -11.6% | 7.5% | 〃 100%突抜, 平均 -8.6% |

- 最悪10件のうち **7件が2024-08**（円キャリー巻き戻し暴落）に集中 = 相関。
- per-trade naive ×2: worst -46%, CVaR5 -38.7%（相関無視の楽観下限）。
- → per-trade では「boatパターン（深い相関左尾）」に見えた。

### W2 portfolio scorecard（5年、base 100k、max_concurrent=10、cost-off）
| variant | closed | bear | tot_ret | maxDD | DD_aug | calmar |
|---|--:|--:|--:|--:|--:|--:|
| V0 flat | 734 | 182 | +89.9% | -19.8% | -19.5% | 4.54 |
| V1a ×1.5 | 734 | 182 | +119% | -23.4% | -23.4% | 5.09 |
| V1b ×2.0 | 734 | 182 | +148% | -27.4% | -27.4% | 5.42 |
| V2a cap5 ×2 | 718 | 121 | +103% | -23.0% | -20.0% | 4.49 |
| V2b cap3 ×2 | 705 | 81 | +72% | -25.8% | -20.5% | 2.80 |

- 見かけ: 素朴×2が calmar 最高（5.42 > flat 4.54）、tail-cap は calmar を下げる。
- cost ON: V0 3.42 / V1b 4.39 / V2a 3.44（×2は総リターン基準では依然高い）。

### 独立検証 verdict: **RED**（エンジン正、結論は信用に足りず）
エンジン sizingロジックは正しく（size_mult が realized PnL / cost / equity の3箇所に一貫、cap も両パス正）、scorecard は equity dump から bit-exact 再現。**だが結論「厚張りが勝つ・capは損」は3つの交絡の合成**:

1. **暗黙レバレッジ（本丸）**: entry gate（`engine.py:349`）は建玉数チェックのみ、**現金/証拠金チェック皆無**。フラットで既に資本100%、×2弱気で 110〜200% 展開。V1bの高リターンの一部は edge でなくレバレッジ。maxDD は margin call/強制ロスカットを見ていない。cross-variant calmar はレバレッジ交絡。
2. **非年率化**: calmar = 5年 total/maxDD。年率化すると見かけ優位 +0.88 → **+0.04（≒ノイズ）**。big-number で 6.6倍に錯覚。
3. **単一暴落依存**: V1bの -27.4% maxDD は丸ごと 2024-08-05底→08-14全戻しの **6営業日V字**。反発した暴落1回に依存。

→ **「弱気で素朴に厚張りが勝つ」は decision-grade でない。** 公平な（等展開）比較は未測定。元仮説「相関暴落で厚張りは破滅」は**反証されていない**（反発しない暴落で一度も試していない）。捨てようとした cap こそ未検証の左尾への防壁。

## 状態更新
- 2-I: 🔵発見 → **⏸️defer 2026-06-14**（S1ゲート RED、見かけ優位は交絡の合成）

## 再開条件（全て満たせば revisit）
1. **engine に現金/証拠金ゲート追加**（`position_size*size_mult` が利用可能残高超過なら entry 拒否）or 全変種を**等ピーク展開に正規化** → レバレッジ交絡を除去した公平な edge を測る。
2. **非反発暴落 stress**（合成 or 2024-08 を V字でなく L字に改変）で生き残るか。
3. 年率化 calmar / Sortino で edge がノイズを明確に超える。
→ 上記で edge が残れば、tail-cap 付き（cap は左尾防壁として保持）で設計フェーズへ。

## 教訓（→ lessons.md / Davis memory）
- **per-trade tail ≠ portfolio tail**: W1の深い左尾(per-trade -46%)は portfolio で1/10に薄まる。だが portfolio sim 自体が**レバレッジ/年率化/サンプル**で誤誘導する → 多層で疑え。
- **backtest leverage confound**: 現金ゲート無しの sizing 比較は「多く張れば勝つ」を edge と誤認する。
- **independent audit が self-cert を救った**: 俺は「厚張れ・capするな」(危険方向)に結論しかけ、opus監査が交絡を捕捉。3連続 cheap-fail gate（p2m / 2-B / 2-I）。
