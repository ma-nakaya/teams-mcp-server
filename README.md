# Teams MCP Server (Unofficial)

> [!CAUTION]
> **Unofficial / 非公式** — 本プロジェクトはMicrosoftによる公式提供、承認、提携、サポートを受けたものではありません。Teams Webの非公開APIを利用する実験的実装であり、API変更やテナントポリシーによって予告なく動作しなくなる可能性があります。利用者自身が、所属組織の利用規約、情報管理ポリシー、条件付きアクセス、対象データへの権限を確認してください。

Microsoft Graph APIとPlaywrightを使わず、ローカルのMicrosoft TeamsへアクセスするPython 3.11製MCPサーバーです。Python配布名やローカル保存領域には初期案の`teams-playwright-mcp-server`が残っていますが、現行実装にPlaywright依存はありません。

## 現在の機能

- OAuth 2.0デバイスコード認証（通常のブラウザーは認証承認にだけ使用）
- Windows資格情報マネージャーによる更新トークン／Skypeトークン保護
- DM・グループチャット・会議チャットの一覧と本文取得
- チーム／チャネル一覧、チャネル親投稿と返信の取得
- チャット／チャネルの自分の既読状態取得と未読フィルタ
- 会議録画メッセージからAMS VTT文字起こしを取得
- DM送信、チャネル投稿、チャネルスレッド返信
- 自分宛てチャットへのローカルPDF添付送信（実験的実装、互換性は環境に依存）
- 明示したメッセージ境界までのチャット／チャネル既読化
- プロセス間ファイルロックによるトークン更新の排他制御
- ホスト許可リスト、応答サイズ制限、書き込み三重ガード

AIリキャップと文字起こしは別物として扱います。現在検証できているのはVTT文字起こしです。AIノート／タスクの内部取得先はまだ安定性を確認できていないため、`ai_recap.status=not_implemented`を明示して返します。

## セットアップ

```powershell
uv sync --python 3.11 --all-groups
uv run teams-mcp login
uv run teams-mcp probe
```

`login`はMicrosoftのデバイスログイン画面を既定ブラウザーで開きます。ブラウザー自動起動が不要なら`--no-browser`を付けてください。認証情報は標準出力や設定ファイルには保存しません。

別端末や会話型の実地検証では、開始と完了を分けられます。短命なdevice codeもWindows資格情報マネージャーにだけ一時保存されます。

```powershell
uv run teams-mcp login-start
# ブラウザーで承認後
uv run teams-mcp login-complete
```

## MCPクライアント設定例

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

### 読み取りツール

| ツール | 用途 |
|---|---|
| `teams_auth_status` | 秘密を含まない認証状態。期限切れtokenは更新可能なら自動更新 |
| `list_recent_chats` | DM、グループ、会議チャット一覧。`read_state`と`unread_only`に対応 |
| `read_chat` | DM／グループ本文 |
| `list_teams` / `list_channels` | チーム／チャネル一覧。チャネルは`read_state`と`unread_only`に対応 |
| `read_channel_threads` | 親投稿と返信をまとめて取得 |
| `read_channel_thread` | 1スレッドの返信取得 |
| `list_meeting_chats` / `read_meeting_chat` | 会議チャット |
| `read_meeting_transcript` | 会議VTT文字起こし |
| `collect_meeting_recaps` | 最近の会議文字起こしを収集（互換名） |
| `collect_meeting_artifacts` | 上記の意味を明確にした別名 |

### 書き込みツール

書き込みは初期状態で必ず拒否されます。以下の3条件がすべて必要です。

1. `TEAMS_WRITES_ENABLED=true`
2. `TEAMS_WRITE_ALLOWLIST`に完全一致の宛先を列挙
3. ツール呼び出しごとに`confirm=true`

```powershell
$env:TEAMS_WRITES_ENABLED = "true"
$env:TEAMS_WRITE_ALLOWLIST = "chat:19:example@thread.v2,channel:team-id/channel-id"
$env:TEAMS_AGENT_MARKER = "[MCP]"
uv run teams-mcp-server
```

対象ツールは`send_chat_message`、`send_channel_post`、`reply_channel_thread`、`send_chat_file`です。テキスト送信の監査ログには宛先、文字数、SHA-256だけを記録し、本文やトークンは記録しません。`TEAMS_AGENT_MARKER`の既定値は`[MCP]`で、添付の説明にも設定済みのマーカーを適用します。

### 自分宛てPDF添付（実験的）

`send_chat_file(chat_id, file_path, request_id, confirm=false)`は、認証中の本人の自分宛てチャット`48:notes`だけを対象にします。既存の書き込み条件に加え、`TEAMS_WRITE_ALLOWLIST`への完全一致の宛先`chat:48:notes`の登録が必要です。ファイルと送信先への明示的な送信依頼を確認し、`teams_auth_status`と現在のselfチャットを確認してから呼び出してください。

`file_path`はMCPサーバーが動く端末上の通常ファイルであるPDFの絶対パスです。ネットワークパス、シンボリックリンク、reparse pointは拒否します。容量は0バイトより大きく、10 MiBと`TEAMS_MAX_RESPONSE_BYTES`の小さい方以下、ファイル名は240文字以下かつUTF-8で255バイト以下です。制御文字や不正なUnicodeを含む名前は拒否します。

1回の送信にUUIDの`request_id`を1つ割り当て、`confirm=true`で呼び出します。タイムアウトや結果不明時も同じUUIDを使ってください。既存の処理記録がある場合は送達確認だけを行い、自動再送はしません。確認範囲内にメッセージが見つからなくても未送信とは判断できないため、新しいUUIDで再送しないでください。

成功と報告できるのは`status=sent_and_verified`かつ`attachment_verified=true`の場合だけです。メッセージ内の識別子とファイル名、添付を再取得した内容と元のPDFの一致を確認します。エラーは`isError=true`と構造化された`error_code`、`message`を返します。`send_outcome_unknown`や`file_operation_ambiguous`は結果不明、`file_acl_unverified`や`file_verification_failed`は検証失敗として停止します。詳細は[ツール契約](skills/use-teams-mcp/references/tool-contract.md#experimental-self-chat-pdf-attachments)を参照してください。読み取り、添付送信、送達確認では既読状態を変更しません。

AMSへの作成要求は、認証中の本人だけに読み取り権限を指定します。その要求に対するHTTP 201を権限指定が受理された根拠とし、テナント、本人の識別子、ファイル名とメモリ内で結び付けます。再開時は`creation_policy=self_read_v1`、一致するfingerprint、`version=1`、既知のphaseを持つ検証済み処理記録からだけ復元します。AMSのGET応答から実際のACLは再取得できないため、サーバー側ACLを独立に検証したことにはなりません。アップロード前と投稿前にオブジェクトのメタデータを確認し、応答に含まれる権限などに矛盾があれば停止します。

既存のSkype認証セッションと非公開のlegacy AMS APIを使い、新しいOAuth scopeやブラウザーCookieを取得せず、公開・組織全体への共有リンクも作りません。AMSはサポート対象外の内部APIで、添付の受理・表示はTeams環境に依存します。単一環境での動作確認は、すべてのテナントでの互換性を保証しません。機密ファイルを扱う前に、明示的に許可された非機密PDFで本人宛ての動作を確認してください。新たなIC3やGraphの権限が必要な場合は停止して報告し、自動取得しません。

処理記録にはハッシュ、送達識別子、作成ポリシーと処理段階を保存し、ファイル名、ローカルパス、PDF本文、トークンは保存しません。添付確認の監査ログはself宛先、バイト数、ハッシュのみです。投稿前の失敗で、本人限定の権限を要求した空または未投稿のAMSオブジェクトが残る場合がありますが、自動削除は行いません。

### 既読状態

`list_recent_chats`と`list_meeting_chats`はCSAの`isRead`を、`list_channels`は`isMessageRead`を`read_state=read|unread|unknown`へ正規化します。`unread_only=true`では、CSAが明示的に未読とした対象だけを返し、状態不明を未読とは推測しません。チャネルの`isImportantMessageRead`は`important_message_read_state`として通常メッセージと分離します。Activity feed、mention、通知badge、他メンバーのread receiptとは同一視しません。

本文取得ツールは既読状態を変更しません。明示的な既読化は`mark_chat_read`または`mark_channel_read`を使用し、message送信とは別の3条件をすべて要求します。

1. `TEAMS_READ_STATE_WRITES_ENABLED=true`
2. `TEAMS_READ_STATE_ALLOWLIST`に完全一致の宛先を列挙
3. ツール呼び出しごとに`confirm=true`

さらに、`through_message_id`または`through_latest=true`のどちらか一方だけを必須とします。現在の既読位置以下ならPUTせず`already_read`を返し、最新messageより先の境界や非数値IDは拒否します。

```powershell
$env:TEAMS_READ_STATE_WRITES_ENABLED = "true"
$env:TEAMS_READ_STATE_ALLOWLIST = "chat:19:example@thread.v2"
uv run teams-mcp-server
```

既読化はTeamsの他端末にも同期され、read receiptが有効なchatでは相手のSeen表示へ影響し得ます。`through_latest=true`は実行時点の最新messageまで進める指定です。初回の実地検証には、専用chat／channelだけをallowlistしてください。

`teams_auth_status`は期限切れのSkype tokenを検出すると、Windows資格情報マネージャー内の更新トークンで自動復旧を試みます。`usable=false`かつ`reauthentication_required=true`の場合にだけ、`uv run teams-mcp login`による再認証が必要です。

## 開発

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

詳しい設計判断は[docs/design.md](docs/design.md)、観測済みAPIと検証レベルは[docs/internal-api.md](docs/internal-api.md)を参照してください。

## 参考実装・資料

- [fossteams/teams-api](https://github.com/fossteams/teams-api) — MITライセンス。Chat Serviceの基本的な認証形式とエンドポイントの参考
- [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api) — MITライセンス。現行MCP実装と文字起こし経路の相互確認
- [m0nkmaster/msteams-mcp](https://github.com/m0nkmaster/msteams-mcp) — MITライセンス。Substrate WorkingSetFiles経路の相互確認（対象テナントでは403のため未採用）
- [agent-messenger/agent-messenger](https://github.com/agent-messenger/agent-messenger) — デバイスコード認証と現行エンドポイントの挙動確認（コードは流用していません）
- [theophile-wallez/teams-lite](https://github.com/theophile-wallez/teams-lite) — `consumptionhorizon`による既読位置の取得／更新と外部状態変更の安全境界を相互確認
- [Microsoft: Export Teams content](https://learn.microsoft.com/en-us/microsoftteams/export-teams-content) — 正式なメッセージ取得方法はGraphベース
- [Microsoft: Intelligent recap privacy](https://learn.microsoft.com/en-us/microsoftteams/privacy/intelligent-recap) — リキャップ、文字起こし、保存先の区別

## ライセンス

MIT
