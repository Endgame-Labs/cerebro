# Deploy

Run a workspace on a server, so your agents can reach it from anywhere with a token. Cerebro is one process, `cerebro serve`, which you start like any web service and leave running.

## On a server

**1. Install** from a checkout of this repository:

```sh
uv tool install .
```

**2. Make the workspace,** or copy yours across and trust it once:

```sh
cerebro init ~/wiki --daily-usd 20 --timezone UTC
cerebro trust ~/wiki     # only for a workspace you copied or cloned
```

**3. Keep the keys.** Off a terminal, `secrets set` reads the value from standard input:

```sh
cerebro secrets set fireworks --workspace ~/wiki < fireworks.key
cerebro secrets set typesafe --workspace ~/wiki < typesafe.key
```

**4. Name the address it's reached by,** in the workspace's `settings.yaml`. Add `127.0.0.1` to check it from the server itself:

```yaml
serve:
  allowed_hosts: [wiki.example.com, 127.0.0.1]
```

**5. Make a token** for each client. It's shown once, so put it straight into the client's config:

```sh
cerebro tokens new desk-agent --workspace ~/wiki
```

**6. Start it:**

```text
$ cerebro serve --workspace ~/wiki
workspace .../wiki
serving   MCP on localhost, a pass every 30 seconds. Ctrl-C stops.
mcp       http://127.0.0.1:4747/mcp
api       http://127.0.0.1:4747/api/v1, the JSON API
health    http://127.0.0.1:4747/healthz, which answers with no token
tokens    every request needs a token: cerebro tokens list shows them
hosts     a request must name one of these in its Host: wiki.example.com, 127.0.0.1
```

Use whatever keeps your other services running to start it at boot and restart it if it stops. It logs to standard output.

## Put HTTPS in front

Cerebro doesn't handle HTTPS itself. Keep it on `127.0.0.1` and put a proxy in front that does, such as Caddy. This is `deploy/caddy/Caddyfile`:

```text
wiki.example.com {
	reverse_proxy 127.0.0.1:4747
}
```

Caddy gets a certificate for that name by itself, once its DNS points at the server and ports 80 and 443 are open. Open no other port.

Then connect a client to the public address:

```sh
claude mcp add --scope user --transport http cerebro-wiki https://wiki.example.com/mcp --header "Authorization: Bearer <token>"
```

## What the server checks

| Request | Answer |
|---|---|
| `/healthz`, from anyone | `200 ok`, for a load balancer to probe |
| No token, or a revoked one | `401` |
| A name not in `allowed_hosts` | `421` |
| A browser page from an origin you didn't list with `--allowed-origin` | `403` |
| More than 100 at once | `503` until some finish |

A request to a name you didn't list is refused with a line that says what to add:

```json
{"error": {"code": "host_refused", "message": "this server is not reached by the Host 'other.example.com': list each name it is reached by with serve --allowed-host <name>"}}
```

To serve on an address other than loopback, as in a container, make a token first. Without one, Cerebro refuses to start:

```text
Error: serve --host 0.0.0.0 is not loopback, and this workspace has no token: make one with cerebro tokens new <name> first, so a caller must prove who it is
```

`cerebro tokens revoke <name>` ends a token at once, with no restart.

## In a container

`Dockerfile` builds an image that runs `cerebro serve`, and `deploy/compose.yaml` runs one workspace from it. Run these from `deploy/`:

```sh
cp cerebro.env.example cerebro.env       # then fill in the keys
docker compose build
docker compose run --rm -T workspace init /workspace --daily-usd 20 --timezone UTC
docker compose run --rm -T --entrypoint sh workspace -c 'printf "serve:\n  allowed_hosts: [wiki.example.com, 127.0.0.1]\n" >> /workspace/settings.yaml'
docker compose run --rm -T workspace tokens new desk-agent
docker compose up -d
```

The compose file publishes port 4747 on `127.0.0.1` only, so put the proxy above on the same host. `docker compose ps` shows the container as healthy once `/healthz` answers.

## What to back up

Two folders hold everything. Copy them with the server stopped:

| Folder | Holds | In the container |
|---|---|---|
| The workspace | Your files, and `.cerebro/` with articles, facts, logs and tokens | `/workspace` |
| `~/.config/cerebro` | The secrets file and the list of trusted workspaces | `/config` |

Restore both, and the workspace comes back whole, tokens included. Your agent files and sources are in git too, but the store and secrets are not.

## Good to know

- **Stopping is clean.** Ctrl-C or SIGTERM stops new requests, cancels the pass under way, gives open requests 5 seconds, and exits within 10 seconds. Give your supervisor a stop timeout of at least 10 seconds.
- **One server per workspace.** Run a single `cerebro serve` on each workspace.
- **No per-token rate limits.** A token may send as many requests as the server takes.
