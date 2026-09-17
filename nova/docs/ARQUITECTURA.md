# Arquitectura

Documento de decisiones. Explica **por qué** cada pieza es la que es, no cómo se
usa; para eso está el README.

---

## 1. La decisión que condiciona todo: dos procesos

Un agente de voz en tiempo real necesita dos cosas que casi nunca conviven bien
en el mismo runtime: acceso de bajo nivel al audio y a modelos de machine
learning, y una capa gráfica con compositing por GPU y ventanas transparentes.

Node no tiene un equivalente maduro de `faster-whisper`, `webrtcvad` o `pvporcupine`.
Python no tiene una forma razonable de pintar un avatar transparente siempre
encima. Forzar cualquiera de los dos lados acaba en bindings frágiles.

Por eso: **núcleo en Python, interfaz en Electron, WebSocket local entre ambos**.

Tres consecuencias buenas que no eran el objetivo inicial:

* el núcleo se puede ejecutar solo (`python -m nova.main --text "..."`), lo que
  hace las pruebas triviales;
* si la interfaz se cuelga, el agente sigue vivo, y al revés;
* el protocolo, al ser explícito, obliga a que el reparto de responsabilidades
  sea explícito también.

El coste es un salto de proceso por evento: menos de 1 ms en localhost, ruido
frente a los 300-800 ms del resto de la cadena.

### Por qué WebSocket y no stdin/stdout

El flujo es bidireccional y asíncrono: el núcleo emite parciales de
transcripción, cambios de estado y progreso de herramientas sin que nadie los
pida. Con tuberías habría que inventar un framing; WebSocket ya lo trae, más
reconexión y varios clientes a la vez (el avatar y el panel escuchan el mismo
flujo).

---

## 2. Stack

| Capa | Elección | Razón |
|---|---|---|
| Interfaz | Electron 31 + TypeScript estricto | Ventana transparente, `always-on-top`, multimonitor y `showInactive` (aparecer sin robar el foco) funcionan y están documentados en Windows 11. El renderer es web puro, así que migrar a Tauri más adelante no obliga a reescribir la interfaz. |
| Núcleo | Python 3.10+, asyncio | Es donde viven las librerías de audio y voz. Un único bucle de eventos evita candados entre micrófono, modelo y síntesis. |
| Palabra activadora | Porcupine, con alternativa local por transcripción | ~1 % de CPU en reposo, que es el requisito real de algo que corre doce horas. openWakeWord obligaría a entrenar un modelo para «Nova» antes de poder probar nada. |
| VAD | webrtcvad | C puro, decide en microsegundos, sin modelo que cargar. Se ejecuta 100 veces por segundo: aquí la simplicidad **es** rendimiento. |
| Transcripción | faster-whisper local | DeepSeek no ofrece STT. Local significa latencia estable, coste cero y audio que no sale de la máquina. |
| Modelo | DeepSeek vía SDK compatible con OpenAI | Soporta *function calling*, que es lo que permite que el modelo elija herramientas declaradas en vez de inventar comandos. |
| Voz | edge-tts, o Chatterbox Turbo local | edge-tts da voces neuronales gratis pero cada petición es un websocket contra Microsoft (~1,3 s). XTTS-v2 clona una voz concreta en la GPU, sin red. |
| Navegador | Playwright sobre CDP contra el Chrome real | Ver §6. |
| Ventanas y entrada | ctypes (`SendInput`, `EnumWindows`) + psutil + `Get-StartApps` | Ver §7. |
| Capturas | mss | ctypes puro sobre la API nativa: ~15 ms en 1080p, recorta regiones sin capturar el escritorio entero y guarda PNG sin necesitar Pillow. |
| Archivos | Recorrido acotado de la biblioteca estándar | Ver §9. Un índice propio se queda obsoleto y duplica lo que el sistema ya sabe. |
| Avatar | Retrato PNG sobre canvas 2D, con skin procedural alternativo | Ver §8. |

### Por qué Electron y no Tauri

Tauri consume bastante menos memoria y era el candidato natural para algo
residente. Pierde por dos razones concretas: la combinación de transparencia,
`always-on-top` y aparecer sin robar el foco tiene menos kilometraje en Windows
que en Electron, y el núcleo Python ya carga con los modelos, de modo que los
~120 MB del shell no son el cuello de botella. La decisión es reversible sin
coste: el renderer no usa ninguna API de Node.

### Por qué la transcripción es local y el modelo no

Son costes distintos. El audio se transcribe muchas veces por minuto y es el dato
más sensible que maneja el sistema; el modelo se invoca una vez por turno y
recibe texto ya filtrado. Local donde el volumen y la sensibilidad son altos,
nube donde hace falta capacidad de razonamiento.

---

## 3. Máquina de estados

Ocho estados con transiciones declaradas en `state.py`. Cualquier salto no
previsto se registra o lanza excepción en modo estricto.

```
IDLE ──palabra activadora──► WAKE_WORD_DETECTED ──► LISTENING
LISTENING ──fin de voz──► THINKING ──herramientas──► EXECUTING ──► SPEAKING
SPEAKING ──► LISTENING ──silencio──► SLEEPING ──► IDLE
SPEAKING + voz del usuario ──► corta la voz ──► LISTENING     (interrupción)
THINKING/EXECUTING + «para» ──► corta la tarea ──► LISTENING
cualquiera + «descansa» ──► corta todo ──► despedida ──► SLEEPING ──► IDLE
cualquiera ──► ERROR ──► IDLE
```

Dos órdenes no pasan por el modelo (`agent/dismissal.py`): «descansa» y
«para». Son las dos que tienen que obedecerse al instante, y el modelo tardaba
dos segundos en contestar «descansa» con una despedida de cinco. Mientras Nova
piensa o ejecuta, el micrófono sigue abierto con el mismo filtro de volumen y
duración que la interrupción, pero lo oído sólo cuenta si es una de esas dos:
cualquier otra cosa se ignora y la tarea sigue, porque cortar una cadena de
herramientas a medias por una conversación ajena sería peor que no oír.

Declarar las transiciones no es burocracia: el fallo más caro de un agente de voz
es quedarse escuchando para siempre porque un componente de audio falló en
silencio. Con la tabla, ese bug aparece como un aviso en el registro en lugar de
como un micrófono abierto toda la tarde.

El avatar no tiene estado propio. Sólo refleja el que publica el núcleo, así que
no puede desincronizarse de lo que el agente cree estar haciendo.

---

## 4. El presupuesto de latencia

La sensación de tiempo real se gana o se pierde aquí. Estos números están
**medidos**, no estimados, en un Ryzen 7 7445HS con RTX 3050, desde que dejas de
hablar hasta que oyes la primera sílaba:

| Tramo | Coste | Cómo se ataca |
|---|---|---|
| Fin de voz detectado | 800 ms | Histéresis del VAD. Con 450 cortaba a quien habla despacio ("¿puedes abrir Discord?" llegaba como "…abrir Discord"). Es el ajuste más personal de todos: `vad.end_ms` |
| Transcripción final | 220-430 ms | Whisper `small` en float16 **sobre la GPU**. En CPU el mismo modelo tarda 1,5-2,2 s |
| Modelo (con 38 herramientas) | 1,3 s, de los que la voz espera ~la mitad | Es el término dominante y es red: DeepSeek desde Perú. La respuesta llega en streaming y la voz arranca en cuanto la línea `EN:` está completa; el subtítulo `ES:` sigue llegando mientras ya suena |
| Ejecución | 50-2000 ms | Depende de la acción |
| Primera sílaba de voz | 1,3 s (edge) / **1,2-2 s** (Chatterbox) | edge-tts paga un websocket nuevo por petición. XTTS emite en streaming desde la MISMA pasada del modelo (`inference_stream`): el audio empieza tras 20 tokens en vez de al final, con idéntica voz (parecido 0,591 vs 0,590 entero). Antes, esperando la síntesis entera, eran 1,65 s de media y hasta 2,8 s |
| **Total percibido** | **~2-2,5 s** | medido en turno completo con la voz clonada; antes 2,6-4 s |

Lo que sale de esta tabla:

1. **Whisper va a la GPU si puede.** Su coste no depende de lo que dure la frase
   —siempre rellena su ventana a 30 s—, así que en CPU un «hola» cuesta lo mismo
   que una orden entera: 1,7 s. Es la diferencia entre inmediato y no serlo. Si
   no hay GPU se baja de modelo (`stt.cpu_model`) en vez de aceptar esa espera.
2. **El micrófono se abre una sola vez, al arrancar.** Abrir un dispositivo de
   audio en Windows cuesta 100-300 ms y se notaría en cada activación.
3. **Todo lo caro se calienta en paralelo al arrancar**: el modelo de Whisper, el
   detector de palabra activadora y la conexión con la API. La voz clonada es la
   excepción: tarda ~70 s y se carga *sin* bloquear, porque hasta que el usuario
   no habla no hace falta. Esperarla dejaba a Nova sorda durante minuto y medio
   por algo que no se usa hasta el final del turno. Y el calentamiento de Whisper usa el mismo `beam_size` y una
   duración realista: con ceros, los núcleos del decodificador se compilaban en
   la primera frase de verdad y esa costaba 2 s en vez de 0,3.
4. **La voz clonada es Chatterbox Turbo, y cada oración es una toma entera.**
   Se midió contra XTTS-v2 y F5-TTS con la misma referencia de Corin, 12
   frases × 3 tomas y los mismos jueces (Whisper medium para entender, un
   codificador de hablante independiente para el parecido contra las líneas
   reales): Chatterbox Turbo se entiende 0,991 con 0 tomas malas de 36 y 0
   desbocadas; XTTS 0,88 con 5-7 malas y 1 desbocada; F5 0,87 con 7 malas,
   todas en frases cortas. Parecido: XTTS 0,64, Chatterbox 0,61, F5 0,60
   (techo 0,68). Velocidad en la RTX 3050: XTTS rtf 0,44, Chatterbox Turbo
   0,55 (el normal 2,4 y F5 1,6-2,5, sin streaming: inutilizables aquí).
   Con XTTS hacía falta vigilar cada toma con Whisper y repetir; con
   Chatterbox no. Su decodificador no es causal, así que no se decodifica por
   prefijos: cada oración se genera entera (la primera de hasta 80 letras)
   y las siguientes mientras suena la anterior. Un planificador calcula el
   retraso mínimo antes de empezar para que ninguna oración llegue tarde
   (`ChatterboxEngine._initial_delay`). La referencia se prepara una vez
   (5 s) y el modelo usa sólo sus primeros 10 s.
5. **La voz no espera al modelo.** El modelo escribe primero la línea hablada y
   luego el subtítulo. Con la respuesta en streaming, la síntesis arranca al
   cerrarse la primera; el subtítulo aparece en pantalla cuando llega, con la
   voz ya sonando. Si el modelo va a recibir el aviso de «afirmas una acción
   que no hiciste», no se adelanta nada: lo adelantado tiene que ser lo que se
   diga.
6. **El reproductor mide la velocidad real y acumula colchón sólo si hace
   falta.** La generación va limitada por la CPU (~20-25 ms por token en el
   bucle de Python; calentar la GPU no cambia el rtf: 0,57 fría, 0,58 a
   1,6 GHz), así que cualquier proceso que compita por la CPU la frena y a
   veces la deja por debajo del tiempo real: el reproductor se quedaba sin
   audio y la voz se cortaba y seguía. Ahora se mide la velocidad desde el
   primer token (no desde que se pidió la frase: eso incluía 0,5 s de
   arranque y hacía creer que iba lenta) y sólo se espera el búfer que falte.
   Medido en la app: la voz arranca 0,6-1,1 s después de cerrarse la línea
   inglesa (antes 2,7-6 s) y los turnos duran lo que dura el audio.
7. **Las frases muy cortas se comprueban con Whisper antes de sonar.** Es el
   único sitio donde cualquier modelo tropieza («Done.», «Sure!»); un Whisper
   `base` aparte las transcribe (280 ms) y, si no dicen lo pedido, se repite
   la toma antes de que suene. Con Chatterbox Turbo no falló ninguna de 36,
   pero comprobar 0,3 s cuesta menos que oír una mala.
8. **La voz carga después de Whisper, no a la vez.** Se probó en paralelo
   (Nova escuchaba a los 17 s y tenía voz a los 35) y el núcleo moría al
   arrancar una de cada tres veces con un *access violation*: CTranslate2 y
   PyTorch inicializando CUDA desde dos hilos distintos en el mismo proceso.
   En serie, escucha a los ~20 s y tiene voz a los ~60, y no se muere. La voz
   sigue sin bloquear: Chatterbox se carga cuando Whisper ya está y mientras
   tanto el resto funciona.
9. **Los parciales de transcripción están apagados.** Eran texto en pantalla
   mientras hablas, pero costaban una decodificación entera cada 900 ms que
   competía por el mismo modelo con la transcripción final y la retrasaba ~2 s.

---

## 5. Orquestador: Intención → Plan → Herramienta → Observación

El «plan» no es una lista escrita de antemano. Es el bucle de razonamiento del
modelo con herramientas: propone una acción, recibe el resultado **real** y
decide la siguiente.

```
usuario → mensajes
  repetir hasta max_steps:
    modelo(mensajes, herramientas)
      ¿pide herramienta? → validar → ¿riesgo? → confirmar → ejecutar
                         → la observación entra en los mensajes
      ¿responde texto?   → hablar y terminar
```

Un plan fijo se rompe en cuanto el mundo no coopera: la página tarda, el tercer
resultado no existe, la app no está instalada. Con observaciones reales, el
modelo se corrige solo. Por eso un error de herramienta nunca lanza excepción:
se convierte en texto y vuelve al modelo, que suele arreglarlo en el paso
siguiente.

El tope de pasos es el cortafuegos. Sin él, un modelo confundido reintenta la
misma acción fallida hasta agotar la cuota.

**Contexto para las referencias.** Las últimas acciones ejecutadas y las pestañas
abiertas se inyectan en cada prompt. Sin eso, «ciérralo» o «abre el segundo» no
tienen a qué referirse; con eso, el modelo lo resuelve sin lógica especial.

---

## 6. Navegador

`playwright.chromium.launch()` daría un navegador estéril: sin sesiones
iniciadas, sin extensiones, en una ventana distinta de la que estás mirando.
Nada de eso sirve para «busca vuelos y abre los tres primeros».

La solución es lanzar el Chrome real del sistema con `--remote-debugging-port` y
conectarse por CDP. Con un matiz: Chrome no abre el puerto de depuración sobre un
perfil que ya está en uso, así que Nova usa un perfil propio en
`%APPDATA%/Nova/chrome-profile`. Inicias sesión una vez ahí y queda guardado.

**Nunca coordenadas X/Y.** La interacción va por rol accesible, etiqueta, texto
visible y selector CSS, en ese orden. Las coordenadas se rompen con cualquier
cambio de resolución, zoom o layout; el árbol de accesibilidad sobrevive a los
rediseños.

---

## 7. Sistema operativo

Todo lo específico de Windows está detrás de `OsAdapter`. Añadir macOS es
escribir una clase y registrarla en `build_os_adapter()`; ni las herramientas ni
el orquestador se enteran.

Tres detalles que cuestan tiempo descubrir:

* **`Get-StartApps` + `shell:AppsFolder`** lanza tanto aplicaciones Win32 como
  UWP sin conocer la ruta del ejecutable. Buscar `.exe` por el disco falla con
  todo lo instalado desde la Microsoft Store.
* **Cerrar manda `WM_CLOSE` primero** y sólo mata el proceso si sigue vivo pasado
  el plazo. Matar directamente pierde el trabajo sin guardar del usuario.
* **`SetForegroundWindow` no funciona sin más** desde un proceso en segundo
  plano: Windows lo bloquea. Hay que enganchar el input del hilo en primer plano
  con `AttachThreadInput`.
* **El teclado se simula con `SendInput` por ctypes**, no con pyautogui. Son unas
  120 líneas de estructuras, evita arrastrar Pillow y pyscreeze, y sobre todo
  permite escribir con `KEYEVENTF_UNICODE`: así las tildes, las eñes y los emojis
  llegan bien a cualquier aplicación sin depender de la distribución de teclado
  activa, que es justo donde fallan las alternativas.
* **Copiar un archivo al portapapeles no es copiar su contenido.** `Set-Clipboard
  -Path` deja un `CF_HDROP`, exactamente igual que copiar desde el Explorador, y
  eso es lo que hace que pegar en Discord adjunte el archivo con su nombre
  original en vez de incrustar un mapa de bits anónimo. La variante de mapa de
  bits existe aparte, para pegar dentro de Paint o Word.
* **Las carpetas del usuario se preguntan, no se construyen.** `C:\Users\<tú>\
  Documents` puede estar redirigida a OneDrive; escribir esa ruta a mano es el
  error clásico que hace que el agente «no encuentre» archivos que están ahí.

---

## 8. Avatar

El avatar es un **retrato fijo** del personaje, un PNG con transparencia que no
se deforma ni cambia de expresión. Todo el lenguaje de estado se cuenta
*alrededor* de la figura, nunca sobre ella:

* un halo de color detrás, con el mismo código de color de los ocho estados
  (verde escuchando, violeta pensando, ámbar ejecutando, rojo error), que se lee
  a un metro sin necesidad de texto;
* un anillo bajo los pies que respira con la amplitud del micrófono;
* una flotación vertical de 3 px, lo justo para que no parezca una captura
  pegada al escritorio.

Se dibuja en canvas 2D y no con CSS por una razón concreta: la sombra de canvas
sigue el canal alfa del PNG, así que el resplandor abraza la silueta del
personaje. Con un `filter: drop-shadow` sobre un `<img>` el resultado es
aceptable, pero cualquier otro efecto que toque el fondo genera un halo
rectangular que delata la caja de la imagen sobre una ventana transparente.

Sigue existiendo el skin procedural anterior —la apertura óptica— como
alternativa sin assets, y se elige desde los ajustes. Ambos cumplen el mismo
contrato de cuatro métodos: `setState`, `setLevel`, `start`, `stop`. Sustituir
cualquiera de los dos por Rive o Live2D es escribir otra clase con esa firma;
ningún otro archivo del sistema toca el canvas.

**Dónde aparece.** La ventana se ancla a una esquina del *área de trabajo*, no de
la pantalla, para no quedar nunca debajo de la barra de tareas. Por defecto abajo
a la derecha; la esquina se elige desde el menú de la bandeja. Si arrastras el
avatar, esas coordenadas mandan hasta que vuelvas a elegir esquina. Al cambiar de
resolución o desconectar un monitor, unas coordenadas absolutas guardadas
apuntarían fuera de la pantalla, así que en ese caso vuelve solo a su esquina.

Aparece con `showInactive` y no con `show`: no debe robarte el foco mientras
escribes, que es el defecto que convierte a un asistente flotante en una molestia.

---

## 9. Archivos, pantalla y control directo

Las tres capacidades que convierten a Nova en algo que «toma el control de la
laptop» en vez de un lanzador de aplicaciones. Son también las tres con más
formas de salir mal, así que cada una tiene un límite estructural, no un aviso.

### Archivos: alcance por lista blanca

Se recorre el disco, no se indexa. Un índice propio se queda obsoleto, hay que
mantenerlo y duplica lo que el sistema ya sabe; un recorrido acotado —carpetas
del usuario, profundidad máxima, presupuesto de tiempo, carpetas de ruido
podadas— resuelve una orden hablada en decenas de milisegundos sobre un SSD.

El alcance es una **lista blanca** (`files.search_roots`): lo que no cuelga de
esas raíces no existe para el agente, aunque el modelo pida una ruta absoluta. La
comprobación vive en un único sitio, `FileSearcher.allows()`, por donde pasa toda
herramienta que toque una ruta. Escalar con `..` no sirve: se resuelve antes de
comparar.

La puntuación de nombres va por escalones —exacto, prefijo, subcadena, difuso—
porque el último escalón es el que salva los errores de transcripción: el STT
oye «corin wikes» y el archivo se llama `Corin_Wickes.webp`.

**No hay borrar, mover ni renombrar.** Un agente de voz que entiende mal un
nombre y abre el archivo equivocado es una molestia; si lo borra, es un desastre
sin deshacer. Cuando se añadan irán con riesgo alto y papelera de reciclaje.

### Pantalla

`mss` y no pyautogui: es ctypes puro sobre la API nativa, tarda ~15 ms en 1080p y
sabe recortar una región sin capturar antes el escritorio entero. Guarda PNG por
su cuenta, así que Pillow no entra en las dependencias del núcleo.

La captura de una ventana usa su rectángulo real en pantalla (vía
`DwmGetWindowAttribute`, para descontar el margen invisible de las sombras) y no
`PrintWindow`. Es menos elegante pero refleja lo que el usuario ve. Por eso la
herramienta enfoca la ventana y espera 250 ms antes de disparar: sin eso
capturaría la ventana tal y como estaba tapada.

Riesgo medio y no bajo, porque una captura puede llevarse por delante una
conversación privada o una contraseña visible.

### Control directo

Teclado, ratón y adjuntos son **riesgo alto sin excepción**, y el riesgo alto no
es configurable. Además:

* el texto de confirmación dice literalmente qué va a pasar, incluido el mensaje
  que se va a escribir y si se va a pulsar Enter — confirmar a ciegas no es
  confirmar;
* las teclas son una lista blanca por nombre: el modelo no puede enviar
  scancodes crudos;
* `NOVA_NO_INPUT=1` desactiva la inyección de entrada sin tocar la
  configuración, y con `control.enabled: false` las herramientas ni siquiera se
  registran, así que el modelo no las ve en el esquema;
* `Ctrl+Shift+X` aborta en cualquier momento, y mover el ratón o teclear tú mismo
  gana la carrera porque estas herramientas no bloquean la entrada real.

**Por qué `send_file_to_app` es una herramienta y no cuatro llamadas.** Adjuntar
una imagen en Discord es copiar → enfocar → pegar → escribir → enviar, y entre
pegar y escribir hay que esperar a que la aplicación procese el adjunto. Dejar
esa coreografía en manos del modelo significa que a veces el mensaje se envía
antes de que la imagen termine de adjuntarse. Enviar un mensaje no se deshace, así
que los tiempos viven en el código y no en el prompt. `send` es `false` por
defecto: adjuntar y enviar son decisiones distintas.

**`scroll_window` es rueda de ratón, no teclas.** `PageDown` va a la ventana
enfocada y a lo que dentro de ella tenga el foco (un cuadro de texto, no la
lista); la rueda va a lo que hay bajo el cursor, así que la herramienta lleva
el cursor al centro de la ventana y manda una muesca por evento (hay programas
que cuentan eventos y no unidades). Después de desplazar, la última lectura de
pantalla caduca: `click_item` obliga a releer, porque las líneas ya no están
donde estaban.

**`click_at` existe pero el prompt empuja a no usarlo.** Las coordenadas se rompen
al cambiar de resolución, de escala o de tema. El navegador se controla por rol
accesible y las aplicaciones por ventana y atajos de teclado; el clic por píxeles
es el último recurso.

---

## 10. Seguridad

El modelo nunca ejecuta nada. Propone `{nombre, argumentos}` y el registro
comprueba, en este orden:

1. la herramienta existe y está en la lista permitida;
2. los argumentos validan contra su JSON Schema;
3. el nivel de riesgo permite ejecución automática;
4. la ejecución respeta un tiempo límite;
5. el resultado queda auditado en JSON.

| Riesgo | Ejemplos | Comportamiento |
|---|---|---|
| Bajo | abrir apps, buscar, cambiar de pestaña, volumen, **buscar archivos** | Automático |
| Medio | cerrar apps con trabajo abierto, formularios, portapapeles, **abrir y leer archivos, capturar pantalla** | Confirmación, configurable |
| Alto | enviar, pagar, cualquier acción irreversible, **teclado, ratón y adjuntar archivos en otras apps** | **Confirmación siempre, no configurable** |

Que el riesgo alto no sea configurable es deliberado. Un ajuste que se puede
desactivar acaba desactivado, normalmente la tarde antes del accidente.

Dos decisiones más, pequeñas y con consecuencias:

* **Sin herramienta «ejecuta este comando».** Sería la forma más rápida de tener
  un agente capaz de todo, y también de que un modelo confundido borrara una
  carpeta. Cada capacidad se añade como herramienta con esquema y riesgo
  explícitos.
* **La confirmación que expira, deniega.** Un timeout no puede interpretarse
  como aprobación tácita.

---

## 11. Memoria

Tres memorias con vidas distintas, porque mezclarlas es la forma habitual de
acabar con un prompt inmanejable:

| Memoria | Dónde | Para qué |
|---|---|---|
| Conversación | RAM, ventana deslizante | Entender «ábrelo», «ahora en inglés» |
| Acciones recientes | RAM, últimas 20 | Resolver «ciérralo», «el anterior» |
| Preferencias | Disco | Navegador habitual, apps frecuentes |

**No se guarda audio nunca.** El audio se descarta en cuanto se transcribe.

Un detalle no obvio en `ConversationMemory.messages()`: al recortar la ventana
pueden quedar mensajes `tool` cuyo `assistant` con `tool_calls` ya se cayó del
búfer. La API rechaza esa secuencia, así que esos huérfanos se descartan al
construir la lista. Es el tipo de fallo que sólo aparece en conversaciones largas
y cuesta horas encontrar.

---

## 12. Estar horas encendido

* En reposo sólo corre el detector de palabra activadora: ~1 % de CPU, sin
  modelos grandes en memoria, sin una sola petición de red.
* El micrófono usa una cola acotada: si un consumidor se atasca, se pierde una
  trama en vez de crecer sin límite.
* Cada turno cancela el anterior: nada se acumula entre interacciones.
* El núcleo se reinicia solo con retroceso exponencial si muere, y la interfaz
  se reconecta sola. Una caída de red interrumpe un turno, no la sesión.
* Tres cortafuegos independientes: tope de pasos en el orquestador, tiempo límite
  por herramienta, tiempo límite en las confirmaciones.

---

## 13. Lo que no se construyó, y por qué

* **Borrar, mover y renombrar archivos.** Leer y copiar sí están (§9); modificar
  el disco no. Es la capacidad cuyo fallo no se deshace, y entra con papelera de
  reciclaje y riesgo alto, no antes.
* **Visión del escritorio (OCR).** Localizar un botón por su texto haría a
  `click_at` fiable en aplicaciones sin atajos de teclado. Cuesta latencia y un
  modelo más; entra cuando aparezca un caso que los atajos no resuelvan.
* **Streaming del modelo.** Reduciría unos cientos de milisegundos, pero
  complica bastante el troceado del TTS y la cancelación. Hay cosas más baratas
  que optimizar primero.
* **Interpretar las capturas con un modelo de visión.** Nova las toma y las
  mueve, pero no las «mira»: en el navegador, el árbol de accesibilidad cubre
  casi todo a una fracción del coste y la latencia.
* **Resumen automático de la conversación.** Hace falta a partir de sesiones
  largas; con la ventana deslizante actual todavía no duele.
