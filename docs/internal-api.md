# Teams内部APIメモ

最終調査日: 2026-08-04

この文書は実装の保守用であり、非公開APIを正式仕様として扱うものではない。URL、ヘッダー、レスポンス形状は変更され得る。

## エンドポイント

| 機能 | 経路 | 認証 | 検証レベル |
|---|---|---|---|
| デバイスコード | `login.microsoftonline.com/organizations/oauth2/v2.0/devicecode` | なし | Microsoft OAuth標準＋公開実装で相互確認 |
| Skype token発行 | `teams.microsoft.com/api/authsvc/v1.0/authz` | Spaces audienceのBearer | 複数公開実装で相互確認 |
| テナント解決 | `teams.microsoft.com/api/mt/emea/beta/users/tenants` | Bearer | 公開実装で相互確認 |
| conversation一覧 | `{region}.ng.msg.teams.microsoft.com/v1/users/ME/conversations` | `Authentication: skypetoken=...` | fossteams、現行fork、Teams bundleで相互確認 |
| chat messages | `.../conversations/{id}/messages` | 同上 | 同上 |
| chat send | `.../conversations/{id}/messages` POST | 同上 | 現行MIT forkで相互確認 |
| CSA用Bearer | `login.microsoftonline.com/{tenant}/oauth2/v2.0/token` | refresh token、scope=`https://chatsvcagg.teams.microsoft.com/.default` | 実アカウントで検証 |
| team/channel snapshot | `teams.microsoft.com/api/csa/api/v1/teams/users/me` | chatsvcagg audienceのBearer | 実アカウントで検証 |
| channel messages | Chat Serviceの`conversations/{channelId}/messages` | `Authentication: skypetoken=...` | 実アカウントで検証 |
| thread replies | Chat Serviceの`conversations/{channelId};messageid={rootId}/messages` | 同上 | 実アカウントで検証 |
| transcript | 録画メッセージ内の`amsTranscript` URI | `Authorization: skype_token ...` | 実アカウントの非空VTTで検証 |
| WorkingSetFiles | `substrate.office.com/api/beta/me/WorkingSetFiles` | substrate audienceのBearer | トークン発行までは成功、対象テナントでは`403 ErrorAccessDenied` |

Teamsの2026-08-04時点の公開Web bundleにはCSAホスト、`/api/v1/`、`teams/users/me`、`groupchats`、`nonrecentchats`、message get/post/updateの識別子が残っていることも確認した。ただしminified bundleは契約ではない。

## 2026-08-04 実地検証結果

- Chat ServiceからDM本文、会議チャット本文を取得し、`RichText/Html`が空ではないことを確認した。検証ログには本文を出力していない。
- CSA aggregateから23チームとチャネルメタデータを取得した。
- チャネル本文はCSAのmessage APIではなくChat Serviceのchannel conversationから取得できた。親投稿と、実データを含む返信スレッドの双方を確認した。
- 直近の会議を走査し、AMSから非空のVTTを取得して話者付きエントリへ変換できた。
- substrate audienceのアクセストークンは発行できたが、`WorkingSetFiles`は`403 ErrorAccessDenied`だった。そのため、現行実装ではAMS文字起こしの代替経路にしない。
- 公開Web bundle上でMeeting Catchup、Meeting Artifacts、MCPS関連の設定値は観測したが、要求形式・権限・応答契約を安全に確定できなかった。AIリキャップ取得は未実装のまま明示する。

上記の件数は検証時点のアカウント状態であり、API仕様や製品上限ではない。

## Graphを使わない理由と代償

Microsoftの正式なTeams message取得経路はGraphのTeams Export APIであり、保護API権限が必要になる。Microsoftのサポート文書もTeams message dataへのサポート対象アクセスはGraphである旨を示している。本実装は「Graphを利用できない」という制約を満たす代わりに、次を受け入れる。

- Microsoftサポート対象外
- 突然のスキーマ／認証変更
- テナントポリシーによるブロック
- 機能ごとの可用性差（ライセンス、会議設定、保存期間）

参考:

- <https://learn.microsoft.com/en-us/microsoftteams/export-teams-content>
- <https://learn.microsoft.com/en-us/troubleshoot/exchange/send-emails/ews-not-support-access-teams-data>
- <https://learn.microsoft.com/en-us/microsoftteams/privacy/intelligent-recap>
- <https://learn.microsoft.com/en-us/microsoftteams/recording-transcription-overview>

## 実地検証時のルール

- 読み取りprobeは件数と表示名だけを出し、本文をコンソールへ表示しない。
- 実メッセージ本文の検証はMCPレスポンスを必要最小限に制限する。
- 書き込みは専用の宛先をallowlistし、ユーザーが文面と送信先を確認してから行う。
- DevToolsやHTTPログを保存する場合は、`Authorization`、`Authentication`、`X-Skypetoken`、Cookie、署名付きURLを除去する。
