"""Minimal public gateway for Vobiz callbacks during local testing.

Only explicitly allowed Vobiz HTTP routes and the media WebSocket are proxied.
Dashboard, campaign, lead, configuration and static-file routes are not exposed.
"""
from __future__ import annotations

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

UPSTREAM_HTTP = "http://127.0.0.1:9090"
UPSTREAM_WS = "ws://127.0.0.1:9090"
ALLOWED = {
    "/vobiz/answer",
    "/vobiz/incoming",
    "/vobiz/hangup",
    "/vobiz/trunk-webhook",
    "/vobiz/recording-webhook",
    "/vobiz_recording",
}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health() -> dict:
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.get(f"{UPSTREAM_HTTP}/health")
    return {"status": "ok" if response.is_success else "upstream_error"}


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy_http(path: str, request: Request) -> Response:
    route = "/" + path
    if route not in ALLOWED:
        raise HTTPException(status_code=404, detail="Not found")
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}}
    headers["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", "https")
    headers["x-forwarded-host"] = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        upstream = await client.request(request.method, f"{UPSTREAM_HTTP}{route}", params=request.query_params, content=body, headers=headers)
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() in {"content-type", "location"}}
    return Response(upstream.content, status_code=upstream.status_code, headers=response_headers)


@app.websocket("/ws/vobiz")
async def proxy_vobiz_ws(client: WebSocket) -> None:
    await client.accept()
    query = client.url.query
    target = f"{UPSTREAM_WS}/ws/vobiz" + (f"?{query}" if query else "")
    try:
        async with websockets.connect(target, max_size=None, ping_interval=20, ping_timeout=20) as upstream:
            async def client_to_upstream() -> None:
                while True:
                    message = await client.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await client.send_bytes(message)
                    else:
                        await client.send_text(message)

            import asyncio
            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    finally:
        try:
            await client.close()
        except Exception:
            pass
