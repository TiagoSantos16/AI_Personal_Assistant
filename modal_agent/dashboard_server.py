"""Streamlit server with a small HttpOnly remembered-device endpoint."""
import os
from pathlib import Path
from urllib.parse import urlsplit
import streamlit as st
from starlette.responses import JSONResponse
from starlette.routing import Route
from core.auth import COOKIE, MAX_AGE, valid


async def device(request):
    origin = urlsplit(request.headers.get("origin", ""))
    if origin.scheme != "https" or origin.netloc != request.headers.get("host"):
        return JSONResponse({"ok": False}, status_code=403)
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    token = data.get("token") if isinstance(data, dict) else None
    response = JSONResponse({"ok": True})
    if token == "":
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    elif valid(token, os.environ.get("DASHBOARD_PASSWORD")):
        response.set_cookie(COOKIE, token, max_age=MAX_AGE, path="/", secure=True,
                            httponly=True, samesite="strict")
    else:
        return JSONResponse({"ok": False}, status_code=403)
    return response


app = st.App(Path(__file__).with_name("dashboard.py"), routes=[Route("/api/device", device, methods=["POST"])])
