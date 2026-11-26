#!/usr/bin/env python3
"""
ClipForge - crea clips virales verticales con subtitulos a partir de un video de YouTube.

Flujo: link de YouTube -> Higgsfield (MCP) busca los momentos -> yt-dlp descarga solo
esos tramos -> ffmpeg los pasa a 9:16 -> whisper.cpp (modelo large) genera subtitulos
-> se queman estilo TikTok y se guarda .srt + resumen con titulos y descripciones.

Solo usa la libreria estandar de Python (tkinter incluido). Herramientas externas:
yt-dlp, ffmpeg/ffprobe, deno (para yt-dlp) y whisper-cli.exe (viene con Subtitle Edit).
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid

APP_NAME = "ClipForge"
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
W, H = 1080, 1920
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
# Subtitle Edit guarda whisper.cpp en SpeechToText\Cpp (versiones nuevas) o Whisper\Cpp (antiguas)
SE_WHISPER_DIRS = [os.path.join(APPDATA, "Subtitle Edit", "SpeechToText", "Cpp"),
                   os.path.join(APPDATA, "Subtitle Edit", "Whisper", "Cpp")]
SE_WHISPER_DIR = SE_WHISPER_DIRS[0]
WHISPER_EXES = ["whisper-cli.exe", "main.exe", "whisper-cli"]


def detect_whisper():
    """Busca whisper-cli y el mejor modelo instalados por Subtitle Edit. Devuelve (cli, modelo)."""
    cli = model = None
    for d in SE_WHISPER_DIRS:
        for exe in WHISPER_EXES:
            if os.path.isfile(os.path.join(d, exe)):
                cli = cli or os.path.join(d, exe)
        mdir = os.path.join(d, "Models")
        if os.path.isdir(mdir) and not model:
            bins = [f for f in os.listdir(mdir) if f.startswith("ggml-") and f.endswith(".bin")]
            pref = ["ggml-large-v3-turbo.bin", "ggml-large-v3.bin"]
            pick = next((b for b in pref if b in bins), None)
            if not pick and bins:
                pick = max(bins, key=lambda b: os.path.getsize(os.path.join(mdir, b)))
            if pick:
                model = os.path.join(mdir, pick)
    return cli, model

DEFAULTS = {
    "mcp_url": "https://mcp.higgsfield.ai/mcp",
    "mcp_token": "",
    "mcp_auth_header": "Authorization",
    "mcp_auth_prefix": "Bearer ",
    "analysis_mode": "glance",
    "whisper_cli": os.path.join(SE_WHISPER_DIR, "whisper-cli.exe"),
    "whisper_model": os.path.join(SE_WHISPER_DIR, "Models", "ggml-large-v3-turbo.bin"),
    "whisper_prompt": "GTA 5, Los Santos, Franklin, Michael, Trevor, sugarrv",
    "whisper_threads": 0,
    "cookies": os.path.join(APP_DIR, "cookies.txt"),
    "output_dir": os.path.join(APP_DIR, "salida"),
    "cam_mode": "auto",          # auto | none | manual
    "cam_manual": "0,0,0.28,0.35",
    "burn_subs": True,
    "font": "Arial Black",
    "font_size": 78,
    "sub_style": "tiktok",
    "last_duration": 30,
    "last_count": 3,
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # Si las rutas de whisper no existen, se detectan automaticamente
    if not os.path.isfile(cfg.get("whisper_cli", "")) or not os.path.isfile(cfg.get("whisper_model", "")):
        cli, model = detect_whisper()
        if cli and not os.path.isfile(cfg.get("whisper_cli", "")):
            cfg["whisper_cli"] = cli
        if model and not os.path.isfile(cfg.get("whisper_model", "")):
            cfg["whisper_model"] = model
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ======================================================================
#  Utilidades de tiempo y procesos
# ======================================================================

def to_seconds(t):
    t = str(t).strip().replace(",", ".")
    parts = [float(p) for p in t.split(":")]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


def hms(s, ms=True):
    s = max(0.0, float(s))
    base = f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:"
    return base + (f"{s % 60:06.3f}" if ms else f"{int(s % 60):02d}")


def safe_name(text, maxlen=40):
    text = re.sub(r"[^\w\- ]+", "", text, flags=re.UNICODE).strip().replace(" ", "_")
    return text[:maxlen] or "clip"


class Cancelled(Exception):
    pass


# ======================================================================
#  Cliente MCP minimo (Streamable HTTP, JSON-RPC 2.0)
# ======================================================================

class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, url, token="", auth_header="Authorization", auth_prefix="Bearer ", log=print):
        self.url = url.strip()
        self.token = token.strip()
        self.auth_header = auth_header.strip() or "Authorization"
        self.auth_prefix = auth_prefix
        self.session_id = None
        self.log = log
        self._id = 0

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"{APP_NAME}/1.0",
        }
        if self.token:
            h[self.auth_header] = f"{self.auth_prefix}{self.token}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload, expect_response=True):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = r.headers.get("Content-Type", "")
                body = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            if e.code in (401, 403):
                raise MCPError(f"Higgsfield rechazo la autenticacion (HTTP {e.code}). "
                               f"Pulsa 'Iniciar sesion Higgsfield' (la API key de la consola no sirve para el MCP).\n{detail}")
            raise MCPError(f"HTTP {e.code} del servidor MCP: {detail}")
        except urllib.error.URLError as e:
            raise MCPError(f"No se pudo conectar al servidor MCP ({self.url}): {e.reason}")
        if not expect_response:
            return None
        msg = self._parse_body(body, ctype, payload.get("id"))
        if "error" in msg:
            raise MCPError(f"Error MCP: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    @staticmethod
    def _parse_body(body, ctype, want_id):
        if "text/event-stream" in ctype or body.lstrip().startswith(("event:", "data:")):
            msgs = []
            for block in re.split(r"\r?\n\r?\n", body):
                data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
                if data.strip():
                    try:
                        msgs.append(json.loads(data))
                    except json.JSONDecodeError:
                        pass
            for m in msgs:
                if m.get("id") == want_id:
                    return m
            if msgs:
                return msgs[-1]
            raise MCPError("Respuesta SSE vacia del servidor MCP")
        try:
            m = json.loads(body)
        except json.JSONDecodeError:
            raise MCPError(f"Respuesta no valida del servidor MCP: {body[:300]}")
        if isinstance(m, list):
            m = next((x for x in m if x.get("id") == want_id), m[-1])
        return m

    def _next_id(self):
        self._id += 1
        return self._id

    def connect(self):
        res = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": APP_NAME, "version": "1.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
        name = res.get("serverInfo", {}).get("name", "?")
        self.log(f"Conectado a MCP: {name}")
        return res

    def list_tools(self):
        return [t["name"] for t in self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {},
        }).get("tools", [])]

    def call(self, name, arguments):
        res = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise MCPError(f"La herramienta {name} devolvio un error: {text[:500]}")
        return res.get("structuredContent"), text


# ======================================================================
#  OAuth (inicio de sesion con la cuenta de Higgsfield, PKCE + registro dinamico)
# ======================================================================

import base64
import hashlib
import http.server
import secrets
import urllib.parse
import webbrowser

OAUTH_PORT = 53682
OAUTH_REDIRECT = f"http://127.0.0.1:{OAUTH_PORT}/callback"


def _http_json(url, data=None, form=False, headers=None, timeout=60):
    h = {"Accept": "application/json", "User-Agent": f"{APP_NAME}/1.0"}
    h.update(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise MCPError(f"HTTP {e.code} en {url}: {e.read().decode('utf-8', 'replace')[:400]}")


def oauth_discover(mcp_url):
    """Descubre el servidor de autorizacion a partir de la URL del MCP."""
    p = urllib.parse.urlparse(mcp_url)
    origin = f"{p.scheme}://{p.netloc}"
    prm = None
    for u in (f"{origin}/.well-known/oauth-protected-resource{p.path}",
              f"{origin}/.well-known/oauth-protected-resource"):
        try:
            prm = _http_json(u)
            break
        except Exception:
            continue
    if not prm or not prm.get("authorization_servers"):
        raise MCPError("El servidor MCP no publica su servidor de autorizacion.")
    issuer = prm["authorization_servers"][0].rstrip("/")
    ip = urllib.parse.urlparse(issuer)
    meta = None
    for u in (f"{issuer}/.well-known/oauth-authorization-server",
              f"{ip.scheme}://{ip.netloc}/.well-known/oauth-authorization-server{ip.path}",
              f"{issuer}/.well-known/openid-configuration"):
        try:
            meta = _http_json(u)
            break
        except Exception:
            continue
    if not meta:
        raise MCPError(f"No se pudo leer la configuracion OAuth de {issuer}")
    meta["_resource"] = prm.get("resource") or mcp_url
    meta["_scopes"] = prm.get("scopes_supported") or ["openid", "email", "offline_access"]
    return meta


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in q.items()}
        ok = "code" in q
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Listo, ya puedes cerrar esta pestana y volver a ClipForge." if ok
               else f"Error: {_CallbackHandler.result.get('error_description') or _CallbackHandler.result.get('error')}")
        self.wfile.write(f"<html><body style='font-family:sans-serif;background:#14161b;color:#eee;"
                         f"padding:40px'><h2>{APP_NAME}</h2><p>{msg}</p></body></html>".encode())


def oauth_login(cfg, log=print, timeout=300):
    """Abre el navegador para iniciar sesion y guarda los tokens en cfg['oauth']."""
    meta = oauth_discover(cfg["mcp_url"])
    oa = dict(cfg.get("oauth") or {})
    scope = " ".join(meta["_scopes"])
    if not oa.get("client_id") or oa.get("issuer") != meta.get("issuer"):
        if not meta.get("registration_endpoint"):
            raise MCPError("El servidor no permite registrar aplicaciones nuevas.")
        reg = _http_json(meta["registration_endpoint"], {
            "client_name": APP_NAME,
            "redirect_uris": [OAUTH_REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": scope,
        })
        oa = {"client_id": reg["client_id"], "client_secret": reg.get("client_secret"),
              "issuer": meta.get("issuer")}
        log(f"App registrada en Higgsfield (client_id {oa['client_id'][:12]}...)")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code", "client_id": oa["client_id"], "redirect_uri": OAUTH_REDIRECT,
        "scope": scope, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": meta["_resource"],
    }
    auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)

    _CallbackHandler.result = {}
    srv = http.server.HTTPServer(("127.0.0.1", OAUTH_PORT), _CallbackHandler)
    srv.timeout = 1
    log("Abriendo el navegador para iniciar sesion en Higgsfield...")
    webbrowser.open(auth_url)
    t0 = time.time()
    try:
        while not _CallbackHandler.result and time.time() - t0 < timeout:
            srv.handle_request()
    finally:
        srv.server_close()
    res = _CallbackHandler.result
    if not res:
        raise MCPError("No se completo el inicio de sesion (tiempo agotado).")
    if res.get("state") != state:
        raise MCPError("Respuesta de inicio de sesion no valida (state distinto).")
    if "code" not in res:
        raise MCPError(f"Higgsfield rechazo el inicio de sesion: {res.get('error_description') or res.get('error')}")

    tok = _token_request(meta["token_endpoint"], oa, {
        "grant_type": "authorization_code", "code": res["code"], "redirect_uri": OAUTH_REDIRECT,
        "code_verifier": verifier, "resource": meta["_resource"],
    })
    oa.update(_token_fields(tok, oa))
    oa["token_endpoint"] = meta["token_endpoint"]
    oa["resource"] = meta["_resource"]
    cfg["oauth"] = oa
    log("Sesion de Higgsfield iniciada correctamente.")
    return cfg


def _token_request(endpoint, oa, data):
    data = dict(data, client_id=oa["client_id"])
    if oa.get("client_secret"):
        data["client_secret"] = oa["client_secret"]
    return _http_json(endpoint, data, form=True)


def _token_fields(tok, oa):
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or oa.get("refresh_token"),
        "expires_at": time.time() + float(tok.get("expires_in") or 3600) - 60,
    }


def oauth_access_token(cfg, force_refresh=False):
    """Devuelve un access token valido, renovandolo si hace falta. None si no hay sesion."""
    oa = cfg.get("oauth") or {}
    if not oa.get("access_token"):
        return None
    if not force_refresh and time.time() < float(oa.get("expires_at") or 0):
        return oa["access_token"]
    if not oa.get("refresh_token"):
        return None
    tok = _token_request(oa["token_endpoint"], oa, {
        "grant_type": "refresh_token", "refresh_token": oa["refresh_token"],
        "resource": oa.get("resource", ""),
    })
    oa.update(_token_fields(tok, oa))
    cfg["oauth"] = oa
    save_config(cfg)
    return oa["access_token"]


def make_mcp_client(cfg, log):
    token = oauth_access_token(cfg)
    if token:
        return MCPClient(cfg["mcp_url"], token, "Authorization", "Bearer ", log=log)
    return MCPClient(cfg["mcp_url"], cfg["mcp_token"], cfg["mcp_auth_header"], cfg["mcp_auth_prefix"], log=log)


# ======================================================================
#  Higgsfield: analisis de momentos virales
# ======================================================================

ANALYSIS_PROMPT = """You are selecting viral moments from a Spanish-language video (likely a gaming livestream) for vertical short clips (TikTok, Reels, YouTube Shorts).

Find the {count} BEST moments, each EXACTLY {dur} seconds long. Prefer: funny reactions, fails, surprises, rage, punchlines, absurd situations, strong hooks in the first 2 seconds. Start each window 1-2 seconds before the setup so the hook lands immediately. Moments must not overlap.

Also estimate the total duration and whether there is a streamer facecam overlay. If there is, give its bounding box as fractions of the full frame (x, y = top-left corner; w, h = size; all between 0 and 1).

Reply with ONLY a JSON object, no extra text, exactly in this shape:
{{
  "duration": "hh:mm:ss",
  "facecam": {{"present": true, "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}},
  "clips": [
    {{"start": "hh:mm:ss", "end": "hh:mm:ss", "title": "titulo gancho en espanol con emoji", "description": "descripcion corta en espanol para el short, con una pregunta al final", "why": "por que puede ser viral"}}
  ]
}}"""

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _yaml_field(text, field):
    """Extrae un campo de la salida tipo YAML de Higgsfield (valor simple o string entre comillas)."""
    m = re.search(rf'^\s*{field}:\s*("(?:[^"\\]|\\.)*")', text, re.M | re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return m.group(1).strip('"')
    m = re.search(rf"^\s*{field}:\s*(.+)$", text, re.M)
    if m:
        v = m.group(1).strip()
        return None if v == "null" else v.strip('"')
    return None


def _result_field(structured, text, field):
    if isinstance(structured, dict):
        node = structured.get("result", structured)
        if isinstance(node, dict) and field in node:
            return node[field]
    return _yaml_field(text, field)


def extract_json_block(text):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        candidates.append(text[i:j + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


def fallback_parse_clips(text, dur):
    """Si Higgsfield no devuelve JSON, busca rangos de tiempo en el texto."""
    clips = []
    for m in re.finditer(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]+\s*(\d{1,2}:\d{2}(?::\d{2})?)", text):
        clips.append({"start": m.group(1), "end": m.group(2), "title": "", "description": "", "why": ""})
    return {"clips": clips, "facecam": {"present": False}}


def analyze_with_higgsfield(cfg, url, count, dur, log, cancel_evt):
    client = make_mcp_client(cfg, log)
    try:
        client.connect()
    except MCPError as e:
        if "401" in str(e) and (cfg.get("oauth") or {}).get("refresh_token"):
            log("Sesion vencida, renovando...")
            oauth_access_token(cfg, force_refresh=True)
            client = make_mcp_client(cfg, log)
            client.connect()
        else:
            raise
    log(f"Pidiendo a Higgsfield {count} momentos de {dur}s (modo {cfg['analysis_mode']})...")
    structured, text = client.call("video_analysis_create", {
        "youtube_url": url,
        "mode": cfg["analysis_mode"],
        "prompt": ANALYSIS_PROMPT.format(count=count, dur=dur),
    })
    job_id = _result_field(structured, text, "id")
    if not job_id or not re.fullmatch(UUID_RE, str(job_id)):
        m = re.search(UUID_RE, text or "")
        job_id = m.group(0) if m else None
    if not job_id:
        raise MCPError(f"Higgsfield no devolvio un id de analisis:\n{text[:500]}")
    log(f"Analisis en cola (id {job_id}). Suele tardar 2-5 minutos...")

    t0 = time.time()
    while True:
        if cancel_evt.is_set():
            raise Cancelled()
        time.sleep(15)
        structured, text = client.call("video_analysis_status", {"video_analyze_id": job_id})
        status = str(_result_field(structured, text, "status") or "").lower()
        log(f"  Estado: {status or '?'} ({int(time.time() - t0)}s)")
        if status == "completed":
            analysis = _result_field(structured, text, "analysis") or ""
            break
        if status == "failed":
            raise MCPError(f"El analisis fallo: {_result_field(structured, text, 'fail_reason')}")
        if time.time() - t0 > 20 * 60:
            raise MCPError("El analisis tardo mas de 20 minutos. Intentalo de nuevo.")

    data = extract_json_block(analysis)
    if not data or not data.get("clips"):
        log("Higgsfield no respondio en JSON; se extraen los tiempos del texto.")
        data = fallback_parse_clips(analysis, dur)
    if not data.get("clips"):
        raise MCPError(f"No se encontraron momentos en la respuesta:\n{analysis[:800]}")
    data["raw_analysis"] = analysis
    return data


# ======================================================================
#  Video: descarga, vertical, subtitulos
# ======================================================================

class Pipeline:
    def __init__(self, cfg, log, progress, cancel_evt, on_clip=None):
        self.on_clip = on_clip or (lambda c: None)
        self.cfg = cfg
        self.log = log
        self.progress = progress
        self.cancel_evt = cancel_evt
        self.proc = None

    # -- procesos --
    def run(self, cmd, cwd=None, quiet=False):
        if self.cancel_evt.is_set():
            raise Cancelled()
        self.log("> " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
        self.proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        tail = []
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail = (tail + [line])[-30:]
            if not quiet:
                self.log("  " + line)
            if self.cancel_evt.is_set():
                self.proc.kill()
                raise Cancelled()
        code = self.proc.wait()
        self.proc = None
        if code != 0:
            raise RuntimeError(f"{os.path.basename(str(cmd[0]))} fallo (codigo {code}):\n" + "\n".join(tail[-10:]))

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def probe_size(self, path):
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", path], creationflags=CREATE_NO_WINDOW,
        ).decode().strip().split(",")
        return int(out[0]), int(out[1])

    # -- descarga --
    def download(self, url, start, dur, out_path):
        a = to_seconds(start)
        cmd = ["yt-dlp", "--no-playlist", "--newline", "--no-part",
               "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
               "--download-sections", f"*{hms(a)}-{hms(a + dur)}",
               "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
               "--force-overwrites", "-o", out_path]
        if self.cfg.get("cookies") and os.path.isfile(self.cfg["cookies"]):
            cmd += ["--cookies", self.cfg["cookies"]]
        cmd.append(url)
        self.run(cmd, quiet=True)

    # -- vertical --
    @staticmethod
    def even(x):
        return max(2, int(x) // 2 * 2)

    def vertical_filter(self, sw, sh, cam):
        if not cam:
            return ("[0:v]split[bg][fg];"
                    f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2[bgb];"
                    f"[fg]scale={W}:-2[fgs];[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"), None
        e = self.even
        cx, cy, cw, ch = cam
        px, py, pw, ph = e(cx * sw), e(cy * sh), e(cw * sw), e(ch * sh)
        pw, ph = min(pw, sw - px), min(ph, sh - py)
        cam_h = min(e(W * ph / pw), H // 2)
        game_h = H - cam_h
        gw = e(min(sw, sh * W / game_h))
        gx = e((sw - gw) / 2)
        if cx < 0.5 and px < gx < px + pw and px + pw + gw <= sw:
            gx = e(px + pw)
        elif cx >= 0.5 and gx < px < gx + gw and px - gw >= 0:
            gx = e(px - gw)
        f = (f"[0:v]split[a][b];"
             f"[a]crop={pw}:{ph}:{px}:{py},scale={W}:{cam_h}:flags=lanczos[cam];"
             f"[b]crop={gw}:{sh}:{gx}:0,scale={W}:{game_h}:flags=lanczos[game];"
             f"[cam][game]vstack,format=yuv420p[v]")
        return f, cam_h

    def make_vertical(self, raw, out, dur, cam):
        sw, sh = self.probe_size(raw)
        vf, cam_h = self.vertical_filter(sw, sh, cam)
        self.run(["ffmpeg", "-y", "-v", "error", "-i", raw, "-t", str(dur),
                  "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                  "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out])
        return cam_h

    # -- subtitulos con whisper.cpp --
    def transcribe(self, video, base_out):
        cli, model = self.cfg["whisper_cli"], self.cfg["whisper_model"]
        wav = base_out + ".wav"
        self.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
                  "-c:a", "pcm_s16le", wav])
        cmd = [cli, "-m", model, "-f", wav, "-l", "es", "-ml", "25", "-sow",
               "-osrt", "-of", base_out, "-sns"]
        if int(self.cfg.get("whisper_threads") or 0) > 0:
            cmd += ["-t", str(self.cfg["whisper_threads"])]
        if self.cfg.get("whisper_prompt"):
            cmd += ["--prompt", self.cfg["whisper_prompt"]]
        self.run(cmd, quiet=False)
        try:
            os.remove(wav)
        except OSError:
            pass
        srt = base_out + ".srt"
        if not os.path.isfile(srt):
            raise RuntimeError("whisper-cli no genero el archivo .srt")
        return srt

    @staticmethod
    def read_srt(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        cues = []
        for block in re.split(r"\r?\n\r?\n", content.strip()):
            lines = block.strip().splitlines()
            ts = next((l for l in lines if "-->" in l), None)
            if not ts:
                continue
            a, b = [to_seconds(x.strip()) for x in ts.split("-->")]
            text = " ".join(lines[lines.index(ts) + 1:]).strip()
            text = re.sub(r"\[.*?\]|\(.*?\)|♪", "", text).strip()  # [Musica], (risas)...
            if text:
                cues.append([a, b, text])
        # cada subtitulo se mantiene hasta que aparece el siguiente (si la pausa es corta)
        for i in range(len(cues) - 1):
            gap = cues[i + 1][0] - cues[i][1]
            if 0 < gap < 0.6:
                cues[i][1] = cues[i + 1][0]
        return cues

    @staticmethod
    def write_srt(cues, path):
        def t(x):
            ms = int(round(x * 1000))
            return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"
        with open(path, "w", encoding="utf-8") as f:
            for i, (a, b, txt) in enumerate(cues, 1):
                f.write(f"{i}\n{t(a)} --> {t(b)}\n{txt}\n\n")

    SUB_STYLES = {
        # nombre: (fuente, tamano, color principal, contorno, sombra, mayusculas)
        "tiktok":  ("Arial Black", 78, "&H00FFFFFF", 7, 3, True),
        "hormozi": ("Arial Black", 84, "&H00FFFFFF", 8, 2, True),
        "minimal": ("Segoe UI Semibold", 62, "&H00FFFFFF", 3, 1, False),
        "beast":   ("Impact", 100, "&H00FFFFFF", 9, 4, True),
    }
    BEAST_COLORS = ["&H0000F0FF&", "&H0055FF55&", "&H00FFFFFF&", "&H00FF7AF5&"]

    def write_ass(self, cues, path, margin_v):
        def t(x):
            cs = int(round(x * 100))
            return f"{cs // 360000}:{cs % 360000 // 6000:02d}:{cs % 6000 // 100:02d}.{cs % 100:02d}"
        style = self.cfg.get("sub_style", "tiktok")
        font, size, color, outline, shadow, caps = self.SUB_STYLES.get(style, self.SUB_STYLES["tiktok"])
        if style == "tiktok" and self.cfg.get("font"):
            font, size = self.cfg["font"], int(self.cfg.get("font_size", size))
        head = (
            "[Script Info]\nScriptType: v4.00+\n"
            f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Main,{font},{size},{color},&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,"
            f"100,100,0,0,1,{outline},{shadow},2,90,90,{margin_v},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(head)
            for n, (a, b, txt) in enumerate(cues):
                txt = txt.replace("{", "(").replace("}", ")")
                if caps:
                    txt = txt.upper()
                if style == "hormozi":
                    words = txt.split()
                    if words:
                        k = max(range(len(words)), key=lambda i: len(re.sub(r"\W", "", words[i])))
                        words[k] = "{\\c&H0000E5FF&}" + words[k] + "{\\c&H00FFFFFF&}"
                        txt = " ".join(words)
                    fx = "{\\fscx80\\fscy80\\t(0,100,\\fscx100\\fscy100)}"
                elif style == "beast":
                    fx = ("{\\c" + self.BEAST_COLORS[n % len(self.BEAST_COLORS)] +
                          "\\fscx70\\fscy70\\frz-3\\t(0,80,\\fscx118\\fscy118\\frz2)"
                          "\\t(80,160,\\fscx100\\fscy100\\frz0)}")
                elif style == "minimal":
                    fx = "{\\fad(80,60)}"
                else:
                    fx = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}"
                f.write(f"Dialogue: 0,{t(a)},{t(b)},Main,,0,0,0,,{fx}{txt}\n")

    def thumbnail(self, video, out_jpg):
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.5", "-i", video, "-frames:v", "1",
                            "-vf", "scale=216:-2", "-q:v", "4", out_jpg],
                           check=True, creationflags=CREATE_NO_WINDOW)
            return out_jpg
        except Exception:
            return None

    def burn(self, workdir, vert_name, ass_name, out_name):
        self.run(["ffmpeg", "-y", "-v", "error", "-i", vert_name, "-vf", f"subtitles={ass_name}",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                  "-c:a", "copy", "-movflags", "+faststart", out_name], cwd=workdir)

    # -- orquestacion --
    def process(self, url, data, dur, video_title):
        clips = data["clips"]
        cam = None
        mode = self.cfg.get("cam_mode", "auto")
        if mode == "manual":
            cam = [float(x) for x in self.cfg["cam_manual"].split(",")]
        elif mode == "auto":
            fc = data.get("facecam") or {}
            if fc.get("present") and all(k in fc for k in ("x", "y", "w", "h")) and fc["w"] > 0.05 and fc["h"] > 0.05:
                cam = [float(fc["x"]), float(fc["y"]), float(fc["w"]), float(fc["h"])]
        self.log(f"Facecam: {'x,y,w,h = ' + ','.join(f'{v:.2f}' for v in cam) if cam else 'sin facecam (fondo desenfocado)'}")

        outdir = os.path.join(self.cfg["output_dir"], time.strftime("%Y-%m-%d_%H%M") + "_" + safe_name(video_title, 30))
        os.makedirs(outdir, exist_ok=True)
        summary = [f"Video: {url}", f"Generado: {time.strftime('%Y-%m-%d %H:%M')}", ""]
        done = []
        total = len(clips)
        for i, c in enumerate(clips, 1):
            if self.cancel_evt.is_set():
                raise Cancelled()
            start = c["start"]
            tag = f"clip{i}_{start.replace(':', '-')}"
            if c.get("title"):
                tag += "_" + safe_name(c["title"], 30)
            raw, vert = os.path.join(outdir, tag + "_raw.mp4"), os.path.join(outdir, tag + "_vertical.mp4")
            step = lambda k: self.progress(((i - 1) + k / 4) / total * 100, f"Clip {i}/{total}")

            self.log(f"\n=== Clip {i}/{total}: {start} (+{dur}s) — {c.get('title', '')} ===")
            step(0); self.log("Descargando tramo...")
            self.download(url, start, dur, raw)
            step(1); self.log("Pasando a vertical 9:16...")
            cam_h = self.make_vertical(raw, vert, dur, cam)
            os.remove(raw)
            final = vert
            if self.cfg.get("burn_subs", True):
                step(2); self.log("Transcribiendo con Whisper (large-v3-turbo)...")
                srt = self.transcribe(vert, os.path.join(outdir, tag))
                cues = self.read_srt(srt)
                self.write_srt(cues, srt)
                if cues:
                    step(3); self.log(f"Quemando {len(cues)} subtitulos...")
                    margin_v = max(220, H - cam_h - 230) if cam_h else 560
                    self.write_ass(cues, os.path.join(outdir, tag + ".ass"), margin_v)
                    final_name = tag + "_subtitulado.mp4"
                    self.burn(outdir, os.path.basename(vert), tag + ".ass", final_name)
                    final = os.path.join(outdir, final_name)
                else:
                    self.log("No se detecto voz en este clip; queda sin subtitulos.")
            done.append(final)
            summary += [f"--- Clip {i}: {start} - {c.get('end', '')} ---",
                        f"Archivo: {os.path.basename(final)}",
                        f"Titulo: {c.get('title', '')}",
                        f"Descripcion: {c.get('description', '')}",
                        f"Por que: {c.get('why', '')}", ""]
            self.log(f"Listo: {final}")
            thumb = self.thumbnail(final, os.path.join(outdir, tag + "_thumb.jpg"))
            self.on_clip({"index": i, "file": final, "thumb": thumb, "start": start,
                          "end": c.get("end") or hms(to_seconds(start) + dur, ms=False),
                          "title": c.get("title", ""), "description": c.get("description", ""),
                          "why": c.get("why", ""), "duration": dur, "folder": outdir})

        with open(os.path.join(outdir, "resumen.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(summary))
            if data.get("raw_analysis"):
                f.write("\n\n=== Respuesta completa de Higgsfield ===\n" + data["raw_analysis"])
        self.progress(100, "Terminado")
        return outdir, done


# ======================================================================
#  Interfaz (HTML en ventana nativa con pywebview; si no esta, en el navegador)
# ======================================================================

UI_DIR = os.path.join(APP_DIR, "ui")
SETTINGS_KEYS = ["mcp_url", "mcp_token", "mcp_auth_header", "mcp_auth_prefix", "analysis_mode",
                 "whisper_cli", "whisper_model", "whisper_prompt", "whisper_threads", "cookies",
                 "output_dir", "cam_mode", "cam_manual", "burn_subs", "font", "font_size", "sub_style"]


class Api:
    """Metodos que llama la interfaz. Todo lo que devuelve es JSON."""

    def __init__(self):
        self._cfg = load_config()
        self._lock = threading.Lock()
        self._events = []          # logs pendientes para la interfaz
        self._state = {"running": False, "progress": 0, "status": "Listo", "phase": "idle",
                      "clips": [], "outdir": None, "video_duration": None}
        self._conn = {"state": "unknown", "detail": ""}   # unknown|checking|connected|disconnected|error
        self._cancel_evt = threading.Event()
        self._pipeline = None
        self._window = None
        threading.Thread(target=self._check_connection, daemon=True).start()

    # ---------- utilidades internas ----------
    def _log(self, msg, kind=None):
        if kind is None:
            m = msg.strip()
            kind = ("head" if m.startswith("===") else "ok" if m.startswith(("Listo", "✔")) else
                    "cmd" if m.startswith(">") else "err" if m.startswith(("ERROR", "Error")) else "info")
        with self._lock:
            self._events.append({"t": time.strftime("%H:%M:%S"), "msg": msg.strip("\n"), "kind": kind})

    def _progress(self, pct, text):
        with self._lock:
            self._state["progress"] = round(pct, 1)
            self._state["status"] = text

    def _set_conn(self, state, detail=""):
        with self._lock:
            self._conn = {"state": state, "detail": detail}

    def _check_connection(self):
        has_session = (self._cfg.get("oauth") or {}).get("access_token") or self._cfg.get("mcp_token")
        if not has_session:
            self._set_conn("disconnected")
            return
        self._set_conn("checking")
        try:
            c = make_mcp_client(self._cfg, lambda *_: None)
            try:
                c.connect()
            except MCPError as e:
                if "401" in str(e) and (self._cfg.get("oauth") or {}).get("refresh_token"):
                    oauth_access_token(self._cfg, force_refresh=True)
                    c = make_mcp_client(self._cfg, lambda *_: None)
                    c.connect()
                else:
                    raise
            tools = c.list_tools()
            if "video_analysis_create" in tools:
                self._set_conn("connected", f"{len(tools)} herramientas")
            else:
                self._set_conn("error", "Conectado, pero sin analisis de video")
        except Exception as e:
            msg = str(e)
            self._set_conn("disconnected" if "401" in msg else "error", msg[:200])

    @staticmethod
    def _file_url(path):
        if not path or not os.path.isfile(path):
            return None
        import base64 as b64
        with open(path, "rb") as f:
            return "data:image/jpeg;base64," + b64.b64encode(f.read()).decode()

    # ---------- llamadas desde la interfaz ----------
    def init(self):
        tools = {t: bool(shutil.which(t)) for t in ("yt-dlp", "ffmpeg", "ffprobe")}
        tools["whisper"] = os.path.isfile(self._cfg.get("whisper_cli", ""))
        tools["modelo"] = os.path.isfile(self._cfg.get("whisper_model", ""))
        return {"settings": {k: self._cfg.get(k) for k in SETTINGS_KEYS},
                "last_duration": self._cfg.get("last_duration", 30),
                "last_count": self._cfg.get("last_count", 3),
                "tools": tools, "version": "2.0"}

    def poll(self):
        with self._lock:
            ev, self._events = self._events, []
            st = dict(self._state)
            st["clips"] = list(self._state["clips"])
            conn = dict(self._conn)
        return {"events": ev, "state": st, "conn": conn}

    def save_settings(self, data):
        for k in SETTINGS_KEYS:
            if k in data:
                v = data[k]
                if k in ("whisper_threads", "font_size"):
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        v = DEFAULTS[k]
                if k == "burn_subs":
                    v = bool(v)
                self._cfg[k] = v
        save_config(self._cfg)
        self._log("Ajustes guardados.", "ok")
        return self.init()

    def set_style(self, style):
        if style in Pipeline.SUB_STYLES:
            self._cfg["sub_style"] = style
            save_config(self._cfg)
        return style

    def login(self):
        if self._conn["state"] == "checking":
            return False
        self._set_conn("checking", "Esperando el navegador...")

        def job():
            try:
                cfg = oauth_login(dict(self._cfg), log=self._log)
                self._cfg = cfg
                save_config(cfg)
            except Exception as e:
                self._log(f"No se pudo iniciar sesion: {e}", "err")
            self._check_connection()
            if self._conn["state"] == "connected":
                self._log("Higgsfield conectado.", "ok")
        threading.Thread(target=job, daemon=True).start()
        return True

    def logout(self):
        oa = self._cfg.get("oauth") or {}
        # se conserva el registro de la app, se borra la sesion
        self._cfg["oauth"] = {k: v for k, v in oa.items() if k in ("client_id", "client_secret", "issuer")}
        save_config(self._cfg)
        self._set_conn("disconnected")
        self._log("Sesion de Higgsfield cerrada.")
        return True

    def recheck(self):
        threading.Thread(target=self._check_connection, daemon=True).start()
        return True

    def start(self, url, dur, count, manual, style):
        if self._state["running"]:
            return {"ok": False, "error": "Ya hay un proceso en marcha."}
        url = (url or "").strip()
        if not re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", url):
            return {"ok": False, "error": "Pega un link de YouTube valido."}
        try:
            dur, count = int(dur), int(count)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Duracion y cantidad deben ser numeros."}
        manual_list = [t for t in re.split(r"[\s,;]+", (manual or "").strip()) if t]
        manual_list = [t.split("-")[0] for t in manual_list]
        if not manual_list and self._conn["state"] != "connected":
            return {"ok": False, "error": "Inicia sesion en Higgsfield o escribe tiempos manuales."}
        self.set_style(style)
        self._cfg["last_duration"], self._cfg["last_count"] = dur, count
        save_config(self._cfg)
        self._cancel_evt.clear()
        with self._lock:
            self._state.update(running=True, progress=1, status="Iniciando...", phase="analyzing",
                              clips=[], outdir=None, video_duration=None)
        threading.Thread(target=self._work, args=(url, dur, count, manual_list), daemon=True).start()
        return {"ok": True}

    def _work(self, url, dur, count, manual):
        try:
            self._progress(2, "Buscando momentos...")
            if manual:
                self._log(f"Usando tiempos manuales: {', '.join(manual)}")
                data = {"clips": [{"start": t, "end": hms(to_seconds(t) + dur, ms=False), "title": "",
                                   "description": "", "why": ""} for t in manual],
                        "facecam": {"present": False}}
            else:
                data = analyze_with_higgsfield(self._cfg, url, count, dur, self._log, self._cancel_evt)
                data["clips"] = data["clips"][:count]
            with self._lock:
                self._state["video_duration"] = data.get("duration")
                self._state["phase"] = "processing"
            self._log("Momentos encontrados:", "head")
            for i, c in enumerate(data["clips"], 1):
                self._log(f"  {i}. {c.get('start')} — {c.get('title', '')}")

            def on_clip(c):
                c = dict(c)
                c["thumb"] = self._file_url(c.get("thumb"))
                with self._lock:
                    self._state["clips"].append(c)

            self._pipeline = Pipeline(self._cfg, self._log, self._progress, self._cancel_evt, on_clip)
            outdir, files = self._pipeline.process(url, data, dur, data["clips"][0].get("title") or "video")
            with self._lock:
                self._state.update(outdir=outdir, status=f"{len(files)} clips listos", phase="done", progress=100)
            self._log(f"✔ {len(files)} clips listos en: {outdir}", "ok")
            self._log("Revisa resumen.txt para titulos y descripciones.", "ok")
        except Cancelled:
            with self._lock:
                self._state.update(status="Cancelado", phase="idle")
            self._log("Proceso cancelado.", "err")
        except Exception as e:
            self._log(f"ERROR: {e}", "err")
            self._log(traceback.format_exc(), "err")
            with self._lock:
                self._state.update(status="Error", phase="error")
        finally:
            with self._lock:
                self._state["running"] = False

    def cancel(self):
        self._cancel_evt.set()
        if self._pipeline:
            self._pipeline.kill()
        self._progress(self._state["progress"], "Cancelando...")
        return True

    def open_path(self, path=None):
        path = path or self._state.get("outdir") or self._cfg["output_dir"]
        if os.path.isdir(path) or not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True

    def show_in_folder(self, path):
        if os.name == "nt" and os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        return self.open_path(os.path.dirname(path))

    def browse(self, kind):
        """kind: file | dir. Solo funciona en la ventana nativa."""
        if not self._window:
            return None
        import webview
        ftype = webview.FOLDER_DIALOG if kind == "dir" else webview.OPEN_DIALOG
        res = self._window.create_file_dialog(ftype)
        if res:
            return os.path.normpath(res[0] if isinstance(res, (list, tuple)) else res)
        return None


# ---------- modo navegador (respaldo y pruebas) ----------

def serve_browser(api, port=8765, open_browser=True):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/icon.png":
                with open(os.path.join(UI_DIR, "icon.png"), "rb") as f:
                    return self._send(200, f.read(), "image/png")
            if path in ("/", "/index.html"):
                with open(os.path.join(UI_DIR, "index.html"), "rb") as f:
                    html = f.read().replace(b"<head>", b"<head><script>window.__CF_HTTP=true</script>", 1)
                return self._send(200, html, "text/html; charset=utf-8")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            name = self.path.strip("/").split("/")[-1]
            if name.startswith("_") or not hasattr(api, name):
                return self._send(404, {"error": "metodo desconocido"})
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"[]")
            try:
                self._send(200, {"result": getattr(api, name)(*args)})
            except Exception as e:
                self._send(500, {"error": str(e)})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} en {url}")
    if open_browser:
        webbrowser.open(url)
    srv.serve_forever()


ICON_PATH = os.path.join(APP_DIR, "icon.ico")
WINDOW_TITLE = f"{APP_NAME} — clips virales automaticos"


def windows_app_id():
    """Hace que la barra de tareas muestre el icono de ClipForge y no el de Python."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipForge.App.2")
    except Exception:
        pass


def set_window_icon(title=WINDOW_TITLE, ico=ICON_PATH):
    """Pone icon.ico en la ventana (barra de titulo y barra de tareas) en Windows."""
    if os.name != "nt" or not os.path.isfile(ico):
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        hwnd = None
        for _ in range(60):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            return
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
        big = user32.LoadImageW(None, ico, IMAGE_ICON, user32.GetSystemMetrics(11),
                                user32.GetSystemMetrics(12), LR_LOADFROMFILE)
        small = user32.LoadImageW(None, ico, IMAGE_ICON, user32.GetSystemMetrics(49),
                                  user32.GetSystemMetrics(50), LR_LOADFROMFILE)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, 1, big)
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, 0, small)
    except Exception:
        pass


def main():
    windows_app_id()
    api = Api()
    if "--browser" not in sys.argv:
        try:
            import webview
        except ImportError:
            webview = None
        if webview:
            win = webview.create_window(
                WINDOW_TITLE, url=os.path.join(UI_DIR, "index.html"),
                js_api=api, width=1360, height=880, min_size=(1000, 680), background_color="#060709")
            api._window = win
            win.events.shown += lambda: threading.Thread(target=set_window_icon, daemon=True).start()
            # En Windows el icono debe ser .ico (un .png hace fallar la ventana)
            icon = ICON_PATH if os.name == "nt" else os.path.join(UI_DIR, "icon.png")
            try:
                webview.start(icon=icon if os.path.isfile(icon) else None)
            except TypeError:
                webview.start()
            return
        print("pywebview no esta instalado (pip install pywebview). Abriendo en el navegador...")
    serve_browser(api, open_browser="--no-open" not in sys.argv)


LOG_PATH = os.path.join(APP_DIR, "clipforge_error.log")


def report_crash(exc_text):
    """Guarda el error y lo muestra (con pythonw no hay consola donde verlo)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("\n[%Y-%m-%d %H:%M:%S]\n") + exc_text)
    except Exception:
        pass
    print(exc_text, file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, f"ClipForge no pudo iniciar:\n\n{exc_text[-900:]}\n\nDetalle en clipforge_error.log",
                "ClipForge - error", 0x10)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        report_crash(traceback.format_exc())
