# App Launcher

Un **único proceso** para centralizar el lanzamiento de todas tus apps. Escanea
tus carpetas buscando un archivo `launcher.json`, muestra las apps en una web
local y las lanza (o detiene) con un click, asignando puertos libres
automáticamente para que **nunca choquen**.

- **Cero instalación**: solo Python (librería estándar). Sin `pip install`, sin
  `node_modules`.
- **Rutas relativas**: cada app se registra con un `launcher.json` dentro de su
  propia carpeta; el registro viaja con la app y los comandos corren con `cwd` en
  esa carpeta. Si mueves las apps a otro computador, siguen funcionando.
- **Multiplataforma**: Windows y Linux.

## Requisitos

- Python 3.8+ (verifica con `python --version`).

## Correr

Desde `C:\Desarrollo\app-launcher`:

```powershell
python launcher.py
```

Abre el navegador automáticamente en `http://127.0.0.1:8765`. `Ctrl+C` detiene el
launcher y todas las apps que hayas lanzado.

## Cómo registrar una app

Crea un archivo `launcher.json` en la **carpeta raíz de la app**. Ejemplo real
(backend Python + frontend Vite que necesita saber el puerto del backend):

```json
{
  "name": "Image Text Finder",
  "description": "OCR sobre imágenes",
  "processes": [
    {
      "name": "backend",
      "cmd": ".venv\\Scripts\\python.exe -m itf.api --host 127.0.0.1 --port {PORT_API}"
    },
    {
      "name": "frontend",
      "cmd": "npm run dev -- --port {PORT_WEB} --strictPort",
      "env": { "ITF_API_URL": "http://127.0.0.1:{PORT_API}" }
    }
  ],
  "ports": ["PORT_API", "PORT_WEB"],
  "open": "http://localhost:{PORT_WEB}"
}
```

### Campos del manifiesto

| Campo | Obligatorio | Descripción |
|---|---|---|
| `name` | no | Nombre visible. Por defecto, el nombre de la carpeta. |
| `description` | no | Texto corto. |
| `processes` | **sí** | Lista (array) de procesos a lanzar, **en orden**. |
| `processes[].name` | no | Etiqueta (usada para el archivo de log). |
| `processes[].cmd` | **sí** | Comando completo. Corre en un shell (`shell=True`). |
| `processes[].cwd` | no | Subcarpeta (relativa a la app) donde correr el comando. Por defecto la carpeta de la app. Útil para un frontend en `web/`. |
| `processes[].env` | no | Variables de entorno extra para ese proceso. |
| `ports` | no | Nombres de puerto a asignar. Cada uno se resuelve a un puerto **libre**. |
| `open` | no | URL a abrir cuando la app arranca. |
| `registered_at` | no | Fecha ISO de ingreso (para ordenar). Si falta, se usa la fecha del archivo. |

### Puertos: cómo evitar colisiones

- Declara los puertos que la app necesita en `ports`, con un nombre cada uno
  (`"PORT_API"`, `"PORT_WEB"`, …).
- Al lanzar, el launcher busca puertos **libres reales** y sustituye
  `{NOMBRE}` en `cmd`, en `env` y en `open`.
- El **mismo nombre se resuelve una sola vez por lanzamiento**, así que si el
  frontend referencia `{PORT_API}`, recibe exactamente el puerto que se le dio al
  backend. Puedes lanzar varias apps a la vez sin que choquen.
- Opcional: puedes pedir un puerto preferido:
  `"ports": [{ "name": "PORT_WEB", "preferred": 5173 }]`. Si está ocupado, se usa
  otro libre.

### Registro automático con Claude

Como registrarse es solo **escribir un JSON**, no hay módulo ni SDK que instalar.
Cuando estés trabajando en una app, pégale a Claude el prompt de abajo. Es
detallado a propósito: sin un esquema exacto, Claude tiende a **inventar campos**
(`services`, `command`, `${VAR}`, `placeholders`, `dependsOn`…) que este launcher
**no** entiende, y la app no lanza.

> **Registra esta app en el App Launcher.**
>
> 1. Lee el `README.md` de esta app (y `package.json` / `pyproject.toml` si los
>    hay) para averiguar los **comandos exactos** que levantan la app: backend,
>    frontend, o lo que aplique; en qué **carpeta** corre cada uno; qué
>    **puertos** usa; y qué **variables de entorno** necesita.
> 2. Crea un archivo `launcher.json` en la **raíz de esta app** con EXACTAMENTE
>    este esquema. El launcher **solo** entiende estos campos — no inventes otros:
>
>    ```json
>    {
>      "name": "Nombre visible de la app",
>      "description": "Una línea de qué hace.",
>      "processes": [
>        {
>          "name": "backend",
>          "cmd": "<comando exacto del backend, con {PLACEHOLDER} de puerto>"
>        },
>        {
>          "name": "frontend",
>          "cwd": "<subcarpeta si el frontend vive en otra carpeta, p.ej. web>",
>          "cmd": "<comando exacto del frontend, con {PLACEHOLDER} de puerto>",
>          "env": { "VAR_QUE_APUNTA_AL_BACKEND": "http://127.0.0.1:{PORT_API}" }
>        }
>      ],
>      "ports": [
>        { "name": "PORT_API", "preferred": 8000 },
>        { "name": "PORT_WEB", "preferred": 5173 }
>      ],
>      "open": "http://localhost:{PORT_WEB}"
>    }
>    ```
>
> **Reglas obligatorias del formato:**
> - `processes` es un **array**, no un objeto. `cmd` (no `command`). El launcher
>   ejecuta cada `cmd` en orden.
> - Los puertos se escriben con **llaves**: `{PORT_API}`, `{PORT_WEB}`. **NO** uses
>   `${VAR}` ni `%VAR%`. Cada nombre entre llaves debe estar declarado en `ports`.
> - Declara cada puerto en `ports`. Para respetar el puerto que la app espera por
>   defecto, usa `{ "name": "PORT_WEB", "preferred": 5173 }`. El launcher usará ese
>   si está libre, o buscará otro; el **mismo** `{PORT_API}` referenciado en el
>   frontend recibe el puerto real del backend.
> - Si un proceso corre en una subcarpeta (frontend en `web/`, etc.), ponle
>   `"cwd": "web"`. Los comandos NO deben usar rutas absolutas.
> - **NO** uses `services`, `placeholders`, `dependsOn`, `command`, `url`, `port`
>   ni ningún otro campo: el launcher los ignora y la app no arrancará.
> - **No lances ni instales nada**: solo escribe el `launcher.json` y muéstramelo.
>
> Al terminar, verifica que el JSON es válido y que cada `{PLACEHOLDER}` usado en
> `cmd`/`env`/`open` aparece en `ports`.

Puedes guardar este prompt en el `CLAUDE.md` de cada app para tenerlo a mano.

## Configuración (`config.json`)

Se crea solo la primera vez. Valores por defecto:

```json
{
  "roots": ["..", "examples"],
  "port": 8765,
  "port_range": [3000, 9999],
  "open_browser": true
}
```

- `roots`: carpetas donde buscar apps (relativas a `launcher.py` o absolutas).
  `".."` es la carpeta padre (p.ej. `C:\Desarrollo`), donde viven tus apps. El
  launcher revisa las **subcarpetas inmediatas** de cada root buscando
  `launcher.json`.
- `port`: puerto del launcher (si está ocupado, prueba los siguientes).
- `port_range`: rango de puertos que puede asignar a las apps.

## Ordenar

En la web puedes ordenar por **fecha de ingreso** (recientes primero, por
defecto) o por **nombre** (A-Z).

## Logs

Cada proceso escribe su salida en `logs/<app>__<proceso>.log`. El botón **Logs**
de cada tarjeta muestra las últimas líneas (útil si una app no arranca).

## Estructura

```
app-launcher/
  launcher.py          # toda la aplicación (un archivo)
  config.json          # configuración (autogenerada)
  logs/                # salida de cada app (ignorado por git)
  examples/
    hello-app/         # app demo para probar el launcher
      launcher.json
      server.py
```
