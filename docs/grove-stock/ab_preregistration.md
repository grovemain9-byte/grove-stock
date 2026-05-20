# regime_filter Live A/B 事前登録（Pre-Registration）

> 過学習門を**データ到来前に**固定する規律資産。handoff教訓: 門を先に
> 宣言した時だけ rank_validate 過学習を捕捉できた。本書は live A/B の
> 結論規則を凍結し、後付けの都合解釈を禁じる。
> 作成 2026-05-18 / status: ACTIVE / applied=False（Shadow厳守）

---

## 1. 背景と仮説（(iii)-a で機構が判明・従来理解を修正）

regime_filter treatment(p5m/p30m, `p4_required=True`) vs control(p1m/p10m
/p50m, G3式 `p4_required=False`)。

**(iii)-a レジーム条件別 3年backtest（コスト込10bps+手数料・end=2026-05-15固定）**:

| bucket | arm | closed | 勝率 | avg_ret | Sharpe/t | maxDD |
|--------|-----|-------:|-----:|--------:|---------:|------:|
| all | control | 546 | 52.9% | +0.46% | 0.050 | -28.8% |
| all | treatment | 283 | 57.2% | +1.40% | 0.134 | -18.3% |
| **bearish** | control | 124 | 58.9% | +2.62% | 0.193 | -17.0% |
| **bearish** | treatment | 124 | 58.9% | +2.62% | 0.193 | -17.0% |
| **bearish** | **Δ(t-c)** | **0** | — | **0** | **0** | **0** |
| ranging | control | 422 | 51.7% | -0.05% | -0.006 | -29.8% |
| ranging | treatment | 258 | 55.4% | +0.54% | +0.070 | -16.3% |
| bullish | control | 228 | 51.8% | **-0.36%** | -0.049 | -18.1% |
| bullish | treatment | 0 | — | — | — | — |

**機構（重要・従来の誤解を訂正）**:
- **弱気バケットで treatment ≡ control（Δ完全ゼロ）**。弱気(dev≤-3%)では
  P4(dev<0)が常時発火＝`p4_required`が何も濾さない。**edgeは弱気アルファ
  ではない**。
- **edgeの正体＝非弱気での損失回避**。逆張りは弱気で稼ぎ(+2.62%)、強気で
  **負け(-0.36%)**、レンジでほぼ-0。controlは全レジーム建玉ゆえ強気/レンジ
  の負けが弱気の利益を希釈(+0.46%)。treatmentは強気を完全回避＋レンジを
  濾過＝弱気アルファを温存(+1.40%・maxDD半減)。

**∴ 仮説（凍結）**: live でも treatment は control を上回る。その源泉は
**弱気での優位ではなく、強気/レンジでの建玉回避による損失回避**であり、
これは弱気相場の到来を待たず**現レジームから連続的に観測可能**
（現状 Nikkei +1.79%＝強気＝control が負け期待値の建玉を実行中、
treatment は正しく休止中）。

---

## 2. 主要指標（live・連続測定）

- **Primary**: treatment book群 と control book群 の **累積 realized
  return（複利エクイティ曲線）の差分**。同一シグナル源・同一会計・cost込。
- **等価な読み筋**: control が非弱気で建てた（treatmentが見送った）取引の
  実現損益。仮説が正なら control の非弱気取引は負寄与＝treatment は
  「持たないこと」で勝つ。
- **Secondary（サニティ）**: 弱気バケットで treatment ≈ control（Δ≈0）が
  live でも成立するか（(iii)-a の機構が live で再現する確認。乖離大なら
  機構理解が誤り＝門を再設計）。

---

## 3. 事前確定ゲート（後付け解釈禁止）

評価は **WF型 3独立窓ロバスト性**（プロジェクト既存 hypothesis_shadow と
同型の規律）で行う。窓 = 連続する暦月（または ≥20営業日のローリング窓）。

| 判定 | 条件（**全て事前固定**） |
|------|--------------------------|
| **GO** | (i) control 累積 **非弱気** closed ≥ **N_min**（§4）到達 **かつ** (ii) treatment エクイティ − control エクイティ差分が **3/3 窓で正** **かつ** (iii) Secondary（弱気Δ≈0）が崩れていない |
| **PASS（継続観測）** | N_min 未到達、または 窓 1〜2/3 のみ正（判定保留・蓄積継続） |
| **KILL（仮説棄却）** | N_min 到達後に差分が **3/3 窓で負**、または Secondary が大きく乖離（機構誤り）→ regime_filter 仮説を棄却し treatment を control 式へ revert |

- **applied=False を GO まで不可侵**（Shadow厳守。GO到達後の適用可否は
  別途 Grove 承認事項。本書は「測るか」だけを凍結し「適用」は凍結しない）。
- 中間で指標定義・窓幅・N_min を**変更しない**（変更時は本書を改版し
  理由と日付を明記、過去評価は無効化して再カウント＝こっそり調整禁止）。

---

## 4. N_min のパワー根拠（per-trade t検定でなくエクイティ窓）

- per-trade ノイズは巨大（G3 std ≈ 9%/trade）。単取引水準の有意差検定は
  非現実的（mean ≈ -0.2% を std 9% で検出 → N ≈ 数千）。∴ **判定は
  book エクイティ曲線（複利・窓集計）水準**で行い、per-trade 検定はしない。
- 効果量アンカー（(iii)-a 実測）: 非弱気 Δ avg = bullish +0.36%/trade,
  ranging +0.59%/trade。control 非弱気取引数 ≈ 228(bull)+ranging差164
  ≈ **約392/3年 ≈ 約130/年**。
- **N_min = control 非弱気 closed ≥ 200**（≈1.5年相当の backtest 密度に
  対応。live paper の約定密度はこれより低く、暦で数ヶ月規模＝**弱気相場
  待ち(年単位)より大幅に速い**＝(iii)-a が示した加速の核心）。到達まで
  PASS で蓄積。N_min は「機会が十分蓄積した」最低条件であり有意性の
  代理。最終判定はあくまで 3/3 窓ロバスト性。

---

## 5. 運用

- 集計は既存 `scripts/hb_learning.py --weekly`（regime_filter A/B 比較・
  決定論・読取のみ・applied=False）に本書のゲートを適用。新層は作らない。
- レビュー頻度: 週次（HB weekly 土曜）。GO/KILL 到達時のみ Grove へ
  エスカレーション、それ以外は PASS を淡々と継続（cheerleading 禁止）。
- 本書改版履歴をここに追記すること（日付・変更・理由）。
  - 2026-05-18 初版（(iii)-a 実測 ground・機構訂正反映）。
