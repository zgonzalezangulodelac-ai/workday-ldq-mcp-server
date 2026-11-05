# Workday LDQ MCP Server

An MCP server that exposes Workday Live Data Query (LDQ) as three tools —
`list_tables`, `describe_table`, `execute_query` — so Claude (or any
MCP-compatible client) can discover and query live Workday data during a
conversation. This is the same pattern shown in the "Claude ↔ Workday"
demo video: a custom MCP server backed by LDQ, registered as a custom
connector in Claude's settings.

Built directly against the real `workday_ldq` Python client — the same one
used for DBeaver/Tableau/Python setups — so the authentication and query
behavior match exactly.

## How it works

```
Claude  --(MCP, streamable-http)-->  this server  --(JDBC/SQL via trino)-->  Workday LDQ
```

- **Claude side**: talks standard MCP over HTTPS. Auth is a simple bearer
  token you set yourself (`MCP_SHARED_SECRET`) — this just protects *this*
  server's endpoint from random internet traffic.
- **Workday side**: this server authenticates to LDQ using **ISU + JWT
  Bearer Grant** — the same method used for unattended/server-to-server
  connections (not User-as-Self, which needs an interactive browser login
  and can't run headless).

## Step 1 — Set up the Workday side (if you haven't already)

You need an Integration System User (ISU) and an x509 certificate
registered as an API Client with **Jwt Bearer Grant**. This is the exact
same setup used for the Power BI / DBeaver / Python LDQ connections —
if you've already done that for this tenant, you can reuse the same ISU
and keys here. If not:

1. Generate a key pair (see your team's cert-generation notes — `openssl`,
   or the PowerShell/Python fallbacks if `openssl` isn't available on your
   machine).
2. In Workday: `Create Integration System User` → `Create Security Group`
   (Integration System Security Group, Unconstrained) → grant the ISU's
   group **View Only** on the domains covering the tables you want exposed.
3. `Register API Client`: Client Grant Type = **Jwt Bearer Grant**, attach
   the x509 public cert, add the ISU, check "Include Workday Owned Scope".
4. Save the **Token Endpoint**, **Client ID**, and keep the private key file
   safe — you'll need all three for this server's config.

## Step 2 — Configure this server

```bash
cp .env.example .env
```

Fill in `.env` with the values from Step 1, plus a random value for
`MCP_SHARED_SECRET`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Step 3 — Test it locally

Install dependencies:

```bash
pip install -r requirements.txt
```

Quick local smoke test over stdio (no network needed):

```bash
python server.py --transport stdio
```

Or test the real HTTP transport (what Claude will actually use) and drive
it with the official MCP Inspector:

```bash
python server.py --transport streamable-http --port 8000
# in another terminal:
npx @modelcontextprotocol/inspector
# point the Inspector at http://localhost:8000/mcp with your bearer token
```

Try calling `list_tables`, then `describe_table` on `worker`, then
`execute_query` with something like:

```sql
SELECT COUNT(*) FROM worker WHERE active = TRUE
```

## Step 4 — Deploy it somewhere Claude can reach

Claude's custom connectors run from **Anthropic's cloud**, not your laptop —
the server needs a public HTTPS URL. Pick whichever hosting option fits
your team's existing setup; this repo's `Dockerfile` works as-is on any of
these:

- **Render / Railway / Fly.io** — point them at this repo, they build the
  `Dockerfile` and give you an HTTPS URL automatically. Simplest option if
  you don't already have cloud infrastructure.
- **A cloud VM you already have** (AWS/Azure/GCP) — run the Docker image
  behind a reverse proxy (nginx/Caddy) for TLS, or behind your existing load
  balancer.
- **Cloud Run / Azure Container Apps / ECS Fargate** — if your org already
  deploys containers this way, this image drops in directly.

Whichever you choose, set the same environment variables from your `.env`
file as platform secrets/env vars — **never commit `.env` or the private key
to source control.**

## Step 5 — Register it in Claude

This is the exact screen you saw in the demo video:

1. **Individual (Pro/Max)**: Customize → Connectors → **+** → Add custom
   connector.
   **Team/Enterprise**: an Owner goes to Organization Settings → Connectors
   → Add → Custom → Web; members then connect individually from
   Customize → Connectors.
2. Paste your deployed URL, e.g. `https://your-server.example.com/mcp`.
3. Under Advanced settings, set the Authorization header to
   `Bearer <your MCP_SHARED_SECRET value>`.
4. Click **Add**, then **Connect**.
5. Start a new chat and ask something like *"how many active workers do we
   have, broken down by region?"* — Claude should call `execute_query`
   automatically.

## Security notes before you deploy this anywhere real

- **This exposes live Workday data to whoever has the connector configured
  in Claude.** Scope the ISU's security group narrowly — only the domains
  you actually want queryable this way, same as any other LDQ client.
- **`execute_query` only allows SELECT/SHOW/DESCRIBE/EXPLAIN** — this is a
  defense-in-depth guard, not a substitute for proper ISU domain scoping.
- **Rotate `MCP_SHARED_SECRET`** if it's ever exposed, the same as any API key.
- **Treat the private key file like any other production credential** — a
  mounted secret or secret-manager reference, not a file baked into the
  image or committed to git.

## Files

- `server.py` — the MCP server (3 tools: `list_tables`, `describe_table`, `execute_query`)
- `requirements.txt` — Python dependencies, including the bundled LDQ wheel
- `ldq_python_client-1.0.0-py3-none-any.whl` — the Workday LDQ Python client
- `.env.example` — configuration template
- `Dockerfile` — container build for any standard container host
