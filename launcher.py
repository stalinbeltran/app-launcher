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
# Puertos reservados durante la ventana entre asignar y que el proceso los enlace.
RESERVED = set()
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
def _ipv6_available():
    """Hay pila IPv6 utilizable? Se calcula una vez, no en cada is_free()."""
    if not socket.has_ipv6:
        return False
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False


HAS_IPV6 = _ipv6_available()


def is_free(port):
    # OJO: NO usar SO_REUSEADDR aqui. En Windows, REUSEADDR permite enlazar a un
    # puerto que YA esta en uso, con lo que is_free daria True para un puerto
    # ocupado y dos apps terminarian compartiendo puerto.
    #
    # Hay que mirar IPv4 *y* IPv6: Node (y por tanto Vite) escucha en "localhost",
    # que resuelve primero a ::1, asi que un puerto puede estar ocupado SOLO en
    # IPv6. Comprobando nada mas 127.0.0.1 lo dabamos por libre, se lo asignabamos
    # al front y este moria al arrancar con "Port N is already in use".
    families = [(socket.AF_INET, "127.0.0.1")]
    if HAS_IPV6:
        families.append((socket.AF_INET6, "::1"))
    for family, addr in families:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            try:
                s.bind((addr, port))
            except OSError:
                return False
    return True


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
        if lo <= p <= hi and p not in exclude and is_free(p):
            return p
    # Fallback: cualquier puerto que de el SO. Pasa por is_free igual que el resto:
    # el SO lo elige mirando solo IPv4, y en Windows los puertos efimeros caen
    # fuera de port_range, asi que en la practica casi todos salen por aqui.
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if p not in exclude and is_free(p):
            return p
    return p


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

    # Asignar puertos de forma atomica: excluir los que ya usan otras apps
    # corriendo y los reservados por lanzamientos en curso, y reservar los
    # nuevos de inmediato para que dos apps nunca reciban el mismo puerto.
    with LOCK:
        taken = set(RESERVED)
        for st in RUNNING.values():
            taken.update(st["ports"].values())
        port_map = {}
        for name, preferred in app["ports_decl"]:
            p = alloc_port(preferred, exclude=taken)
            taken.add(p)
            port_map[name] = p
        RESERVED.update(port_map.values())

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
        with LOCK:
            RESERVED.difference_update(port_map.values())
        return False, f"Error al lanzar: {exc}"

    open_url = substitute(app["open"], port_map)
    with LOCK:
        # Ya viven en RUNNING; su exclusion pasa a derivarse de ahi.
        RESERVED.difference_update(port_map.values())
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


def pids_on_port(port):
    """Devuelve el conjunto de PIDs que tienen abierto (LISTEN/ocupado) `port`.

    Multiplataforma sin dependencias externas: parsea `netstat` en Windows y usa
    `lsof`/`ss` en Linux.
    """
    pids = set()
    try:
        port = int(port)
    except (TypeError, ValueError):
        return pids

    if os.name == "nt":
        try:
            # Sin `-p TCP`: ese filtro deja fuera las escuchas IPv6 (TCPv6), que es
            # justo como escucha Node/Vite ("localhost" -> ::1). Con el filtro, un
            # front en [::1]:5173 era invisible y "Apagar puerto" no podia matarlo.
            # Listamos todo y nos quedamos con las filas TCP de ambas familias.
            out = subprocess.run(["netstat", "-ano"],
                                 capture_output=True, text=True).stdout
        except Exception:
            return pids
        for line in out.splitlines():
            parts = line.split()
            # Formato: Proto  Local  Remote  Estado  PID  (las filas UDP no traen
            # Estado, se caen solas por el len).
            if len(parts) < 5 or parts[0].upper() != "TCP":
                continue
            local = parts[1]
            # El puerto es lo que va tras el ultimo ':' de la direccion local;
            # sirve igual para "127.0.0.1:5173" que para "[::1]:5173".
            if local.rsplit(":", 1)[-1] != str(port):
                continue
            pid = parts[-1]
            if pid.isdigit() and pid != "0":
                pids.add(pid)
    else:
        # Preferir lsof; si no esta, caer a ss.
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"],
                                 capture_output=True, text=True).stdout
            for line in out.split():
                if line.strip().isdigit():
                    pids.add(line.strip())
        except FileNotFoundError:
            try:
                out = subprocess.run(["ss", "-ltnp", f"( sport = :{port} )"],
                                     capture_output=True, text=True).stdout
                for m in re.finditer(r"pid=(\d+)", out):
                    pids.add(m.group(1))
            except Exception:
                pass
        except Exception:
            pass
    return pids


def kill_by_port(port):
    """Mata cualquier proceso que ocupe `port`. Devuelve (ok, mensaje)."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False, "Puerto invalido."
    if not (0 < port < 65536):
        return False, "El puerto debe estar entre 1 y 65535."

    pids = pids_on_port(port)
    if not pids:
        return False, f"No hay ningun proceso escuchando en el puerto {port}."

    killed, failed = [], []
    for pid in pids:
        try:
            if os.name == "nt":
                r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                (killed if r.returncode == 0 else failed).append(pid)
            else:
                import signal
                os.kill(int(pid), signal.SIGKILL)
                killed.append(pid)
        except Exception:
            failed.append(pid)

    # Si alguno de los PIDs muertos pertenece a una app lanzada por el launcher,
    # limpiar su estado para que la UI la muestre como detenida.
    _forget_apps_with_pids(killed)

    if killed and not failed:
        return True, f"Puerto {port} liberado. PID(s) detenido(s): {', '.join(killed)}."
    if killed and failed:
        return True, (f"Puerto {port}: detenidos {', '.join(killed)}; "
                      f"no se pudo con {', '.join(failed)}.")
    return False, f"No se pudo detener el/los proceso(s) en el puerto {port}: {', '.join(failed)}."


def _forget_apps_with_pids(pids):
    """Quita de RUNNING las apps cuyos procesos ya no viven tras un kill externo."""
    pidset = {str(p) for p in pids}
    with LOCK:
        for app_id in list(RUNNING.keys()):
            state = RUNNING[app_id]
            app_pids = {str(p.pid) for p in state["procs"]}
            # Si matamos alguno de sus PIDs, o ya ninguno vive, olvidarla.
            if app_pids & pidset or all(p.poll() is not None for p in state["procs"]):
                RUNNING.pop(app_id, None)
                for fh, _ in state["logs"]:
                    try:
                        fh.close()
                    except Exception:
                        pass


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
        elif parsed.path == "/api/kill-port":
            ok, res = kill_by_port(body.get("port"))
            self._json(200 if ok else 400, {"ok": ok, "result": res})
        else:
            self._json(404, {"error": "no encontrado"})


def cleanup_on_exit():
    for app_id in list(RUNNING.keys()):
        stop_app(app_id)


def main():
    port = CONFIG.get("port", 8765)
    server = None
    fallback = False
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            fallback = candidate != port
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        print("[error] no se encontro puerto libre para el launcher.")
        sys.exit(1)

    if fallback:
        print("!" * 60)
        print(f"  AVISO: el puerto {CONFIG.get('port', 8765)} estaba ocupado.")
        print(f"  Probablemente hay OTRO launcher aun corriendo. Este arranco")
        print(f"  en el puerto {port}. Cierra el launcher viejo (o su terminal)")
        print(f"  para evitar hablarle a la instancia equivocada.")
        print("!" * 60)

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
  button, select, input { font:inherit; color:var(--fg); background:var(--card2);
    border:1px solid var(--line); border-radius:8px; padding:7px 12px; cursor:pointer; }
  input { cursor:text; width:100px; }
  input:focus, select:focus { outline:none; border-color:var(--accent); }
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
    <input id="killport" type="number" min="1" max="65535" placeholder="Puerto"
           title="Puerto a liberar" />
    <button id="killbtn" class="danger" title="Detiene cualquier proceso que ocupe ese puerto">Apagar puerto</button>
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
const openLogs = new Set();  // ids de apps cuyo panel de log esta abierto

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
    // Cuando la app corre, mostrar el puerto REAL asignado a cada nombre
    // (p.ej. "PORT_WEB: 5173"); si no, solo los nombres declarados.
    let portsTxt;
    if (a.running && a.live_ports && Object.keys(a.live_ports).length)
      portsTxt = Object.entries(a.live_ports).map(([k,v]) => `${k}: ${v}`).join(", ");
    else
      portsTxt = a.ports.length ? a.ports.join(", ") : "ninguno";
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
        <button data-logs="${esc(a.id)}">${openLogs.has(a.id) ? "Ocultar logs" : "Logs"}</button>
      </div>
      <pre class="logs" id="logs-${esc(a.id)}"></pre>`;
    main.appendChild(card);
  }
  // Restaurar paneles de log que estaban abiertos (sobreviven al redibujado).
  for (const id of openLogs) loadLogs(id);
}

async function loadLogs(id) {
  const pre = document.getElementById("logs-" + id);
  if (!pre) return;
  // Mantener la posicion si el usuario subio a leer; auto-scroll solo si esta abajo.
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
  const r = await fetch("/api/logs?id=" + encodeURIComponent(id));
  const d = await r.json();
  pre.textContent = d.logs;
  pre.style.display = "block";
  if (atBottom) pre.scrollTop = pre.scrollHeight;
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

function toggleLogs(id) {
  if (openLogs.has(id)) openLogs.delete(id);
  else openLogs.add(id);
  render();
}

document.getElementById("main").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.launch) launch(b.dataset.launch, b);
  else if (b.dataset.stop) stop(b.dataset.stop);
  else if (b.dataset.logs) toggleLogs(b.dataset.logs);
});
async function killPort() {
  const inp = document.getElementById("killport");
  const btn = document.getElementById("killbtn");
  const port = parseInt(inp.value, 10);
  if (!port || port < 1 || port > 65535) {
    alert("Ingresa un puerto valido (1-65535).");
    inp.focus();
    return;
  }
  if (!confirm("Detener cualquier proceso que ocupe el puerto " + port + "?")) return;
  btn.disabled = true; btn.textContent = "Apagando...";
  try {
    const r = await fetch("/api/kill-port", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({port})});
    const d = await r.json();
    alert(d.result);
    if (d.ok) inp.value = "";
  } catch (e) {
    alert("Error: " + e);
  } finally {
    btn.disabled = false; btn.textContent = "Apagar puerto";
    await fetchApps();
  }
}

document.getElementById("killbtn").addEventListener("click", killPort);
document.getElementById("killport").addEventListener("keydown", e => {
  if (e.key === "Enter") killPort();
});
document.getElementById("sort").addEventListener("change", e => {
  sortKey = e.target.value; render();
});
document.getElementById("refresh").addEventListener("click", fetchApps);

fetchApps();
setInterval(fetchApps, 30000);  // refresca estado de apps corriendo
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
