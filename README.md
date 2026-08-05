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

対象ツールは`send_chat_message`、`send_channel_post`、`reply_channel_thread`です。監査ログには宛先、文字数、SHA-256だけを記録し、本文やトークンは記録しません。

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
