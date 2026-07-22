#!/usr/bin/env python3
"""
App Launcher - un unico proceso para centralizar el lanzamiento de tus apps.

Arquitectura deliberadamente simple:
  - Solo libreria estandar de Python (sin pip install, sin node_modules).
  - Descubre apps escaneando carpetas en busca de un archivo `launcher.json`.
  - Asigna puertos libres reales y los sustituye en comandos/env/URL.
  - Lanza cada app como un grupo de procesos y permite detenerlo.

Correr:  python launcher.py
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_DIR = os.path.join(HERE, "logs")

# Nombres de carpeta que nunca contienen apps registrables (se ignoran al escanear).
SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", "__pycache__",
             "dist", "build", ".idea", ".vscode"}

DEFAULT_CONFIG = {
    # Carpetas donde buscar apps (relativas a este archivo o absolutas).
    # "..": la carpeta padre (p.ej. C:\\Desarrollo) donde viven tus apps.
    "roots": ["..", "examples"],
    # Puerto del propio launcher (si esta ocupado, se prueba el siguiente).
    "port": 8765,
    # Rango de puertos que el launcher puede asignar a las apps.
    "port_range": [3000, 9999],
    # Abrir el navegador automaticamente al arrancar.
    "open_browser": True,
}

# Estado en memoria de apps corriendo.  id -> dict
RUNNING = {}
LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Configuracion
# --------------------------------------------------------------------------- #
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception as exc:  # config malformada: seguir con defaults
            print(f"[warn] config.json no se pudo leer: {exc}")
    else:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, indent=2)
            print(f"[info] config.json creado en {CONFIG_PATH}")
        except Exception:
            pass
    return cfg


CONFIG = load_config()


def resolve_root(root):
    return root if os.path.isabs(root) else os.path.normpath(os.path.join(HERE, root))


# --------------------------------------------------------------------------- #
# Puertos
# --------------------------------------------------------------------------- #
def is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def alloc_port(preferred=None, exclude=frozenset()):
    """Devuelve un puerto libre, preferiblemente `preferred`."""
    lo, hi = CONFIG.get("port_range", [3000, 9999])
    if preferred and preferred not in exclude and is_free(preferred):
        return preferred
    # busca dentro del rango
    for _ in range(500):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if lo <= p <= hi and p not in exclude:
            return p
    # fallback: cualquier puerto que de el SO
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# Descubrimiento de apps
# --------------------------------------------------------------------------- #
def parse_ports_decl(decl):
    """Normaliza la declaracion `ports` a lista de (name, preferred|None)."""
    out = []
    for item in decl or []:
        if isinstance(item, str):
            out.append((item, None))
        elif isinstance(item, dict) and "name" in item:
            out.append((item["name"], item.get("preferred")))
    return out


def reg_timestamp(manifest, path):
    """Fecha de ingreso: campo registered_at si existe, si no fecha del archivo."""
    ra = manifest.get("registered_at")
    if ra:
        try:
            return datetime.fromisoformat(ra.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    try:
        return os.path.getctime(path)
    except Exception:
        return os.path.getmtime(path)


def discover_apps():
    apps = []
    seen = set()
    for root in CONFIG.get("roots", []):
        base = resolve_root(root)
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            if entry in SKIP_DIRS:
                continue
            app_dir = os.path.join(base, entry)
            manifest_path = os.path.join(app_dir, "launcher.json")
            if not os.path.isfile(manifest_path):
                continue
            real = os.path.normpath(app_dir).lower()
            if real in seen:
                continue
            seen.add(real)
            app = build_app(entry, app_dir, manifest_path)
            apps.append(app)
    return apps


def build_app(folder, app_dir, manifest_path):
    app_id = folder
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            m = json.load(fh)
        error = None
    except Exception as exc:
        m = {}
        error = f"launcher.json invalido: {exc}"

    ts = reg_timestamp(m, manifest_path)
    procs = m.get("processes", [])
    ports = parse_ports_decl(m.get("ports"))
    return {
        "id": app_id,
        "name": m.get("name", folder),
        "description": m.get("description", ""),
        "dir": app_dir,
        "rel_dir": os.path.relpath(app_dir, HERE),
        "processes": procs,
        "ports": [p[0] for p in ports],
        "ports_decl": ports,
        "open": m.get("open", ""),
        "registered_at": datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
        "registered_ts": ts,
        "error": error,
    }


# --------------------------------------------------------------------------- #
# Lanzar / detener
# --------------------------------------------------------------------------- #
def substitute(text, mapping):
    if not isinstance(text, str):
        return text
    for name, val in mapping.items():
        text = text.replace("{" + name + "}", str(val))
    return text


def launch_app(app_id):
    with LOCK:
        if app_id in RUNNING:
            return False, "La app ya esta corriendo."

    apps = {a["id"]: a for a in discover_apps()}
    app = apps.get(app_id)
    if not app:
        return False, "App no encontrada."
    if app["error"]:
        return False, app["error"]
    if not app["processes"]:
        return False, "El manifiesto no declara 'processes'."

    # Asignar puertos
    port_map = {}
    used = set()
    for name, preferred in app["ports_decl"]:
        p = alloc_port(preferred, exclude=used)
        used.add(p)
        port_map[name] = p

    os.makedirs(LOG_DIR, exist_ok=True)
    procs = []
    logs = []
    try:
        for idx, proc in enumerate(app["processes"]):
            cmd = substitute(proc.get("cmd", ""), port_map)
            if not cmd.strip():
                continue
            env = os.environ.copy()
            for k, v in (proc.get("env") or {}).items():
                env[k] = substitute(v, port_map)

            pname = proc.get("name", f"proc{idx}")
            # Directorio de trabajo del proceso: por defecto la carpeta de la app,
            # o una subcarpeta relativa (p.ej. "web" para un frontend en su propio dir).
            proc_cwd = os.path.normpath(os.path.join(app["dir"], proc.get("cwd", ".")))
            log_path = os.path.join(LOG_DIR, f"{app_id}__{pname}.log")
            log_fh = open(log_path, "ab")
            header = (f"\n===== {datetime.now().isoformat()} :: (cwd={proc_cwd}) "
                      f"{cmd} =====\n")
            log_fh.write(header.encode("utf-8", "replace"))
            log_fh.flush()

            popen = spawn(cmd, proc_cwd, env, log_fh)
            procs.append(popen)
            logs.append((log_fh, log_path))
    except Exception as exc:
        for p in procs:
            _kill(p)
        for fh, _ in logs:
            fh.close()
        return False, f"Error al lanzar: {exc}"

    open_url = substitute(app["open"], port_map)
    with LOCK:
        RUNNING[app_id] = {
            "procs": procs,
            "logs": logs,
            "ports": port_map,
            "open": open_url,
            "started": time.time(),
        }
    return True, {"ports": port_map, "open": open_url}


def spawn(cmd, cwd, env, log_fh):
    kwargs = dict(cwd=cwd, env=env, shell=True, stdout=log_fh,
                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _kill(popen):
    try:
        if popen.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(popen.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
    except Exception:
        pass


def stop_app(app_id):
    with LOCK:
        state = RUNNING.pop(app_id, None)
    if not state:
        return False, "La app no esta corriendo."
    for p in state["procs"]:
        _kill(p)
    for fh, _ in state["logs"]:
        try:
            fh.close()
        except Exception:
            pass
    return True, "Detenida."


def is_running(app_id):
    with LOCK:
        state = RUNNING.get(app_id)
        if not state:
            return False
        alive = any(p.poll() is None for p in state["procs"])
        if not alive:
            RUNNING.pop(app_id, None)
        return alive


def running_info(app_id):
    with LOCK:
        state = RUNNING.get(app_id)
        if not state:
            return None
        return {"ports": state["ports"], "open": state["open"],
                "started": state["started"]}


def read_logs(app_id, max_bytes=8000):
    with LOCK:
        state = RUNNING.get(app_id)
    paths = []
    if state:
        paths = [lp for _, lp in state["logs"]]
    else:
        # buscar logs por prefijo
        if os.path.isdir(LOG_DIR):
            for f in os.listdir(LOG_DIR):
                if f.startswith(app_id + "__"):
                    paths.append(os.path.join(LOG_DIR, f))
    chunks = []
    for lp in paths:
        try:
            with open(lp, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                data = fh.read().decode("utf-8", "replace")
            chunks.append(f"----- {os.path.basename(lp)} -----\n{data}")
        except Exception:
            pass
    return "\n\n".join(chunks) if chunks else "(sin logs todavia)"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def apps_payload():
    apps = discover_apps()
    for a in apps:
        a["running"] = is_running(a["id"])
        info = running_info(a["id"]) if a["running"] else None
        a["live_open"] = info["open"] if info else ""
        a["live_ports"] = info["ports"] if info else {}
        # no serializar internos
        a.pop("ports_decl", None)
    return {"apps": apps, "count": len(apps)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silencio

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/apps":
            self._json(200, apps_payload())
        elif path == "/api/logs":
            qs = parse_qs(parsed.query)
            app_id = (qs.get("id") or [""])[0]
            self._json(200, {"logs": read_logs(app_id)})
        else:
            self._json(404, {"error": "no encontrado"})

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        app_id = body.get("id", "")

        if parsed.path == "/api/launch":
            ok, res = launch_app(app_id)
            self._json(200 if ok else 400, {"ok": ok, "result": res})
        elif parsed.path == "/api/stop":
            ok, res = stop_app(app_id)
            self._json(200 if ok else 400, {"ok": ok, "result": res})
        else:
            self._json(404, {"error": "no encontrado"})


def cleanup_on_exit():
    for app_id in list(RUNNING.keys()):
        stop_app(app_id)


def main():
    port = CONFIG.get("port", 8765)
    server = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        print("[error] no se encontro puerto libre para el launcher.")
        sys.exit(1)

    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print(f"  App Launcher corriendo en  {url}")
    print(f"  Escaneando: {[resolve_root(r) for r in CONFIG.get('roots', [])]}")
    print("  Ctrl+C para salir (detiene todas las apps lanzadas).")
    print("=" * 60)

    if CONFIG.get("open_browser", True):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo apps lanzadas...")
    finally:
        cleanup_on_exit()
        server.shutdown()


# --------------------------------------------------------------------------- #
# UI (una sola pagina, sin dependencias externas)
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>App Launcher</title>
<style>
  :root {
    --bg:#0f1117; --card:#1a1d27; --card2:#20242f; --line:#2b2f3c;
    --fg:#e6e8ee; --mut:#9aa0ae; --accent:#5b8cff; --green:#3ecf8e; --red:#ff6b6b;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f5f8; --card:#fff; --card2:#f0f2f6; --line:#e2e5ec;
            --fg:#1a1d27; --mut:#666c7a; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { display:flex; align-items:center; gap:16px; flex-wrap:wrap;
           padding:18px 24px; border-bottom:1px solid var(--line); position:sticky; top:0;
           background:var(--bg); z-index:5; }
  h1 { font-size:19px; margin:0; }
  .count { color:var(--mut); font-size:13px; }
  .spacer { flex:1; }
  .controls { display:flex; gap:8px; align-items:center; }
  button, select { font:inherit; color:var(--fg); background:var(--card2);
    border:1px solid var(--line); border-radius:8px; padding:7px 12px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  button.danger { background:transparent; color:var(--red); border-color:var(--red); }
  button:disabled { opacity:.5; cursor:default; }
  main { padding:20px 24px; display:grid; gap:14px;
         grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px; display:flex; flex-direction:column; gap:10px; }
  .card.live { border-color:var(--green); }
  .top { display:flex; align-items:flex-start; gap:10px; }
  .name { font-weight:600; font-size:16px; }
  .desc { color:var(--mut); font-size:13px; }
  .dot { width:9px; height:9px; border-radius:50%; margin-top:6px; flex:none;
         background:var(--mut); }
  .dot.on { background:var(--green); box-shadow:0 0 0 3px rgba(62,207,142,.2); }
  .meta { color:var(--mut); font-size:12px; display:flex; flex-wrap:wrap; gap:4px 12px; }
  .meta code { background:var(--card2); padding:1px 6px; border-radius:5px;
               color:var(--fg); font-size:11.5px; }
  .row { display:flex; gap:8px; align-items:center; margin-top:2px; }
  .row a { color:#fff; text-decoration:none; }
  .err { color:var(--red); font-size:12.5px; }
  .empty { color:var(--mut); grid-column:1/-1; text-align:center; padding:60px 0; }
  pre.logs { background:#000; color:#c8f7c5; font-size:11.5px; padding:10px;
    border-radius:8px; max-height:220px; overflow:auto; white-space:pre-wrap;
    margin:0; display:none; }
</style>
</head>
<body>
<header>
  <h1>App Launcher</h1>
  <span class="count" id="count"></span>
  <div class="spacer"></div>
  <div class="controls">
    <label class="count">Ordenar:</label>
    <select id="sort">
      <option value="date">Fecha de ingreso (recientes primero)</option>
      <option value="name">Nombre (A-Z)</option>
    </select>
    <button id="refresh">Reescanear</button>
  </div>
</header>
<main id="main"><div class="empty">Cargando...</div></main>

<script>
let APPS = [];
let sortKey = "date";

async function fetchApps() {
  const r = await fetch("/api/apps");
  const d = await r.json();
  APPS = d.apps;
  render();
}

function sortApps(list) {
  const l = [...list];
  if (sortKey === "name")
    l.sort((a,b) => a.name.localeCompare(b.name));
  else
    l.sort((a,b) => b.registered_ts - a.registered_ts);
  return l;
}

function esc(s){ return (s||"").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c])); }

function render() {
  const main = document.getElementById("main");
  document.getElementById("count").textContent = APPS.length + " apps";
  if (!APPS.length) {
    main.innerHTML = '<div class="empty">No se encontraron apps.<br>' +
      'Registra una creando un <code>launcher.json</code> en la carpeta de la app.</div>';
    return;
  }
  main.innerHTML = "";
  for (const a of sortApps(APPS)) {
    const card = document.createElement("div");
    card.className = "card" + (a.running ? " live" : "");
    const portsTxt = a.ports.length ? a.ports.join(", ") : "ninguno";
    const openUrl = a.running ? a.live_open : a.open;
    card.innerHTML = `
      <div class="top">
        <div class="dot ${a.running?'on':''}"></div>
        <div>
          <div class="name">${esc(a.name)}</div>
          <div class="desc">${esc(a.description)}</div>
        </div>
      </div>
      <div class="meta">
        <span>Ruta: <code>${esc(a.rel_dir)}</code></span>
        <span>Puertos: <code>${esc(portsTxt)}</code></span>
        <span>Ingreso: ${esc(a.registered_at)}</span>
      </div>
      ${a.error ? `<div class="err">${esc(a.error)}</div>` : ""}
      <div class="row">
        ${a.running
          ? `<button class="danger" data-stop="${esc(a.id)}">Detener</button>`
          : `<button class="primary" data-launch="${esc(a.id)}" ${a.error?'disabled':''}>Lanzar</button>`}
        ${a.running && openUrl ? `<a href="${esc(openUrl)}" target="_blank"><button class="primary">Abrir</button></a>` : ""}
        <button data-logs="${esc(a.id)}">Logs</button>
      </div>
      <pre class="logs" id="logs-${esc(a.id)}"></pre>`;
    main.appendChild(card);
  }
}

async function launch(id, btn) {
  btn.disabled = true; btn.textContent = "Lanzando...";
  const r = await fetch("/api/launch", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({id})});
  const d = await r.json();
  if (!d.ok) alert("No se pudo lanzar: " + JSON.stringify(d.result));
  const open = d.ok && d.result.open;
  await fetchApps();
  if (open) setTimeout(() => window.open(open, "_blank"), 1200);
}

async function stop(id) {
  await fetch("/api/stop", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({id})});
  await fetchApps();
}

async function toggleLogs(id) {
  const pre = document.getElementById("logs-" + id);
  if (pre.style.display === "block") { pre.style.display = "none"; return; }
  const r = await fetch("/api/logs?id=" + encodeURIComponent(id));
  const d = await r.json();
  pre.textContent = d.logs;
  pre.style.display = "block";
  pre.scrollTop = pre.scrollHeight;
}

document.getElementById("main").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.launch) launch(b.dataset.launch, b);
  else if (b.dataset.stop) stop(b.dataset.stop);
  else if (b.dataset.logs) toggleLogs(b.dataset.logs);
});
document.getElementById("sort").addEventListener("change", e => {
  sortKey = e.target.value; render();
});
document.getElementById("refresh").addEventListener("click", fetchApps);

fetchApps();
setInterval(fetchApps, 3000);  // refresca estado de apps corriendo
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
