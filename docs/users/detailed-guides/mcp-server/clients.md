# Connect an MCP client

Connect your assistant or IDE to HarborRAG's search, evidence, and graph tools.
Use the [MCP quick start](setup-and-integration.md) to prepare a running HarborRAG
stack with at least one indexed source and grant the reader its read-only database
and object-store access.

| Client | Local stdio | Streamable HTTP |
| --- | --- | --- |
| Claude Desktop | [Claude Desktop](#claude-desktop) | |
| Claude Code | [Claude Code](#claude-code) | |
| Cursor | [Cursor](#cursor) | [Cursor HTTP](#cursor-over-http) |
| VS Code | [VS Code](#vs-code) | [VS Code HTTP](#vs-code-over-http) |
| Codex | [Codex](#codex) | [Codex HTTP](#codex-over-http) |
| Zed | [Zed](#zed) | |
| Gemini CLI | [Gemini CLI](#gemini-cli) | |
| Another MCP client | [Stdio setup](#connect-over-stdio) | [HTTP setup](#connect-an-http-client) |

## Connect over stdio

Use an absolute path to the checkout's `scripts/deployment/mcp.sh` as the
client's command, with no arguments. The client starts it when needed. Stdio
doesn't open a port or use an MCP token. The client must run where it can reach
that path, Bash, Docker, and the Docker daemon running HarborRAG.

Replace `/path/to/HarborRAG` with your checkout path and merge the `harborrag`
entry into the client's existing settings. If you installed the reader as a
Python package, set the command to the absolute path of `harborrag-mcp` and
pass `--env-file` and the path to the operator-provided environment file as
separate arguments. See [launcher options](server-operations.md#run-the-wrapper-and-forward-server-options).

### Claude Desktop

Open **Settings → Developer → Edit Config**. Add the entry to
`claude_desktop_config.json`, then restart Claude Desktop.
[Claude Desktop local MCP setup](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

```json
{
  "mcpServers": {
    "harborrag": {
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh"
    }
  }
}
```

### Claude Code

Register the server for the current project. Use `/mcp` inside Claude Code to
see whether it connected. [Claude Code MCP setup](https://code.claude.com/docs/en/mcp).

```bash
claude mcp add --transport stdio harborrag -- \
  "/path/to/HarborRAG/scripts/deployment/mcp.sh"
```

### Cursor

Add the entry to `~/.cursor/mcp.json` or to the project's `.cursor/mcp.json`.
[Cursor MCP setup](https://cursor.com/docs/mcp).

```json
{
  "mcpServers": {
    "harborrag": {
      "type": "stdio",
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh"
    }
  }
}
```

### VS Code

For a new Copilot session in VS Code, add this entry to
`~/.copilot/mcp-config.json`. If you set `COPILOT_HOME`, use the `mcp-config.json`
there. Run **Chat: Open Customizations** and select **MCP Servers** to find the
server in the Copilot session.
[VS Code MCP configuration](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#configure-mcp-for-copilot).

```json
{
  "mcpServers": {
    "harborrag": {
      "type": "stdio",
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh",
      "tools": ["*"]
    }
  }
}
```

<details markdown="1">
<summary>Existing editor-managed MCP configuration</summary>

To maintain an editor-managed server, run **MCP: Open User Configuration** or
edit the workspace's `.vscode/mcp.json`. Add this entry under `servers` and use
**MCP: List Servers** to inspect it.
[Editor-managed configuration](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#editor-managed-configuration-for-local-harness-compatibility).

```json
{
  "servers": {
    "harborrag": {
      "type": "stdio",
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh"
    }
  }
}
```

</details>

### Codex

Run this in the terminal for a local Codex setup, then start Codex and use `/mcp`
to see the server. The command saves the entry in your Codex configuration.
[Codex MCP setup](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

```bash
codex mcp add harborrag -- "/path/to/HarborRAG/scripts/deployment/mcp.sh"
```

### Zed

Open **Settings → AI → MCP Servers** and select **Add Server → Add Local
Server**. Enter the wrapper command there or add the `context_servers` entry
in the settings file. [Zed MCP setup](https://zed.dev/docs/ai/mcp).

```json
{
  "context_servers": {
    "harborrag": {
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh",
      "args": []
    }
  }
}
```

### Gemini CLI

Add the entry to `~/.gemini/settings.json` or to the project's
`.gemini/settings.json`. Use `/mcp` to check the connection.
[Gemini CLI MCP setup](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md).

```json
{
  "mcpServers": {
    "harborrag": {
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh",
      "args": []
    }
  }
}
```

## Connect an HTTP client

From the checkout root, run `scripts/deployment/mcp.sh --http`. In a client that
supports **Streamable HTTP**, enter `http://127.0.0.1:8010/mcp` and set the
`Authorization` header to `Bearer <token>`. Use the port and path from your
server configuration if they differ. The browser status page is at `/`, and
`/healthz` checks the process independently of your MCP credentials.

For your own local server in its default auth mode, use the value of
`HARBORRAG_MCP_BEARER_TOKEN` in `env/.env.mcp`. For other readers, use an issued
[tenant-bound reader key](server-operations.md#tenant-bound-reader-keys-and-shared-corpus-access)
with the server in `api_key` mode. In your client, use a secret prompt or an
environment-backed header. Keep literal tokens out of project files, shell
history, chats, and screenshots.

Loopback means the address is local to the **client process**. A cloud agent
or an IDE running on another host cannot reach your laptop at `127.0.0.1`.
See [server security boundaries](server-operations.md#security-boundaries)
for operator guidance.

### Cursor over HTTP

Make `HARBORRAG_MCP_TOKEN` available in the environment that launches Cursor.
Merge this entry into `mcp.json`, or replace your `harborrag` stdio entry to
switch transports. [Cursor MCP settings](https://cursor.com/docs/mcp).

```json
{
  "mcpServers": {
    "harborrag": {
      "url": "http://127.0.0.1:8010/mcp",
      "headers": {
        "Authorization": "Bearer ${env:HARBORRAG_MCP_TOKEN}"
      }
    }
  }
}
```

### VS Code over HTTP

Add this entry to your Copilot `mcp-config.json` and provide
`HARBORRAG_MCP_TOKEN` in the environment where the Copilot session runs.
[VS Code MCP environment variables](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#environment-variable-references).

```json
{
  "mcpServers": {
    "harborrag": {
      "type": "http",
      "url": "http://127.0.0.1:8010/mcp",
      "headers": {
        "Authorization": "Bearer ${HARBORRAG_MCP_TOKEN}"
      },
      "tools": ["*"]
    }
  }
}
```

### Codex over HTTP

Use the URL and set the name of the environment variable that holds the token.
The example uses a hidden Bash prompt, so the token stays out of the command
history and Codex configuration. Start Codex from the same shell and use
`/mcp` to check the connection.
[Codex Streamable HTTP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

```bash
read -r -s -p 'HarborRAG MCP token: ' HARBORRAG_MCP_TOKEN
printf '\n'
export HARBORRAG_MCP_TOKEN
codex mcp add harborrag-http --url http://127.0.0.1:8010/mcp \
  --bearer-token-env-var HARBORRAG_MCP_TOKEN
```

## Verify the connection

1. Open the client's MCP tool list and find `list_sources` on the `harborrag`
   server (or the name you registered).
2. Ask the assistant to run HarborRAG's `list_sources` for tenant `DEFAULT`.
   Substitute the tenant that holds your source if you use another one. The tool returns a
   `sources` array. Look for the source you ingested.
3. Ask a question about that source and have the assistant call `vector_search`
   followed by `fetch_evidence` for citations.

### Troubleshoot a connection

| What you see | What to check |
| --- | --- |
| Stdio server missing or exits | From the checkout root, run `scripts/deployment/mcp.sh --check`. If it lists tools, check the client log for an incorrect command path or lack of Docker access. Restart the client after changing its configuration. |
| HTTP connection refused | Confirm the wrapper started and `http://127.0.0.1:8010/healthz` responds on the client machine. See [startup diagnostics](server-operations.md#checks-before-the-http-server-starts). |
| HTTP rejects authentication | Check that the client sends `Authorization: Bearer <token>` to `/mcp` and that the credential matches the server's auth mode. |
| The tool connects but `sources` is empty | Check that the tenant matches your credential and that the source is published and readable under the [corpus access policy](server-operations.md#shared-corpus-access). |

See the [MCP tool reference](README.md) for arguments, defaults, and the full
search-to-evidence workflow.
