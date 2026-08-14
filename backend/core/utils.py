from pathlib import Path
from typing import Union

from core.phone_norm import norm_phone_str as _norm_phone_str


async def _prewarm_opening(call_id: str, text: str, voice: str):
    """No-op: Gemini Live handles the opening natively."""
    pass


def _build_opening_line(row_data: dict, role: str = "sales_1") -> str:
    from core.opening_line import build_opening_line

    return build_opening_line(row_data, role)


def range_file_response(
    file_path: Path,
    request,  # fastapi.Request — avoid circular import
    media_type: str,
):
    """
    Return a streaming audio/video response that honours the HTTP ``Range``
    header so browsers can seek/scrub recordings and display the player
    correctly.  Falls back to a full ``FileResponse`` (with inline disposition)
    when no ``Range`` header is present.
    """
    from fastapi import Response
    from fastapi.responses import FileResponse, StreamingResponse

    try:
        file_size = file_path.stat().st_size
    except OSError:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Recording file not found")

    range_header = (request.headers.get("range") or "").strip()

    if not range_header or not range_header.lower().startswith("bytes="):
        # No range requested — serve the full file inline so the browser can play it.
        return FileResponse(
            file_path,
            media_type=media_type,
            content_disposition_type="inline",
            headers={"Accept-Ranges": "bytes"},
        )

    try:
        range_val = range_header.split("=", 1)[1]
        start_str, end_str = (range_val.split("-", 1) + [""])[:2]

        start = int(start_str) if start_str.strip() else 0
        end = int(end_str) if end_str.strip() else file_size - 1

        if start >= file_size:
            return Response(
                status_code=416,
                content="Requested Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        end = min(end, file_size - 1)
        length = end - start + 1

        def _iter_file():
            chunk_size = 64 * 1024  # 64 KB
            bytes_left = length
            with open(file_path, "rb") as fh:
                fh.seek(start)
                while bytes_left > 0:
                    to_read = min(chunk_size, bytes_left)
                    data = fh.read(to_read)
                    if not data:
                        break
                    yield data
                    bytes_left -= len(data)

        return StreamingResponse(
            _iter_file(),
            status_code=206,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
                "Content-Disposition": "inline",
            },
        )
    except Exception:
        # Fallback: serve the whole file inline.
        return FileResponse(
            file_path,
            media_type=media_type,
            content_disposition_type="inline",
            headers={"Accept-Ranges": "bytes"},
        )
