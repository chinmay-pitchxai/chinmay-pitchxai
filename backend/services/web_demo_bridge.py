"""Web Demo Bridge: Directly connect browser audio to Gemini Live.
Allows testing the prompted AI through the web dashboard.
"""

import asyncio
import base64
import json
from typing import Any, Callable, Optional
from fastapi import WebSocket
from loguru import logger
import websockets as ws_client

from config import settings
from core.gemini_auth import gemini_auth_headers, get_gemini_api_key
from core.state import get_state
from services.vobiz_bridge.gemini_protocol import gemini_live_ws_url
from services.sandbox_manager import get_agent
from services.conversation_log import (
    append_session_meta,
    append_turn,
    new_session_id,
)

async def handle_web_voice_demo(
    ws: WebSocket,
    role: str = "sales_1",
    agent_id: Optional[str] = None,
) -> None:
    """Bridge Browser <-> Gemini Live.
    Resolves prompt/voice based on role or agent_id.
    """
    await ws.accept()
    
    # 1. Resolve Config
    system_prompt = ""
    voice = "Puck"
    
    if role == "factory" and agent_id:
        agent = get_agent(agent_id)
        if agent:
            system_prompt = agent.get("prompt", "")
            voice = agent.get("voice", "Puck")
        else:
            logger.error(f"Test Bridge: Agent {agent_id} not found")
            await ws.close(code=4004)
            return
    else:
        state = get_state(role)
        system_prompt = state.get("prompt", "")
        # Fallback to default prompt if empty
        if not system_prompt:
            from prompts.role_prompts import get_role_prompt_text
            system_prompt = get_role_prompt_text("sales_1")
            
        # Add static RAG if available
        rag = state.get("rag", "")
        if rag:
            system_prompt += f"\n[Knowledge Base]\n{rag}\n"
            
        # Voice configured via GEMINI_LIVE_VOICE environment setting.
        voice = settings.gemini_live_voice

    api_key = get_gemini_api_key()
    model = settings.gemini_live_model

    session_id = new_session_id("web-demo")
    
    # Log dir mapping
    from core.state import _get_role_path
    log_dir = str(_get_role_path(role, "logs"))

    append_session_meta(
        session_id,
        "web-demo",
        path="web_voice",
        model=model,
        base_dir=log_dir
    )

    gemini_url = gemini_live_ws_url()

    try:
        async with ws_client.connect(
            gemini_url,
            extra_headers=gemini_auth_headers(api_key),
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=60,
            close_timeout=5,
        ) as gws:
            from services.vobiz_bridge.gemini_protocol import build_live_setup
            setup_msg = build_live_setup(
                model=model,
                system_instruction=system_prompt,
                voice=voice,
            )
            await gws.send(json.dumps(setup_msg))
            
            # Wait for setup complete
            try:
                resp = await asyncio.wait_for(gws.recv(), timeout=10.0)
                logger.info("Gemini Live Web Demo: Setup complete")
            except Exception as e:
                logger.error(f"Gemini Live Web Demo: Setup timeout/error: {e}")
                await ws.close()
                return

            async def gemini_to_browser():
                try:
                    async for msg in gws:
                        data = json.loads(msg)
                        if "serverContent" in data:
                            content = data["serverContent"]
                            if "modelTurn" in content:
                                parts = content["modelTurn"]["parts"]
                                for p in parts:
                                    if "inlineData" in p:
                                        audio_b64 = p["inlineData"]["data"]
                                        await ws.send_json({"type": "audio", "data": audio_b64})
                            
                            if content.get("turnComplete"):
                                await ws.send_json({"type": "status", "event": "turn_complete"})
                except Exception as e:
                    logger.debug(f"Gemini->Browser closed: {e}")

            async def browser_to_gemini():
                try:
                    while True:
                        msg = await ws.receive_json()
                        if msg["type"] == "audio":
                            gemini_msg = {
                                "realtimeInput": {
                                    "mediaChunks": [
                                        {"data": msg["data"], "mimeType": "audio/pcm"}
                                    ]
                                }
                            }
                            await gws.send(json.dumps(gemini_msg))
                except Exception as e:
                    logger.debug(f"Browser->Gemini closed: {e}")

            await asyncio.gather(gemini_to_browser(), browser_to_gemini())
    except Exception as e:
        logger.error(f"Web Bridge Connection Error: {e}")
    finally:
        try: await ws.close()
        except: pass
