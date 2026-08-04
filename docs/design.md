# 設計

## 結論

初期案の「PlaywrightでTeams Webを操作し、snapshotを解析する」方式は主経路から外した。DOMは頻繁に変わり、本文抽出の誤爆、ブラウザー常駐、画面ロック、並行操作の競合が避けにくいためである。

代わりに、Teamsクライアント自身が利用する通信を小さなAPIクライアントとして実装する。

## 非公式性とサポート境界

本プロジェクトはMicrosoftによる公式提供、承認、提携、サポートを受けたものではない。Teams Webの非公開APIを利用するため、Microsoftの製品仕様や外部統合契約として互換性を期待してはならない。利用者は所属組織の規約、情報管理、条件付きアクセス、対象データへの権限を確認し、内部API変更への追従と運用停止の判断を自身で行う。

```text
MCP client
    |
    v
TeamsService ---- WriteGuard (default deny / exact allowlist / per-call confirm)
    |
    +---- Chat Service -------- DM / group / meeting / channel content / send
    |
    +---- CSA ----------------- teams / channels metadata
    |
    +---- AMS ----------------- meeting transcript (VTT)
    |
    v
AuthManager ---- OAuth device code ---- Microsoft sign-in
    |
    +---- Windows Credential Manager (long-lived secrets)
    +---- file lock (cross-process refresh exclusion)
```

Graph APIはスコープ要求にも通信先にも含めない。Playwright、Selenium、ブラウザーのCookie抽出も行わない。

## 認証境界

1. Teamsデスクトップ向け公開クライアントIDを既定値としてOAuth 2.0デバイスコードフローを開始する。
2. `https://api.spaces.skype.com/.default openid profile offline_access`だけを要求する。
3. 実テナントを解決し、Teams authzサービスで短命なSkypeトークンを発行する。
4. 更新トークンとSkypeトークンはOS資格情報ボルトに格納する。安全なkeyringがない環境では失敗させ、平文ファイルへフォールバックしない。
5. MCPレスポンス、例外、監査ログへトークン、Cookie、署名付き文字起こしURLを出さない。

公開クライアントIDの再利用と非公開APIはMicrosoftの正式な外部統合契約ではない。組織が承認した独自公開クライアントを用意できる場合は`TEAMS_CLIENT_ID`で差し替える。

## 読み取りモデル

### DM・グループ・会議チャット

Chat Serviceのconversation一覧を共通入口にし、`threadProperties`とconversation IDから分類する。本文はJSONの`content`だけをHTMLテキスト化するため、Teams画面全体を誤って返すことはない。

過去方向のページングには`_metadata.backwardLink`を使う。リンク先ホストと認証リージョンが一致しない場合は拒否する。`syncState`は新着同期用であり、過去ログ取得には使わない。

### チャネルスレッド

CSA aggregateからチーム／チャネルのメタデータを取得する。本文はChat Serviceのchannel conversationを読み、`rootMessageId`で親投稿と返信を分ける。特定スレッドは`channelId;messageid=rootId` conversationとして取得し、`Thread { root, replies }`へ正規化する。

### 会議ログ

`RichText/Media_CallRecording`メッセージのXMLから、成功済み録画に含まれる`amsTranscript`だけを選ぶ。AMSへ`Authorization: skype_token ...`でVTTを取得し、話者・開始・終了・本文へ変換する。署名付きURLへリダイレクトされた場合、Teamsトークンを外し、許可済みストレージホストにだけ接続する。

### AIリキャップ

AIリキャップ、AIノート、タスクはVTT文字起こしと同一ではない。Microsoftの説明ではAIノート等はExchange mailbox側、録画はOneDrive/SharePoint側にも関係する。

公開Web bundleとMITライセンスの現行参考実装からSubstrate `WorkingSetFiles`経路を特定し、対象テナントで実地検証した。substrate audienceのトークン発行には成功したが、APIは`403 ErrorAccessDenied`を返した。Meeting Catchup／Meeting Artifacts／MCPSの設定値も観測したものの、安定した要求・応答契約は確定できていない。このため、通常のMCPツールはSubstrateを呼ばず、AIリキャップは`not_implemented`とし、文字起こしを「AIリキャップ」と偽装しない。

## 書き込みモデル

書き込みは技術的に可能でも、読み取りより事故影響が大きい。そこで次の条件をコード上で強制する。

- サーバー起動時設定`TEAMS_WRITES_ENABLED=true`
- `chat:<conversation-id>`または`channel:<team-id>/<channel-id>`の完全一致許可
- 個別呼び出しの`confirm=true`
- 最大4,000文字
- 既定`[MCP]`マーカー
- 監査ログは本文ではなくSHA-256

初期版では送信と返信だけを許可し、編集・削除・リアクション・ファイル添付は実装しない。不可逆性やなりすまし範囲を広げないためである。

## 故障モード

| 故障 | 挙動 |
|---|---|
| API変更 | 安定したエラーコードで停止。DOM操作へ自動フォールバックしない |
| 401 | ファイルロック下で1回だけ再発行して再試行 |
| 429 / 5xx | 上限付きリトライ |
| 未知ホスト | 認証ヘッダー送信前に拒否 |
| 巨大レスポンス | 設定済みバイト上限で拒否 |
| keyring不在 | 平文保存せず認証失敗 |
| 書き込み設定不足 | API呼び出し前に拒否 |

## 今後の検証順

1. デバイスコード認証とリージョン解決
2. conversation一覧、DM本文、会議チャット分類
3. チーム／チャネル一覧、親投稿、返信
4. 実際に文字起こしが存在する会議でAMS VTT
5. 専用テストチャット／チャネルだけをallowlistし、短いテスト投稿
6. AIリキャップ画面を利用できる専用テスト会議で、秘密除去したネットワーク記録を比較し、Meeting Catchup／Meeting Artifacts／MCPSの契約を調査
