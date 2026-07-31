# teams-mcp-server

認証済みの Microsoft Teams Web セッションを Playwright CLI で操作し、必要な情報だけを構造化して返す読み取り専用 MCP サーバーです。

Phase 1 では、指定日の会議チャットから AI リキャップを取得する `collect_meeting_recaps` のみを公開します。Teams の HTML、snapshot 全体、Cookie、認証情報は MCP レスポンスへ含めません。

> [!IMPORTANT]
> **公開状況:** Phase 1のプロトタイプです。単体テストは完了していますが、実際のTeams環境を使ったセレクタ調整とSecure MCP Tunnel経由の疎通確認は未完了です。利用可能な完成版としてXなどで紹介するのは、実機確認とPRのマージ後を想定しています。

## 前提

- Windows 11
- Python 3.11 以上
- Node.js 20 以上
- `npm install -g @playwright/cli`
- Secure MCP Tunnel
- Teams Web にログインできる Edge persistent profile

Microsoft Graph API と Entra ID アプリ登録は使用しません。

## セットアップ

```powershell
git clone https://github.com/ma-nakaya/teams-mcp-server.git
cd teams-mcp-server
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

最初にブラウザを表示して Teams の認証を完了します。

```powershell
teams-authenticate
```

通常の取得処理では対話入力や MFA を要求しません。セッションが失効した場合は `authentication_required` を返します。

## CLI

標準出力を機械可読に保つため、JSON モードでは JSON 以外を stdout に出力しません。

```powershell
teams-recaps --date 2026-07-22 --format json
teams-recaps --date 2026-07-22 --format markdown --output .\recaps\2026-07-22.md
```

## MCP

stdio サーバーを起動します。

```powershell
teams-mcp
```

公開ツール:

```json
{
  "name": "collect_meeting_recaps",
  "arguments": {
    "date": "2026-07-22"
  }
}
```

主な結果ステータスは `ok`、`authentication_required`、`busy`、`no_recap`、`no_access`、`timeout`、`ui_changed`、`internal_error` です。

## 設定

| 環境変数 | 用途 |
|---|---|
| `PLAYWRIGHT_CLI` | `playwright-cli` を自動検出できない場合の絶対パス |
| `TEAMS_MCP_LOCK_PATH` | 共有セッションのロックファイル。既定は `%PROGRAMDATA%\\teams-mcp\\teams-session.lock` |

Playwright のセッション名は `teams` 固定です。別の読み取り処理と同時に使う場合も、同じロックファイルを共有してください。

## 開発

```powershell
python -m unittest discover -s tests
ruff check .
```

実際の Teams snapshot は機密情報を除去したうえで `tests/fixtures/` に追加し、UI変更への追従は parser の fixture テストから行います。

## 現在の制約

- Teams UI と表示言語の変更に影響されます。
- 一覧外の過去会議や仮想スクロールの完全取得には未対応です。
- 会議一覧の実 snapshot を用いたセレクタ調整が必要です。
- チャット、チャネル投稿、横断検索は後続 Phase です。

設計: [Teams Chat / Meeting Recap MCPサーバー設計（Playwright方式）](https://app.notion.com/p/3a5e3de47ee08181890bce6f64ba173c)
