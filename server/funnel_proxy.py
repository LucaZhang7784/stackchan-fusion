"""Tailscale Funnel path router: 443 -> /xiaozhi/ota/* (8003) | /* (8000 WS)"""
import asyncio
import logging
from aiohttp import web, ClientSession, WSMsgType, ClientConnectorError, ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("proxy")

OTA_PORT = 8003
WS_PORT = 8000
PROXY_PORT = 8090  # Funnel 443 -> localhost:8090


async def proxy_ws_to_backend(request):
    """Proxy WebSocket connections through to port 8000"""
    target = f"http://127.0.0.1:{WS_PORT}{request.path_qs}"

    # Accept the incoming WS connection from Funnel
    ws_in = web.WebSocketResponse(
        max_msg_size=1024 * 1024,  # 1MB
        heartbeat=30.0,
    )
    await ws_in.prepare(request)

    try:
        async with ClientSession() as session:
            # Strip hop-by-hop headers
            hdrs = {}
            for k, v in request.headers.items():
                kl = k.lower()
                if kl not in ("host", "upgrade", "connection",
                              "sec-websocket-key", "sec-websocket-version",
                              "sec-websocket-extensions", "sec-websocket-protocol"):
                    hdrs[k] = v

            async with session.ws_connect(
                target, headers=hdrs, max_msg_size=1024 * 1024
            ) as ws_out:
                # Bidirectional relay
                async def relay(src, dst, name):
                    try:
                        async for msg in src:
                            if msg.type == WSMsgType.TEXT:
                                await dst.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await dst.send_bytes(msg.data)
                            elif msg.type == WSMsgType.CLOSE:
                                await dst.close(code=msg.data, message=msg.extra)
                                break
                            elif msg.type == WSMsgType.ERROR:
                                log.warning(f"{name}: WS error: {msg.data}")
                                break
                    except (ConnectionResetError, ConnectionError,
                            asyncio.CancelledError, RuntimeError) as e:
                        log.debug(f"{name}: relay ended: {e}")

                await asyncio.gather(
                    relay(ws_in, ws_out, "client->backend"),
                    relay(ws_out, ws_in, "backend->client"),
                    return_exceptions=True,
                )
    except (ClientConnectorError, ClientError, asyncio.TimeoutError) as e:
        log.error(f"WS backend connection failed: {e}")

    return ws_in


async def proxy_http_to_backend(request):
    """Proxy HTTP requests through to port 8003"""
    target = f"http://127.0.0.1:{OTA_PORT}{request.path_qs}"
    hdrs = {k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "transfer-encoding")}

    try:
        async with ClientSession() as session:
            body = await request.read()
            async with session.request(
                request.method, target, headers=hdrs, data=body
            ) as resp:
                resp_body = await resp.read()
                resp_hdrs = {k: v for k, v in resp.headers.items()
                             if k.lower() != "transfer-encoding"}
                return web.Response(body=resp_body, status=resp.status, headers=resp_hdrs)
    except (ClientConnectorError, ClientError) as e:
        log.error(f"HTTP proxy error to {target}: {e}")
        return web.Response(status=502, text="Backend unreachable")


def create_app():
    # OTA route: /xiaozhi/ota/* -> port 8003
    # All other (including /xiaozhi/v1/) -> WS proxy to port 8000
    app = web.Application()
    app.router.add_route("*", "/xiaozhi/ota/{tail:.*}", proxy_http_to_backend)
    app.router.add_route("*", "/{tail:.*}", proxy_ws_to_backend)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="127.0.0.1", port=PROXY_PORT)
