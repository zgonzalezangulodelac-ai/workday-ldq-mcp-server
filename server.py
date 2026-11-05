"""
Workday Live Data Query (LDQ) — MCP Server
===========================================

Exposes Workday Live Data Query as an MCP server so Claude (or any MCP client)
can discover tables and run read-only SQL against a Workday tenant, the same
pattern shown in the "Claude <-> Workday" demo: list_tables / describe_table /
execute_query tools backed by real LDQ queries.

Built directly against the workday_ldq Python client (create_connection ->
a standard PEP 249 / trino.dbapi connection), using JWT Bearer (ISU)
authentication, since this runs unattended on a server with no browser
available for the interactive OAuth flow.

Configuration is via environment variables (see .env.example).

Run locally (stdio, for testing with the MCP Inspector):
    python server.py --transport stdio

Run as a remote server (what a Claude custom connector needs):
    python server.py --transport streamable-http --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Any

from workday_ldq import create_connection

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ldq-mcp")

# ---------------------------------------------------------------------------
# Configuration (environment variables)
# ---------------------------------------------------------------------------
# Required for every deployment:
#   WD_HOST                 e.g. wd3-impl-services1.workday.com
#   WD_TOKEN_ENDPOINT       e.g. https://{host}/ccx/oauth2/{tenant}/token
#   WD_CLIENT_ID            Client ID from the Register API Client task
#   WD_ISU                  Integration System User name
#   WD_PRIVATE_KEY_FILE     Path to the private key .pem (mounted as a secret)
#     -- or, if you'd rather pass the key content directly (e.g. as a
#        platform secret env var instead of a mounted file) --
#   WD_PRIVATE_KEY          Full PEM content of the private key
#
# Optional (defaults shown):
#   WD_PORT=443
#   WD_CATALOG=workday_core
#   WD_SCHEMA=public
#   MCP_SHARED_SECRET        If set, every request must send
#                            `Authorization: Bearer <value>` — protects this
#                            server's own HTTP endpoint (separate from the
#                            Workday-side ISU auth above). Strongly
#                            recommended for anything reachable on the public
#                            internet, since Claude's custom connector dialog
#                            lets you supply exactly this kind of bearer token.
#   MAX_ROWS=500             Hard cap on rows returned per query, to avoid
#                            flooding the model's context with a huge table.

REQUIRED_VARS = ["WD_HOST", "WD_TOKEN_ENDPOINT", "WD_CLIENT_ID", "WD_ISU"]


def _build_ldq_properties() -> dict[str, str]:
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "See .env.example for the full list."
        )
    if not os.environ.get("WD_PRIVATE_KEY_FILE") and not os.environ.get("WD_PRIVATE_KEY"):
        raise RuntimeError(
            "Set either WD_PRIVATE_KEY_FILE (path to the .pem file) or "
            "WD_PRIVATE_KEY (the PEM content itself)."
        )

    props = {
        "wd.authn.authModel": "JWT_BEARER",
        "wd.authn.accessTokenEndpoint": os.environ["WD_TOKEN_ENDPOINT"],
        "wd.authn.clientId": os.environ["WD_CLIENT_ID"],
        "wd.authn.isu": os.environ["WD_ISU"],
        "wd.host": os.environ["WD_HOST"],
        "wd.port": os.environ.get("WD_PORT", "443"),
        "wd.catalog": os.environ.get("WD_CATALOG", "workday_core"),
        "wd.schema": os.environ.get("WD_SCHEMA", "public"),
    }
    if os.environ.get("WD_PRIVATE_KEY_FILE"):
        props["wd.authn.privateKeyFile"] = os.environ["WD_PRIVATE_KEY_FILE"]
    else:
        props["wd.authn.privateKey"] = _normalize_private_key(os.environ["WD_PRIVATE_KEY"])
    return props


def _normalize_private_key(raw: str) -> str:
    """Make WD_PRIVATE_KEY robust to how it was pasted.

    Web dashboards (Render, Railway, etc.) sometimes store a pasted
    multi-line value with literal two-character "\\n" sequences instead of
    real newline bytes, depending on how the value was copied in. A key in
    that form fails to parse with a confusing "Could not parse the provided
    public key" error that looks identical to a genuinely corrupt key. If we
    don't see any real newlines but do see literal "\\n", convert them.
    """
    if "\n" not in raw and "\\n" in raw:
        raw = raw.replace("\\n", "\n")
    return raw.strip()


def _get_connection():
    """Create a fresh LDQ connection for one query.

    trino.dbapi.connect() is lightweight — it doesn't open a socket until a
    query actually runs — so creating one per tool call is simple and avoids
    any issue with a long-lived connection outliving a short-lived JWT.
    """
    props = _build_ldq_properties()
    return create_connection(props)


MAX_ROWS = int(os.environ.get("MAX_ROWS", "500"))

# Only allow read-only statements. LDQ itself is a query engine (no writes),
# but this is a defense-in-depth guard against anyone trying DDL/DML through
# this tool, and against accidental mistakes from the calling model.
_ALLOWED_STATEMENT_PREFIX = re.compile(r"^\s*(SELECT|SHOW|DESCRIBE|EXPLAIN)\b", re.IGNORECASE)


def _rows_to_table(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    if not rows:
        return "(no rows returned)"
    header = " | ".join(columns)
    sep = " | ".join("---" for _ in columns)
    body = "\n".join(" | ".join(str(v) for v in row) for row in rows)
    return f"{header}\n{sep}\n{body}"


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------

mcp = MCPServer(
    name="workday-ldq",
    title="Workday Live Data Query",
    description=(
        "Query a Workday tenant's live Workforce/Talent data via Live Data Query "
        "(real-time SQL over Trino). Tools: list_tables, describe_table, execute_query."
    ),
)


@mcp.tool()
def list_tables() -> str:
    """List the tables available in the configured Workday Data Cloud catalog/schema."""
    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SHOW TABLES")
        rows = cur.fetchall()
    finally:
        conn.close()
    tables = [r[0] for r in rows]
    return "\n".join(tables) if tables else "(no tables visible — check ISU domain permissions)"


@mcp.tool()
def describe_table(table_name: str) -> str:
    """Describe a table's columns and types.

    Args:
        table_name: The table to describe, e.g. "worker" or "position".
                    Do not include the catalog/schema prefix — that's
                    applied automatically from server configuration.
    """
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        return f"Invalid table name: {table_name!r}"
    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"DESCRIBE {table_name}")
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    finally:
        conn.close()
    return _rows_to_table(columns, rows)


@mcp.tool()
def execute_query(sql: str) -> str:
    """Run a read-only SQL query against Workday Live Data Query.

    Only SELECT, SHOW, DESCRIBE, and EXPLAIN statements are allowed. Results
    are capped to avoid flooding the conversation with huge tables — add your
    own LIMIT clause for large tables.

    Args:
        sql: Standard SQL (Trino dialect) to execute, e.g.
             "SELECT COUNT(*) FROM worker WHERE active = TRUE".
    """
    if not _ALLOWED_STATEMENT_PREFIX.match(sql):
        return (
            "Rejected: only SELECT, SHOW, DESCRIBE, and EXPLAIN statements are "
            "permitted through this tool."
        )
    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchmany(MAX_ROWS)
        columns = [d[0] for d in cur.description] if cur.description else []
    finally:
        conn.close()
    table = _rows_to_table(columns, rows)
    if len(rows) == MAX_ROWS:
        table += f"\n\n(truncated at {MAX_ROWS} rows — narrow your query or add LIMIT)"
    return table


# ---------------------------------------------------------------------------
# Optional shared-secret auth for this server's own HTTP endpoint
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Rejects any request that doesn't present the configured bearer token.

    This is separate from, and in addition to, the Workday-side ISU/JWT auth
    used inside the tools above — this protects the MCP endpoint itself from
    being called by anyone who finds the URL.
    """

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {self.token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def debug_connection(request: Request) -> Response:
    """Diagnostic endpoint: attempts a real LDQ connection and returns either
    the live table list or the FULL exception traceback as plain text.

    This exists specifically so the real error is visible without needing
    shell/log access (e.g. on a free-tier host where a shell isn't available).
    Protected by the same bearer secret as the MCP endpoint itself -- do not
    leave this reachable without MCP_SHARED_SECRET set.
    """
    import traceback as tb_module

    secret = os.environ.get("MCP_SHARED_SECRET")
    if secret:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {secret}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("SHOW TABLES")
        rows = cur.fetchall()
        conn.close()
        return Response(
            f"SUCCESS\n\nFirst 10 tables:\n" + "\n".join(r[0] for r in rows[:10]),
            media_type="text/plain",
        )
    except Exception:
        return Response(
            "FAILED\n\n" + tb_module.format_exc(),
            media_type="text/plain",
            status_code=500,
        )


def build_app():
    # The MCP SDK validates the incoming Host header (DNS rebinding protection)
    # against an explicit allowlist -- by default that list is empty, which
    # rejects every real deployment host with "421 Misdirected Request". Set
    # ALLOWED_HOSTS to the host(s) this server will actually be reached at,
    # e.g. "workday-ldq-mcp-server.onrender.com" (no scheme, no port needed
    # for the default 443/80). Comma-separate multiple hosts if needed.
    allowed_hosts_env = os.environ.get("ALLOWED_HOSTS", "")
    allowed_hosts = [h.strip() for h in allowed_hosts_env.split(",") if h.strip()]
    # Always allow localhost too, so local testing keeps working.
    for local_host in ("127.0.0.1", "localhost"):
        if local_host not in allowed_hosts:
            allowed_hosts.append(local_host)

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=["*"],
    )
    if not allowed_hosts_env:
        log.warning(
            "ALLOWED_HOSTS is not set -- only localhost will be accepted. "
            "Set ALLOWED_HOSTS to your deployed hostname (e.g. "
            "'workday-ldq-mcp-server.onrender.com') or every real request will "
            "get '421 Misdirected Request'."
        )

    app = mcp.streamable_http_app(transport_security=transport_security)

    from starlette.routing import Route
    app.router.routes.append(Route("/debug", debug_connection, methods=["GET"]))

    shared_secret = os.environ.get("MCP_SHARED_SECRET")
    if shared_secret:
        app = Starlette(
            routes=app.routes,
            middleware=[Middleware(BearerAuthMiddleware, token=shared_secret)],
            lifespan=app.router.lifespan_context,
        )
        log.info("MCP_SHARED_SECRET is set — this endpoint requires a bearer token.")
    else:
        log.warning(
            "MCP_SHARED_SECRET is NOT set — this endpoint will accept unauthenticated "
            "requests from anyone who has the URL. Set MCP_SHARED_SECRET before "
            "deploying anywhere reachable from the public internet."
        )
    return app


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Workday LDQ MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    if args.transport == "stdio":
        # Local testing only (e.g. with the MCP Inspector). Claude's custom
        # connectors need a remote HTTPS URL, so use --transport streamable-http
        # for an actual deployment.
        mcp.run(transport="stdio")
    else:
        import uvicorn

        app = build_app()
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
