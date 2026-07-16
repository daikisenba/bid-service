# 入札支援サービス(外販版)- bid-service フェーズ1

顧客企業ごとの条件に合致する公共入札案件を自動探索し、案件レコメンドを顧客専用
スプレッドシートに配信する日次バッチ。既存の社内向け4スキル
(`C:\Users\User\Documents\Claude\Projects\公共入札\` 配下)とは完全に独立して
おり、社内スキル・社内シート・GAS Web Appには一切変更を加えていない。

## ディレクトリ構成

```
bid-service/
├── main.py                       # エントリポイント(全顧客ループ)
├── config/
│   ├── settings.yaml             # スプレッドシートID・重み・送信元アドレス等
│   └── past_bids_reference.csv   # カバレッジ検証用の過去応札実績(要記入)
├── modules/
│   ├── models.py                 # 顧客・案件・マッチング結果のPydanticモデル
│   ├── config.py                 # settings.yamlの読込
│   ├── auth.py                   # gspread/Gmail API認証情報の読込
│   ├── customer.py                # 顧客マスタ読込・バリデーション
│   ├── search.py                 # kkj.go.jp検索APIクライアント
│   ├── matching.py               # スコアリング・除外判定
│   ├── awards.py                 # 落札実績オープンデータ取得・参考落札相場計算
│   └── delivery.py               # シート追記・管理者宛メール・実行ログ
├── scripts/
│   ├── setup_customer_sheet.py   # 顧客追加(マスタ行追加+専用シート初期化)
│   └── coverage_check.py         # データソースのカバレッジ検証
├── templates/recommend_mail.md
├── tests/                        # フェイクgspreadクライアントによる単体/E2Eテスト
└── .github/workflows/daily_run.yml
```

## データソースについて(重要な設計判断)

社内スキル(nyusatsu-search)はp-portal.go.jpへの**ログインを前提としたブラウザ
操作**(ブックマークレットでDOMを抽出)を行っており、無人のGitHub Actionsでは
再現できない。代わりに**官公需情報ポータルサイト検索API**
(`https://www.kkj.go.jp/api/`、中小企業庁提供)を採用した。認証不要・無料・
公式ドキュメント提供済みで、キーワード/都道府県/カテゴリー/資格等級/期間に
よる絞り込みが可能。利用にあたっては同サイトの利用規約に従うこと。

**既知の限界**: 本APIは各省庁・自治体の調達情報公開システムからの集約データで
あり、「オープンカウンタ(少額)」案件の掲載網羅性は保証されない。また、
予定価格は構造化データとして提供されないため、価格マッチングは公告文からの
正規表現ベストエフォート抽出に留まる(抽出できない場合は「要確認」として
加減点しない)。**トライアル顧客に提供する前に、必ず下記「カバレッジ検証」を
実施すること。**

## セットアップ手順(初回のみ)

### ① GCPサービスアカウントの作成とJSONキー発行

1. [Google Cloud Console](https://console.cloud.google.com/) で新規プロジェクトを作成する(oracleプロジェクトとは共用しない)
2. 「APIとサービス」→「ライブラリ」から **Google Sheets API** を有効化する
3. 「APIとサービス」→「認証情報」→「認証情報を作成」→「サービスアカウント」で作成する(名前例: `bid-service-writer`)
4. 作成したサービスアカウントの「キー」タブ →「鍵を追加」→「新しい鍵を作成」→ JSON形式でダウンロードする
5. JSONファイルの中身をそのまま GitHub Secrets の `GOOGLE_SERVICE_ACCOUNT_JSON` に登録する(次項参照)
6. JSON内の `"client_email"` の値を控えておく。後述のスプレッドシート共有設定で「編集者」として追加する

### ② Gmail送信の設定(ドメイン全体の委任)

メール送信は Gmail API(サービスアカウント + ドメイン全体の委任)で行う。Google Workspace
は2025年にSMTPの基本認証(ユーザー名+アプリパスワード)を廃止したため、アプリパスワードは
使わない。①のサービスアカウントに送信元ユーザーを代理送信する権限を1回だけ付与する。

1. Google Cloud Console でプロジェクトの **Gmail API** を有効化する
2. `admin.google.com` → セキュリティ → アクセスとデータ管理 → API の制御 →
   「ドメイン全体の委任を管理」→「新しく追加」
3. **クライアントID**: サービスアカウントJSONの `client_id` の値
4. **OAuthスコープ**: `https://www.googleapis.com/auth/gmail.send`
5. 承認する。これで、サービスアカウントが `settings.email.from_address` のユーザーとして
   (パスワードなしで)メール送信できる

### ③ GitHub Secretsへの登録

リポジトリの Settings → Secrets and variables → Actions で以下を登録する:

| Secret名 | 内容 |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ①で発行したサービスアカウントJSONキーの中身全体 |

送信専用のSMTPパスワードは不要(Gmail APIの委任で送るため)。スプレッドシートID等の
非秘匿情報は `config/settings.yaml` に直接記載する(共有権限で保護されるため秘匿情報
としては扱わない)。

送信経路が正しく設定できたかは、GitHub Actions の「Run workflow」で **mail_check**
にチェックを入れて実行するか、ローカルで `python main.py --mail-check` を実行して確認できる
(新着マッチの有無に関係なく、管理者=自分宛にテストメールを1通送って委任・スコープを検証する)。

### ④ 顧客マスタスプレッドシートの準備

1. Google Sheetsで空のスプレッドシートを新規作成する(例:「入札支援サービス 顧客マスタ」)
2. 共有設定で①のサービスアカウントのclient_emailを「編集者」として追加する
3. 「顧客マスタ」「条件プロファイル」「実行ログ」の3タブを作成する(ヘッダー行は最初の顧客追加時に自動で入る)
4. スプレッドシートURLからIDを取得し、`config/settings.yaml` の `google.customer_master_sheet_id` に設定する

## 顧客追加手順

1. Google Sheetsで顧客専用の空スプレッドシートを新規作成する
2. 共有設定でサービスアカウントのclient_emailを「編集者」として追加する
3. ローカルで認証情報を環境変数にセットする:
   ```
   export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service_account.json)"
   ```
4. セットアップスクリプトを実行する:
   ```
   python scripts/setup_customer_sheet.py \
     --customer-id C004 --company-name "株式会社サンプル" \
     --contact-name "山田太郎" --contact-email yamada@example.co.jp \
     --plan standard --sheet-id <手順1で作成したシートのID> \
     --keywords "消耗品,印刷,文具" --exclude-keywords "工事,保守" \
     --prefecture-codes "13,14,11,12" --qualification-grades "C,D" \
     --price-min 10000 --price-max 1000000 --organization-types "国,独法"
   ```
   これにより、顧客専用シートへのヘッダー・ステータス列ドロップダウンの設定と、顧客マスタ/条件プロファイル/実行ログ各タブへの行追加が行われる(ステータスは既定で `trial`)。
5. 内容を確認し、問題なければ顧客マスタの「ステータス」列を `active` に変更する(次回バッチから処理対象になる)

## ローカルでのテスト実行

```
python -m venv .venv
./.venv/Scripts/pip install -r requirements-bid-service.txt pytest
./.venv/Scripts/pytest -q
```

このテストスイートは実際のGoogle Sheets/Gmail/kkj.go.jp APIに一切接続しない
(フェイクgspreadクライアントで検証する)。ダミー顧客3社によるE2Eシナリオ
(正常完走・再実行時の重複なし・1社エラー時の他顧客継続)も `tests/test_main.py`
でカバーしている。

## 実際のGoogle API/Gmailを使ったE2E確認手順(受け入れ基準の最終確認)

1. 上記セットアップ①〜④を完了させる(②のドメイン全体の委任を含む)
2. ダミー顧客3社分を `setup_customer_sheet.py` で追加する。**メールアドレスは
   全て自分自身(`d.senba@souki-cp.co.jp`)を指定し、本番顧客のメールアドレスは
   絶対に使わない**
3. 手元で環境変数を設定し、まず送信経路だけを検証してから本実行する:
   ```
   export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service_account.json)"
   python main.py --mail-check   # 委任・スコープの検証(自分宛にテスト送信)
   python main.py                # 本実行
   ```
4. 顧客専用シートへの追記・管理者宛メールの着信・実行ログタブへの記録を確認する
5. **本番の入札管理台帳(`1Zo-LNjN33-hCqk97MaS1OClTa0UXpokiK9dV8yIg-cg`)には
   一切書き込まれていないことを確認する**(`customer_master_sheet_id`と各顧客の
   出力先スプレッドシートIDがすべてテスト用シートを指していることを事前に
   必ず確認すること)

## カバレッジ検証(データソースの妥当性確認)

`config/past_bids_reference.csv` に過去実際に応札した案件のキーワード・発注
機関名を1行1件で追記する(正確な案件名でなくてもよい)。

```
keyword,organization_name,note
消耗品,経済産業省,2026年5月応札
印刷用紙,○○市,2026年3月応札
```

追記後、以下を実行する:

```
python scripts/coverage_check.py
```

**ヒット率が低い場合、これは技術課題ではなく商品定義の問題である。** 自社の
入札実績が「オープンカウンタ(少額)」中心だった場合、kkj.go.jp APIでは十分に
拾えない可能性が高い。トライアル顧客への提供前に、商品説明の見直し(例:
「一般競争入札の案件レコメンド」への変更)や、フェーズ2以降での追加データ
ソース検討を行うこと。

## トラブルシューティング

| 症状 | 原因・対処 |
|---|---|
| ローカル実行時に `SSLCertVerificationError` | 社内プロキシ等でPythonの証明書ストアが信頼していない可能性。GitHub Actions(Ubuntu runner)では発生しない想定。ローカル確認が必要な場合はネットワーク管理者に確認する |
| kkj.go.jp APIが `<Error>` を返す | パラメータ不正またはAPI側の一時的な問題。main.pyはこれを検出すると(1顧客の失敗ではなく案件探索全体の失敗として)実行ログにエラー記録し、その回のバッチは中断する |
| 顧客シートに書き込めない(`KeyError`/権限エラー) | 顧客専用シートにサービスアカウントのclient_emailが編集者として共有されているか確認する |
| メール送信で `SMTPAuthenticationError (535 BadCredentials)` | GmailがSMTPログインを拒否している。①`BID_SERVICE_SMTP_PASSWORD`が通常パスワードではなくGoogleの**アプリパスワード(16桁)**であること、②送信元アカウントで**2段階認証が有効**であること、③`BID_SERVICE_SMTP_USER`が送信元アドレスと一致していること、を確認する。アプリパスワードは https://myaccount.google.com/apppasswords で再発行できる。Google Workspace(独自ドメイン)の場合は管理者がアプリパスワードを許可している必要がある。※前後の空白/改行やアプリパスワード表示の空白はコード側で自動除去する |

## フェーズ2設計メモ(今回は設計のみ・実装しない)

### 見積ドラフト自動生成(premiumプラン向け)

既存のgenka-check/mitsumori-createスキルは、「確認OK」「金額確定」という
**人間の明示確認が出るまで絶対に先に進まない**設計になっている(価格の推測・
流用による事故を防ぐための安全策)。これをそのまま無人の日次バッチに組み込む
ことはできない。フェーズ2では次のいずれかの方向性が必要:

- (a) 原価調査までを自動化し、見積書生成は人間の最終確認を必須ゲートとして残す(推奨)
- (b) 価格取得をAPI経由で確認できる仕入先に限定し、自動化範囲を明確に絞る
- (c) 完全自動生成はせず「見積ドラフトのたたき台」(原価候補・粗利計算例)のみを管理者向けに提示し、最終確認・提出は人間が行う

いずれの方向でも、既存スキルの絶対ルール(原価割れ禁止・最低粗利5%確保・
提出操作禁止)は維持する。

### 週次サマリレポート

顧客ごとの新着マッチ数推移と、顧客がシートの「ステータス」列を更新した内容
(応札結果)を集計し、管理者向けに週次でレポートするバッチを追加する。
実行ログタブの蓄積データを再利用する想定。
