# auカブコム証券 (kabu.com) 口座開設後 即実装 Checklist

> 作成: 2026-05-25
> 想定: Grove が個人口座申込 → 1-2週間で開通 → このリストに沿って Phase 2 完遂
> 期待効果: 立花e支店 22bps × ¥1M notional × 60trades/日 削減 = 月¥2.86M / 年¥34.3M commission削減
> retrospective n=198 trades: 立花 -¥94K → kabu想定 +¥378K (差 +¥471K)

---

## Phase A: Grove 側 (人間作業) — 開設まで 1-2週間

### A-1. 口座開設 (Web 10分申込 + 審査 1-2週間)
- [ ] **auカブコム証券** (Mitsubishi UFJ eSmart Securities) HPで個人口座申込
  - https://kabu.com/
- [ ] 必要書類:
  - マイナンバー (個人番号カード or 通知カード)
  - 本人確認書類 (運転免許証 or マイナンバーカード)
  - 金融機関口座 (振込先)
- [ ] **重要: 申込画面で以下を同時にチェック**:
  - ✅ NISA口座 (Phase 1 個人で枠活用)
  - ✅ **信用取引口座** ← **kabuステーションAPI Pro plan trigger 条件**
  - ✅ 先物・オプション取引口座 (将来用、なくてもOK)

### A-2. Pro plan trigger
- [ ] 開通後、信用取引口座で **¥100 程度の超小額試し売買 1回**
  - Pro plan 自動付帯 (3ヶ月内に1取引が条件)
- [ ] kabuステーション GUI ダウンロード + Mac mini VM内インストール (Phase B-1 後)

### A-3. Mac mini Windows VM準備 (並行)
- [ ] **Parallels Desktop** 購入 (Pro Edition ¥10K/年程度) or **UTM** 無料
  - 推奨: Parallels (kabuステーション 動作実績豊富)
- [ ] **Windows 11 OS** ライセンス (¥18K Microsoft Store or 既存流用)
- [ ] Mac mini に VM作成 → Windows 11 install
- [ ] VMネットワーク設定: **Shared Network** (Parallels default、Mac側Python から 10.211.55.X:18080 形式アクセス可)

---

## Phase B: Davis 側 (Phase 2 実装) — 開通後 1週間集中

### B-1. kabuステーション セットアップ
- [ ] Windows VM内:
  - kabuステーション インストール (auカブコム HP からDL)
  - 通常ログイン → 「ピックアップサービス」→ 「kabuステーションAPI」設定
  - APIシステム設定で **API password** 設定 (英数字6-16桁)
  - Windows起動時 kabuステーション 自動起動設定 (スタートアップ folder)

### B-2. Mac側 接続検証
- [ ] `.env` に `KABU_API_PASSWORD=...` 追加
- [ ] `KABU_ENV=demo` で疎通test:
  ```bash
  curl -X POST http://10.211.55.X:18081/kabusapi/token \
    -H "Content-Type: application/json" \
    -d '{"APIPassword":"YOUR_API_PASSWORD"}'
  ```
- [ ] `tests/test_kabu_com.py` に **実 API 疎通テスト** 追加 (`@pytest.mark.integration`)

### B-3. KabuComClient Phase 2 完全実装 (`src/broker/kabu_com.py`)
- [ ] **`/sendorder` 約定価格取得**: POST後 OrderId → polling で /orders 照会、Executed=true まで wait
- [ ] **`get_positions()` 実装**: GET /positions → list[dict]
- [ ] **`cancel_order(order_id)` 実装**: PUT /cancelorder
- [ ] **`get_orders()` 実装**: GET /orders (status query)
- [ ] **401 リトライ**: token無効時に自動 `login()` → request再送
- [ ] **取引パスワード** (注文時 Password field): env `KABU_TRADE_PASSWORD` で別途設定
- [ ] **token cache file** (`.kabu_token`) で多プロセス共有: cron 8:55 で取得 → main.py で読み込み

### B-4. 法人 AccountType=12 確認 (Phase 3用、優先度低)
- [ ] kabu サポートに問い合わせ: 「法人口座で kabuステーション API 利用可能か」
- [ ] 回答 yes → 法人口座開設時に AccountType=12 切替で運用継続可能
- [ ] 回答 no → 立花 or IBKR 法人口座 検討

### B-5. main.py / runner 統合
- [ ] `src/main.py` の MockTachibanaClient 注入箇所を **broker abstraction** で切替可能化
  - `--broker tachibana` or `--broker kabu` で CLI切替
- [ ] paper モードで MockKabuComClient (commission ¥0) を使う option追加
- [ ] LIVE モードで KabuComClient へ切替

### B-6. forward paper 立花 → kabu 移行検証
- [ ] **MockKabuComClient で 5/26-30 paper再走** (commission ¥0想定)
- [ ] Q3 (全book net PnL) が retrospective +¥471K と整合するか実測
- [ ] kill_criterion.md 更新: kabu前提の GO/KILL閾値

### B-7. LIVE移行準備 (Grove最終承認後のみ)
- [ ] 小資金 paper → LIVE 切替 step-by-step plan:
  1. p1m ¥1M LIVE (Grove名義) で 1週間試走
  2. 安定なら p5m ¥5M に拡張
  3. 段階的に p50m まで
- [ ] **絶対ルール**: kabu LIVE 起動は **必ず Grove 明示承認**
- [ ] 緊急停止スクリプト (`scripts/kabu_emergency_stop.py`) 作成
  - 全 open positions 即成行売り
  - cron全停止
- [ ] Grove に Discord/Slack 通知 (毎日約定サマリ)

---

## Phase C: 運用 (LIVE後の維持)

### C-1. 監視
- [ ] kabuステーション GUI が裏で running か (Mac mini → VM → process check)
- [ ] daily token 8:55 自動再取得 cron稼働 (失敗時 Grove alert)
- [ ] Pro plan 維持: 3ヶ月毎に信用1取引 (cron で自動小額売買)

### C-2. 障害対応
- [ ] VM crash → Parallels CLI で再起動 cron
- [ ] kabuステーション 異常終了 → Windows AppRestart 設定
- [ ] API token無効 → 自動 re-login (B-3-401リトライ)

### C-3. 月次 review
- [ ] Pro plan continuation check (信用1取引履歴確認)
- [ ] commission削減 実績 vs 推定 (¥2.86M/月) 検証
- [ ] 取引銘柄 sector分布 (集中度確認)

---

## Phase D: 将来 (Phase 3 AGI法人化)

### D-1. 法人口座切替判断 (12ヶ月後 想定)
- [ ] AGI法人設立 (株式会社、合同会社等)
- [ ] auカブコム 法人口座開設
  - 登記簿謄本、印鑑証明、責任者本人確認、FATCA宣誓書
  - 開設期間: 2-4週間
- [ ] kabuステーション API 法人対応確認 (B-4結果次第)
- [ ] 個人口座 → 法人口座 資産移動 (Grove個人売却 → 法人入金 → 法人買戻し or 現物移管)
- [ ] AccountType=4 → 12 へ config切替

### D-2. もし kabu法人 API 不可なら代替
- [ ] **IBKR 法人口座** (REST API、海外、現物8bps + min $5)
- [ ] **立花e支店 法人口座** (REST API、22bps、慣れた環境)

---

## Phase B-3 で気をつけるべき具体的な実装ポイント

### Order body 完全形 (kabu yaml v1.5)
```python
{
    "Password": os.getenv("KABU_TRADE_PASSWORD"),  # 取引パスワード (必須!)
    "Symbol": "7203",
    "Exchange": 1,                  # 東証
    "SecurityType": 1,              # 株式
    "Side": "2",                    # 買=2 / 売=1
    "CashMargin": 1,                # 現物
    "DelivType": 2,                 # お預り金
    "FundType": "AA",               # 現物買: AA / 現物売: '  '(半角スペース2)
    "AccountType": 4,               # 個人特定 / 法人=12
    "Qty": 100,
    "ClosePositionOrder": 0,        # 信用なら指定
    "ClosePositions": None,         # 信用なら指定
    "FrontOrderType": 10,           # 成行
    "Price": 0,                     # 成行は 0
    "ExpireDay": 0,                 # 当日
}
```

### token cache file (`.kabu_token`)
```python
# 推奨フォーマット (yaml or json):
{
  "token": "abc123...",
  "issued_at": "2026-05-26T08:55:00",
  "env": "production"
}
# expiry: 翌朝の強制ログアウト (next day 06:00頃)
```

### 401 リトライ pattern
```python
def _request_with_retry(self, fn, *args, **kwargs):
    for attempt in range(2):
        try:
            resp = fn(*args, **kwargs)
            if resp.status_code == 401 and attempt == 0:
                logger.warning("Token expired, re-login")
                self.login()
                # retry with new token
                if "headers" in kwargs:
                    kwargs["headers"]["X-API-KEY"] = self._token
                continue
            return resp
        except Exception:
            if attempt == 1:
                raise
```

---

## TL;DR (Grove向け要約)

**今すぐ**:
1. auカブコム HP で個人口座申込 (10分、必要書類事前準備)
2. 申込画面で **信用取引口座** 同時チェック (必須、Pro plan trigger 条件)

**開通後 (1-2週間後)**:
3. 信用で ¥100 試し売買 1回 → Pro plan 自動取得
4. Parallels Desktop インストール → Windows 11 VM作成
5. Davis が Phase B-1 ~ B-7 を 1週間で完遂

**LIVE移行 (3-4週間後想定)**:
6. p1m ¥1M で1週間試走 → 段階拡張
7. 月次 review で commission削減 ¥2.86M/月 実績確認

このリストを順番に消化すれば、grove-stock = 令和式 BNF AGI fund が **commission ¥0** で稼働開始可能。
