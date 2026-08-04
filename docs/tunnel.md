# OpenAI Secure MCP Tunnel運用

## 構成

Tunnel Clientがローカルで`teams-mcp-server`をstdio子プロセスとして起動し、OpenAI control planeへアウトバウンド接続する。

```text
ChatGPT / approved workspace
        |
        v
OpenAI MCP control plane
        ^ outbound poll
        |
Tunnel Client --stdio--> teams-mcp-server --> Teams internal APIs
```

Teamsの更新トークンとSkypeトークンはWindows資格情報マネージャーに残る。control plane用APIキーもprofileへ直書きせず、`env:CONTROL_PLANE_API_KEY`を参照する。

## Profile作成

以下は`C:\Apps\TunnelClient\tunnel-client.exe`が導入済みの場合の例である。実際のTunnel IDと作業ディレクトリへ置き換える。

```powershell
$tunnelClient = "C:\Apps\TunnelClient\tunnel-client.exe"
$projectDirectory = "C:\path\to\teams-playwright-mcp-server"
$uv = (Get-Command uv).Source.Replace("\", "/")
$mcpCommand = "$uv --directory $($projectDirectory.Replace('\', '/')) run teams-mcp-server"

& $tunnelClient init `
  --profile teams-local `
  --sample sample_mcp_stdio_local `
  --tunnel-id tunnel_REPLACE_ME `
  --mcp-command $mcpCommand `
  --health-listen-addr 127.0.0.1:0

& $tunnelClient doctor --profile teams-local --json --explain
```

生成先は`%APPDATA%\tunnel-client\teams-local.yaml`である。APIキーは次のようにユーザー環境へ安全に設定し、YAML、コマンドライン、ログへ値を含めない。

```powershell
[Environment]::SetEnvironmentVariable(
  "CONTROL_PLANE_API_KEY",
  "<runtime API key>",
  "User"
)
```

## 起動とhealth確認

health listenerは`127.0.0.1:0`で動的ポートを使い、URL、PID、ログはmachine-localディレクトリへ保存する。

```powershell
$runtimeDirectory = Join-Path $env:LOCALAPPDATA "TeamsMcpTunnel"
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

& "C:\Apps\TunnelClient\tunnel-client.exe" run `
  --profile teams-local `
  --health.url-file (Join-Path $runtimeDirectory "health-url.txt") `
  --pid.file (Join-Path $runtimeDirectory "tunnel-client.pid") `
  --log.file (Join-Path $runtimeDirectory "tunnel-client.log") `
  --log.format json
```

別プロセスから次を実行すると、PID、`/healthz`、`/readyz`に加え、control plane poll成功まで確認できる。

```powershell
& "C:\Apps\TunnelClient\tunnel-client.exe" health `
  --url-file "$env:LOCALAPPDATA\TeamsMcpTunnel\health-url.txt" `
  --pid-file "$env:LOCALAPPDATA\TeamsMcpTunnel\tunnel-client.pid" `
  --require-control-plane-poll `
  --json
```

常駐運用では、ユーザーログオン時に非表示で起動するScheduled Taskを使用する。タスクは`Limited`権限、`MultipleInstances=IgnoreNew`、異常終了時の回数制限付き再起動とし、loopback healthを監視するSupervisorへ`teams-local` profileを登録する。

## セキュリティ境界

- Tunnelを関連付けるOpenAI organization/workspaceは、Teams情報へのアクセスを許可した範囲に限定する。
- Tunnel経由の読み取り結果はcontrol planeへ返る。ローカル保存だけに閉じる構成ではない。
- `--allow-remote-ui`を指定せず、admin UIをloopbackから公開しない。
- `--log.http-raw-unsafe`を使用しない。ログへMCP payloadや認証ヘッダーを保存しない。
- 書き込みツールはTunnel接続後も、`TEAMS_WRITES_ENABLED`、宛先allowlist、`confirm=true`の三条件が必要である。
- Tunnel IDはprofileに必要だが、runtime APIキーやTeamsトークンをリポジトリへコミットしない。
