<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="MCP-Plag：ChatGPT が MCP を通じて Mail.ru、Yandex Music、Telegram に接続">
</p>

<p align="center">
  <a href="README.md">Русский</a> · <a href="README.en.md">English</a> · <a href="README.zh-CN.md">简体中文</a> · <strong>日本語</strong> · <a href="README.de.md">Deutsch</a>
</p>

# MCP-Plag

**ChatGPT でメール、音楽、Telegram を操作。自分で運用できるサーバーのソースコードも公開。**

ChatGPT プラグインと MCP サーバーのコレクションです。パッケージ、コード、アイコン、導入手順、検証ツールをまとめています。Mail.ru、Yandex Music、Telegram の 3 つの連携を収録しています。

## 連携を選ぶ

| 連携 | できること | ソースとパッケージ |
| --- | --- | --- |
| **Mail / Mail.ru** | メールの検索・閲覧、下書き・添付ファイル・フォルダーの管理 | [概要](Mail.ru/README.md) · [アーカイブ](Mail.ru/releases/) |
| **Yandex Music** | 楽曲の検索、おすすめの取得、自分のプレイリストの管理 | [概要](Yandex.Music/README.md) · [アーカイブ](Yandex.Music/releases/) |
| **@tg / Telegram** | メッセージの閲覧・検索、名前付きアカウントの選択。実アカウントの初回ログインは未確認 | [概要](Telegram/README.md) · [アーカイブ](Telegram/releases/) · [状況](Telegram/STATUS.md) |

### Mail：メールから返信の下書きへ

> 「会議についてのメールを探し、スレッドを表示して、全員への返信を下書きとして保存して」

**Mail 1.0.0 · MCP 0.4.0 · 31 ツール。** `mail.ru`、`inbox.ru`、`list.ru`、`bk.ru`、`internet.ru` のメールボックスに対応。1 回のリクエストで最大 20 通を読み込み、添付付きの返信・転送、アーカイブ、既読状態の変更、フォルダーの管理ができます。

返信と転送は、既定で下書きとして保存されます。送信には明示的な依頼が必要です。完全削除やゴミ箱を空にする操作にも、個別の明示的な依頼が必要です。[Mail の機能と制限 →](Mail.ru/README.md)

### Yandex Music：検索からプレイリストへ

> 「このアーティストの曲を探して、選んだ曲を自分のプレイリストに追加して」

**プラグイン 1.0.3 · 音楽用ツール 14 個 · サーバースナップショット：2026 年 10 月 25 日。** カタログ検索、アーティストやアルバムの楽曲、チャート、お気に入り、再生履歴、My Wave、類似曲を取得できます。非公開プレイリストの作成、1 回につき最大 100 曲の追加、明示的な依頼による自分のプレイリストの削除に対応しています。

1 つのサーバー側アカウントを使う個人向けの構成です。音声のストリーミング再生やダウンロードには対応していません。[Yandex Music の機能と設定 →](Yandex.Music/README.md)

## 自分のサーバーで運用する

接続は **ChatGPT → MCP サーバー → 対象サービス** という流れです。プラグインのパッケージは ChatGPT と登録済みアプリを結び付け、サーバーがメールや音楽の操作を実行します。

1. 利用する連携の概要を開き、プラグインとサーバーのアーカイブをダウンロードします。
2. 各手順に従ってサーバーを配置し、依存関係、HTTPS、非公開の設定を用意します。
3. 自分のサーバーの HTTPS エンドポイントでアプリを登録し、パッケージ内のアプリとの関連付けを置き換えます。

公開パッケージ内の識別子は作者の環境に紐付いています。ZIP のコピーだけでは独立した連携は作成されません。現在のサーバーは **VDSina** 上で稼働しています。別の VPS/VDS や、外部からアクセスできる HTTPS アドレスを持つマシンにも導入できます。Yandex Music には SSH 接続モードも含まれています。

[Mail の導入手順](Mail.ru/server/README.md) · [Yandex Music の導入手順](Yandex.Music/server/README.md) · [@tg の導入手順](Telegram/server/README.md)

## データとアクセス範囲

- **秘密情報は別途設定します。** 本番環境のパスワード、トークン、鍵、データベース、ログは公開パッケージに含まれません。
- **Mail はアプリ用パスワードを使用します。** HTTPS のページからログインするため、チャットにパスワードを送る必要はありません。接続は個別に管理され、保存するパスワードは Fernet で暗号化されます。
- **Yandex Music はサーバー側のアカウントを使います。** OAuth によるアクセスはその所有者に限定されます。利用者ごとに異なる音楽アカウントを提供する構成ではありません。
- **サーバーがデータを処理します。** メール本文や添付ファイルなど、要求した結果は MCP を通じて ChatGPT に渡されます。設定、データベース、鍵は、そのサーバーの管理者が管理します。

Mail の添付ファイル制限、認証の条件、検証方法は各連携の手順を参照してください。Mail は小規模な個人運用を想定しています。利用を広げる前に、必要な操作をテスト用アカウントで確認してください。

## リポジトリの構成

```text
Mail.ru/           Mail プラグイン、MCP サーバー、テスト、ZIP
Yandex.Music/      Yandex Music プラグイン、MCP サーバー、ZIP
Telegram/          @tg プラグイン、MCP サーバー、アイコン、テスト、ZIP
assets/readme/     編集可能な SVG 接続図
README*.md        5 言語のプロジェクト紹介
```

リポジトリのルートからアーカイブを再構築し、内容とチェックサムを検証できます。

```sh
python Mail.ru/tools/build_releases.py
python Mail.ru/tools/verify_package.py
python Yandex.Music/tools/build_releases.py
python Yandex.Music/tools/verify_package.py
python Telegram/tools/build_releases.py
python Telegram/tools/verify_package.py
```

各連携のディレクトリにテスト手順とバージョン情報があります。アーカイブの構築に、実際のメールへのアクセスや音楽プレイリストの変更は必要ありません。詳しい連携手順は現在ロシア語で提供しています。
