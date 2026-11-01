# Teams MCP Server（非公式）

> [!CAUTION]
> **Unofficial／非公式** — 本プロジェクトは Microsoft による公式提供・承認・提携・サポートを受けたものではありません。Teams Web の非公開 API を利用する実験的実装であり、API 変更やテナントポリシーによって予告なく動作しなくなる可能性があります。利用者自身が、所属組織の利用規約、情報管理ポリシー、条件付きアクセス、対象データへの権限を確認してください。

Microsoft Graph API と Playwright を使わず、ローカルの Microsoft Teams へアクセスする Python 3.11 製の MCP サーバーです。認証は OAuth 2.0 デバイスコードフロー、トークンの保管は Windows 資格情報マネージャーを使います。チャット／チャネルの読み取りを中心に、送信と既読化は三重ガード付きで提供します。

なお、Python 配布名とローカル保存領域には初期案の `teams-playwright-mcp-server` という名前が残っていますが、現行実装に Playwright 依存はありません。

## 特徴

- Graph API のアプリ登録なしで、Teams Web の非公開 API を利用
- デバイスコード認証（ブラウザーは認証承認にだけ使用）
- 更新トークン／Skype トークンは Windows 資格情報マネージャーに保護して保存し、プロセス間ファイルロックで更新を排他制御
- 書き込みは既定で拒否。有効化フラグ、宛先 allowlist、呼び出しごとの `confirm=true` の 3 条件がそろった場合だけ実行
- ホスト許可リストと応答サイズ制限

## 動作要件

- Windows（資格情報マネージャーを使用）
- Python 3.11 以上と [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Microsoft Teams を利用できるアカウント

## セットアップ

```powershell
uv sync --python 3.11 --all-groups
uv run teams-mcp login
uv run teams-mcp probe
```

`login` は Microsoft のデバイスログイン画面を既定ブラウザーで開きます。ブラウザーの自動起動が不要なら `--no-browser` を付けてください。認証情報は標準出力や設定ファイルには保存しません。

別端末での検証など、開始と完了を分けたい場合は短命な device code も Windows 資格情報マネージャーにだけ一時保存されます。

```powershell
uv run teams-mcp login-start
# ブラウザーで承認した後
uv run teams-mcp login-complete
```

`teams_auth_status` は期限切れの Skype トークンを検出すると、資格情報マネージャー内の更新トークンで自動復旧を試みます。`usable=false` かつ `reauthentication_required=true` の場合にだけ、`uv run teams-mcp login` による再認証が必要です。

## MCP クライアント設定例

作業ディレクトリは実際の絶対パスへ置き換えてください。

```json
{
  "mcpServers": {
    "teams-local": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\path\\to\\teams-mcp-server",
        "run",
        "teams-mcp-server"
      ]
    }
  }
}
```

## ツール一覧

### 読み取り

| ツール | 用途 |
| --- | --- |
| `teams_auth_status` | 秘密を含まない認証状態。期限切れトークンは更新可能なら自動更新 |
| `list_recent_chats` | DM、グループ、会議チャットの一覧。`read_state` と `unread_only` に対応 |
| `read_chat` | DM／グループチャットの本文 |
| `list_teams` / `list_channels` | チーム／チャネルの一覧。チャネルは `read_state` と `unread_only` に対応 |
| `read_channel_threads` | チャネルの親投稿と返信をまとめて取得 |
| `read_channel_thread` | 1 スレッドの返信を取得 |
| `list_meeting_chats` / `read_meeting_chat` | 会議チャットの一覧と本文 |
| `read_meeting_transcript` | 会議録画メッセージから AMS VTT 文字起こしを取得 |
| `collect_meeting_recaps` | 最近の会議文字起こしを収集（互換名） |
| `collect_meeting_artifacts` | 上記の意味を明確にした別名 |

AI リキャップと文字起こしは別物として扱います。現在検証できているのは VTT 文字起こしです。AI ノート／タスクの内部取得先は安定性を確認できていないため、`ai_recap.status=not_implemented` を明示して返します。

### 書き込み

書き込みは初期状態で必ず拒否されます。次の 3 条件がすべて必要です。

1. `TEAMS_WRITES_ENABLED=true`
2. `TEAMS_WRITE_ALLOWLIST` に完全一致の宛先を列挙
3. ツール呼び出しごとに `confirm=true`

```powershell
$env:TEAMS_WRITES_ENABLED = "true"
$env:TEAMS_WRITE_ALLOWLIST = "chat:19:example@thread.v2,channel:team-id/channel-id"
$env:TEAMS_AGENT_MARKER = "[MCP]"
uv run teams-mcp-server
```

対象ツールは `send_chat_message`、`send_channel_post`、`reply_channel_thread`、`send_chat_file` です。テキスト送信の監査ログには宛先、文字数、SHA-256 だけを記録し、本文やトークンは記録しません。`TEAMS_AGENT_MARKER` の既定値は `[MCP]` で、添付の説明にも設定済みのマーカーを適用します。

### 既読化

対象ツールは `mark_chat_read`、`mark_channel_read` です。本文取得ツールは既読状態を変更しません。既読化は送信とは別の 3 条件をすべて要求します。

1. `TEAMS_READ_STATE_WRITES_ENABLED=true`
2. `TEAMS_READ_STATE_ALLOWLIST` に完全一致の宛先を列挙
3. ツール呼び出しごとに `confirm=true`

さらに、`through_message_id` または `through_latest=true` のどちらか一方だけを必須とします。現在の既読位置以下なら PUT せず `already_read` を返し、最新 message より先の境界や非数値 ID は拒否します。

```powershell
$env:TEAMS_READ_STATE_WRITES_ENABLED = "true"
$env:TEAMS_READ_STATE_ALLOWLIST = "chat:19:example@thread.v2"
uv run teams-mcp-server
```

既読化は Teams の他端末にも同期され、read receipt が有効な chat では相手の Seen 表示へ影響し得ます。`through_latest=true` は実行時点の最新 message まで進める指定です。初回の実地検証では、専用 chat／channel だけを allowlist してください。

既読状態の正規化について: `list_recent_chats` と `list_meeting_chats` は CSA の `isRead` を、`list_channels` は `isMessageRead` を `read_state=read|unread|unknown` へ正規化します。`unread_only=true` では、CSA が明示的に未読とした対象だけを返し、状態不明を未読とは推測しません。チャネルの `isImportantMessageRead` は `important_message_read_state` として通常メッセージと分離します。Activity feed、mention、通知 badge、他メンバーの read receipt とは同一視しません。

## 設定

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `TEAMS_CREDENTIAL_PROFILE` | `default` | 資格情報マネージャー内のプロファイル名 |
| `TEAMS_CLIENT_ID` | 既定値あり | デバイスコード認証に使うクライアント ID |
| `TEAMS_REGION` | `apac` | 接続リージョン（`amer`／`emea`／`apac`） |
| `TEAMS_WRITES_ENABLED` | `false` | 送信系ツールの有効化 |
| `TEAMS_WRITE_ALLOWLIST` | 空 | 送信を許可する宛先（カンマ区切り、完全一致） |
| `TEAMS_READ_STATE_WRITES_ENABLED` | `false` | 既読化ツールの有効化 |
| `TEAMS_READ_STATE_ALLOWLIST` | 空 | 既読化を許可する宛先（カンマ区切り、完全一致） |
| `TEAMS_AGENT_MARKER` | `[MCP]` | 送信メッセージへ付けるマーカー |
| `TEAMS_REQUEST_TIMEOUT_SECONDS` | `30` | リクエストのタイムアウト（秒、正の値） |
| `TEAMS_MAX_RESPONSE_BYTES` | `10000000` | 応答サイズ上限（1024 以上） |
| `TEAMS_LOG_LEVEL` | `INFO` | ログレベル |

## 自分宛て PDF 添付（実験的）

`send_chat_file(chat_id, file_path, request_id, confirm=false)` は、認証中の本人の自分宛てチャット `48:notes` だけを対象にします。通常の書き込み条件に加え、`TEAMS_WRITE_ALLOWLIST` への完全一致の宛先 `chat:48:notes` の登録が必要です。ファイルと送信先への明示的な送信依頼を確認し、`teams_auth_status` と現在の self チャットを確認してから呼び出してください。

- `file_path` は MCP サーバーが動く端末上の通常ファイルである PDF の絶対パスです。ネットワークパス、シンボリックリンク、reparse point は拒否します。
- 容量は 0 バイトより大きく、10 MiB と `TEAMS_MAX_RESPONSE_BYTES` の小さい方以下。ファイル名は 240 文字以下かつ UTF-8 で 255 バイト以下です。制御文字や不正な Unicode を含む名前は拒否します。
- 1 回の送信に UUID の `request_id` を 1 つ割り当て、`confirm=true` で呼び出します。タイムアウトや結果不明時も同じ UUID を使ってください。既存の処理記録がある場合は送達確認だけを行い、自動再送はしません。確認範囲内にメッセージが見つからなくても未送信とは判断できないため、新しい UUID で再送しないでください。
- 成功と報告できるのは `status=sent_and_verified` かつ `attachment_verified=true` の場合だけです。メッセージ内の識別子とファイル名、添付を再取得した内容と元の PDF の一致を確認します。エラーは `isError=true` と構造化された `error_code`、`message` を返します。`send_outcome_unknown` や `file_operation_ambiguous` は結果不明、`file_acl_unverified` や `file_verification_failed` は検証失敗として停止します。詳細は[ツール契約](skills/use-teams-mcp/references/tool-contract.md#experimental-self-chat-pdf-attachments)を参照してください。
- 読み取り、添付送信、送達確認では既読状態を変更しません。

AMS への作成要求は、認証中の本人だけに読み取り権限を指定します。その要求に対する HTTP 201 を、権限指定が受理された根拠とし、テナント、本人の識別子、ファイル名とメモリ内で結び付けます。再開時は `creation_policy=self_read_v1`、一致する fingerprint、`version=1`、既知の phase を持つ検証済み処理記録からだけ復元します。AMS の GET 応答から実際の ACL は再取得できないため、サーバー側 ACL を独立に検証したことにはなりません。アップロード前と投稿前にオブジェクトのメタデータを確認し、応答に含まれる権限などに矛盾があれば停止します。

既存の Skype 認証セッションと非公開の legacy AMS API を使い、新しい OAuth scope やブラウザー Cookie は取得せず、公開・組織全体への共有リンクも作りません。AMS はサポート対象外の内部 API で、添付の受理・表示は Teams 環境に依存します。単一環境での動作確認は、すべてのテナントでの互換性を保証しません。機密ファイルを扱う前に、明示的に許可された非機密 PDF で本人宛ての動作を確認してください。新たな IC3 や Graph の権限が必要な場合は停止して報告し、自動取得しません。

処理記録にはハッシュ、送達識別子、作成ポリシーと処理段階を保存し、ファイル名、ローカルパス、PDF 本文、トークンは保存しません。添付確認の監査ログは self 宛先、バイト数、ハッシュのみです。投稿前の失敗で、本人限定の権限を要求した空または未投稿の AMS オブジェクトが残る場合がありますが、自動削除は行いません。

## セキュリティと制約

- トークンは Windows 資格情報マネージャーに保存し、標準出力・設定ファイル・監査ログへ出しません
- 送信の監査ログは宛先・文字数・SHA-256 のみです
- 書き込みと既読化はどちらも既定で無効で、allowlist と呼び出しごとの確認が必須です
- 対応しないもの: AI リキャップの取得（未実装を明示して返却）、Graph API 経由の正式な取得

詳しい設計判断は [docs/design.md](docs/design.md)、観測済み API と検証レベルは [docs/internal-api.md](docs/internal-api.md)、添付の仕様は [docs/file-attachments.md](docs/file-attachments.md) を参照してください。

## 開発

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## 参考実装・資料

- [fossteams/teams-api](https://github.com/fossteams/teams-api) — MIT ライセンス。Chat Service の基本的な認証形式とエンドポイントの参考
- [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api) — MIT ライセンス。現行 MCP 実装と文字起こし経路の相互確認
- [m0nkmaster/msteams-mcp](https://github.com/m0nkmaster/msteams-mcp) — MIT ライセンス。Substrate WorkingSetFiles 経路の相互確認（対象テナントでは 403 のため未採用）
- [agent-messenger/agent-messenger](https://github.com/agent-messenger/agent-messenger) — デバイスコード認証と現行エンドポイントの挙動確認（コードは流用していません）
- [theophile-wallez/teams-lite](https://github.com/theophile-wallez/teams-lite) — `consumptionhorizon` による既読位置の取得／更新と外部状態変更の安全境界を相互確認
- [Microsoft: Export Teams content](https://learn.microsoft.com/en-us/microsoftteams/export-teams-content) — 正式なメッセージ取得方法は Graph ベース
- [Microsoft: Intelligent recap privacy](https://learn.microsoft.com/en-us/microsoftteams/privacy/intelligent-recap) — リキャップ、文字起こし、保存先の区別

## ライセンス

MIT License（`LICENSE` を参照）
