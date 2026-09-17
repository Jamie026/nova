# Nova

Agente de voz de escritorio en tiempo real para Windows 11. Vive en la bandeja
del sistema, escucha una palabra activadora, muestra un avatar flotante y
controla aplicaciones y el navegador mediante herramientas declaradas.

## Instalación en un solo comando

Con Python 3.10+ y Node 20+ instalados, esto lo deja **todo** listo (entorno
de Python, voz clonada si hay GPU NVIDIA, navegador, modelos ya descargados,
Electron y el `.env`). Tarda 10–20 minutos y ocupa ~8 GB con la voz:

```powershell
git clone <tu-repo> nova && cd nova
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# abre .env y pon tu DEEPSEEK_API_KEY
npm run dev
```

En macOS/Linux, `bash scripts/setup.sh`. Los dos aceptan `-SinVoz` /
`--sin-voz` (sin PyTorch ni Chatterbox, ~4 GB menos; Nova habla con edge-tts)
y `-SinModelos` / `--sin-modelos` (los modelos se bajan en el primer arranque).
Es seguro repetirlo: no reinstala lo que ya está.

La voz clonada va incluida: la referencia está en el repo
(`core/nova/tts/voices/reference.wav`) y el proveedor por defecto es `auto`
(voz clonada si está instalada, edge-tts si no), así que quien clone no tiene
que copiar ningún audio ni tocar el `.env`. Para clonar otra voz, pon un WAV
en `%APPDATA%\Nova\voices\reference.wav` (o `NOVA_TTS_REFERENCE=` en
`.env`): gana sobre la del repo. Sección 7 de `INSTRUCCIONES.txt`.

Sin tocar nada más, `npm run dev:mock` arranca todo con proveedores simulados:
la interfaz, el avatar y el flujo completo funcionan sin gastar llamadas a la API
ni cargar modelos pesados.

---

## Qué hace hoy

| | |
|---|---|
| Escucha pasiva | Detección local de palabra activadora, sin enviar audio a ningún sitio |
| Voz | Transcripción local con Whisper; habla con una voz clonada en local (Chatterbox Turbo) o con voces neuronales de red |
| Interrupción | Le hablas mientras habla y se calla en menos de 50 ms |
| Conversación | Varios turnos seguidos sin repetir la palabra activadora |
| Aplicaciones | Abrir, cerrar, enfocar, minimizar, listar ventanas |
| Navegador | Pestañas, búsquedas, clics, formularios, lectura de la página |
| Archivos | Buscar por nombre en tus carpetas, abrir, mostrar en el Explorador, copiar |
| Pantalla | Capturar la pantalla completa o una ventana concreta |
| Control directo | Escribir, pulsar atajos, hacer clic y adjuntar archivos en cualquier app |
| Seguridad | Tres niveles de riesgo, confirmación obligatoria en el nivel alto |
| Memoria | Conversación, acciones recientes y preferencias persistentes |

Ejemplo del encadenado que sale de ahí:

> «Nova, busca la imagen que se llama corin y mándamela por Discord»

`find_files(name="corin", kind="image")` → `send_file_to_app(app="Discord",
path=…, send=false)`. Nova pide confirmación antes de pegar nada, y no lo envía
salvo que se lo pidas explícitamente.

## Cómo está montado

Dos procesos que hablan por WebSocket local:

```
┌─ Electron ──────────────┐        ┌─ Python ─────────────────────┐
│  avatar flotante        │        │  micrófono → wake word → VAD │
│  panel de ajustes       │◄──ws──►│  → Whisper → orquestador     │
│  bandeja y atajos       │        │  → herramientas → voz        │
└─────────────────────────┘        └──────────────────────────────┘
```

La interfaz no decide nada: refleja el estado que publica el núcleo. Toda la
lógica del agente vive en Python.

El detalle de por qué cada pieza es la que es está en
[`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md).

## Estructura

```
nova/
├── core/                     núcleo Python
│   ├── nova/
│   │   ├── app.py            composición: qué implementación usa cada interfaz
│   │   ├── main.py           punto de entrada
│   │   ├── session.py        bucle de audio y ciclo de vida de una interacción
│   │   ├── state.py          máquina de estados
│   │   ├── config.py         configuración en tres capas
│   │   ├── agent/            orquestador, memoria, prompts
│   │   ├── audio/            micrófono, VAD, palabra activadora
│   │   ├── stt/ tts/ llm/    proveedores intercambiables + simulados
│   │   ├── tools/            registro, validación, riesgo, herramientas
│   │   ├── os_adapter/       capa de sistema operativo (Windows implementado)
│   │   ├── browser/          control de Chrome por CDP
│   │   ├── files/            búsqueda de archivos con alcance acotado
│   │   ├── capture/          capturas de pantalla
│   │   └── ipc/              servidor WebSocket
│   └── tests/
├── desktop/src/
│   ├── main/                 proceso principal de Electron
│   ├── preload/              puente aislado
│   └── renderer/
│       ├── overlay/          avatar flotante (assets/ guarda el retrato)
│       └── settings/         panel de ajustes y registro en vivo
├── scripts/
└── docs/
```

## Requisitos

* Windows 11 (el núcleo y el navegador funcionan en macOS y Linux; el control de
  ventanas todavía no)
* Python 3.10 o superior (probado con 3.13)
* Node.js 20 o superior
* Un navegador (Opera, Chrome o Edge; se elige con `browser.prefer`)
* Una clave de DeepSeek en `.env`
* Para la voz clonada: GPU NVIDIA con 6 GB y, en la práctica, 16 GB de RAM
  (con 8 GB funciona pero pagina; ver «error 1455» en `INSTRUCCIONES.txt`)
* Opcional: una clave gratuita de Picovoice para la palabra activadora ligera

## Uso

Di la palabra activadora y habla. El avatar aparece, escucha, ejecuta y responde;
después sigue escuchando unos segundos por si continúas, y vuelve solo al reposo.

* `Ctrl+Shift+Espacio` despierta a Nova sin hablar
* `Ctrl+Shift+X` interrumpe lo que esté haciendo — es el freno de mano cuando
  esté escribiendo o haciendo clic por ti
* Doble clic en el avatar abre los ajustes
* Arrastra el avatar para moverlo; recuerda dónde lo dejaste
* El menú del icono de la bandeja elige a qué esquina vuelve

El avatar sale por defecto abajo a la derecha, sólo mientras Nova está activa, y
se esconde solo cuando termina.

En los ajustes, la pestaña **Consola** manda órdenes escritas por el mismo camino
que la voz. Es la forma más rápida de probar herramientas nuevas sin hablar.

## Desarrollo

```bash
npm run dev          # interfaz + núcleo
npm run dev:mock     # todo simulado, sin API ni modelos
npm run core         # sólo el núcleo, con logs en la terminal
npm run test:core    # pruebas de los componentes críticos
npm run deps:core    # reinstala las dependencias de Python en core/.venv
npm run deps:voice   # sólo la voz clonada (PyTorch CUDA + Chatterbox)
npm run models:download  # baja por adelantado Whisper, wake word y la voz
npm run typecheck    # TypeScript sin emitir
npm run dist         # instalador Nova-Setup-<versión>.exe en release/
```

Para depurar los dos procesos a la vez desde VS Code, copia
`.vscode-launch.json.example` a `.vscode/launch.json` y usa la configuración
compuesta «Nova completo».

Una sola orden sin interfaz ni micrófono:

```bash
cd core && python -m nova.main --text "abre spotify"
```

Para trabajar sin que Nova pueda tocar el teclado ni el ratón (recomendado
mientras iteras en el prompt):

```bash
set NOVA_NO_INPUT=1          # PowerShell: $env:NOVA_NO_INPUT="1"
```

### Prueba de humo del control del escritorio

Lo único que no se puede cubrir con tests automáticos es la inyección de entrada
real. El guion, con Discord abierto:

1. `python -m nova.main --text "busca la imagen corin"` → debe devolver la ruta
2. `... --text "mándala por Discord sin enviarla"` → confirma, y la imagen debe
   quedar adjunta en la caja de texto de Discord sin enviarse
3. `... --text "captura la ventana de Discord"` → PNG en `Imágenes\Nova`

## Configuración

Las claves van en `.env` y nunca en el código. El resto de ajustes se editan en
el panel y se guardan en `%APPDATA%/Nova/settings.json`; los valores por defecto
están documentados en `core/nova/config.py`.

Los dos que conviene conocer:

* `files.search_roots` — las únicas carpetas que Nova puede leer. En blanco usa
  tus carpetas de usuario. Nada fuera de ahí existe para el agente, aunque el
  modelo se invente una ruta absoluta.
* `control.enabled` — teclado y ratón simulados. En `false`, esas herramientas ni
  siquiera se registran: el modelo no las ve.

### Cambiar el avatar

Son dos imágenes en `desktop/src/renderer/overlay/assets/`: `nova-avatar.webp`
(en reposo, escuchando, pensando) y `nova-avatar-sonriendo.webp` (mientras
habla, con fundido). Sustitúyelas por cualquier PNG o WEBP con fondo
transparente y el mismo nombre; si además quieres que cambie el icono de la
bandeja, `python scripts/make-tray-icon.py` (necesita Pillow). En los ajustes
puedes volver al avatar procedural.

## Lo que falta

En orden de utilidad:

1. Mover, renombrar y borrar archivos (a la papelera, nunca permanente)
2. Lectura de PDF y Word para `read_text_file`
3. Resumen automático de la conversación para sesiones largas
4. `MacAdapter` y `LinuxAdapter` detrás de `OsAdapter`
5. Firmar el instalador (hoy SmartScreen avisa la primera vez)
