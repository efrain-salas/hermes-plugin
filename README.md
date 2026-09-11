# Hermes Mobile

`hermes-mobile` convierte un Hermes Gateway multiplexado en un backend móvil seguro. El teléfono usa
access/refresh tokens del plugin y nunca recibe `API_SERVER_KEY`. La raíz `/` sirve un portal propio,
independiente del dashboard de Hermes, para crear emparejamientos protegidos con passkey.

## Compatibilidad

- Python 3.11–3.13.
- Hermes Agent probado contra `bf53ff00a7360826ec2c9e2949533160068a8fc8`.
- `gateway.multiplex_profiles: true` y API Server de Hermes habilitado.
- El modo rápido usa la clase interna `AIAgent` de Hermes en el proceso del
  Gateway; si no está disponible cae de forma transparente al flujo completo.
- SQLite con WAL; `aiohttp`; tokens Ed25519.

## Instalación

Instala y habilita el plugin directamente desde este repositorio:

```bash
hermes plugins install efrain-salas/hermes-plugin --enable
hermes mobile provision
hermes gateway restart
```

`provision` es idempotente. Conserva settings existentes, habilita el plugin en los perfiles servidos,
genera una `API_SERVER_KEY` distinta donde falte, crea las bases/directorios con permisos restrictivos y
avisa si hace falta reiniciar. Para fijar una versión reproducible, añade `--ref <commit SHA>` a la orden
de instalación.

También puede instalarse como wheel en el mismo entorno Python que ejecuta Hermes con
`pip install './hermes-mobile[documents]'`; el extra `documents` habilita extracción de PDF.

Configuración mínima del perfil `default`:

```yaml
gateway:
  multiplex_profiles: true
  multiplex_profile_allowlist: [mujer]
plugins:
  enabled: [hermes-mobile]
  entries:
    hermes-mobile:
      settings:
        public_base_url: https://hermes.example.com
```

En producción publica el listener solo detrás de HTTPS. `provision` añade el origen público exacto a
`platforms.api_server.cors_origins`, porque el middleware de Hermes rechaza también los POST same-origin
que llegan a través del proxy si no están allowlisted. No configura comodines ni otros orígenes.

`public_base_url` debe ser el origen HTTPS exacto, sin ruta, query ni fragmento. Ese origen es también el
RP de WebAuthn: cambiar su dominio invalida el uso de las passkeys registradas para el dominio anterior.

## Portal de emparejamiento

La passkey es global para esta instalación de Hermes; no pertenece a un perfil. Tras autenticarse en
`https://hermes.example.com/`, el portal muestra de forma interactiva todos los perfiles servidos por el
Gateway multiplexado. El propietario elige uno y genera un QR de un solo uso para ese perfil.

Inicializa la primera passkey con un enlace temporal:

```bash
hermes mobile admin-init
```

El enlace caduca a los 15 minutos, solo puede consumirse una vez y lleva el secreto en el fragmento
`#setup=...`, que el navegador no envía al proxy ni al servidor. Al abrirlo, registra una passkey con
verificación de usuario (Face ID, Touch ID, huella, PIN o una llave compatible). A partir de entonces basta
abrir `/`, autenticarse, elegir el perfil y pulsar **Generar QR de emparejamiento**.

El portal usa una cookie de sesión `Secure`, `HttpOnly`, `SameSite=Strict`, comprobación de origen y token
CSRF. Los endpoints de alta y login tienen limitación de intentos. `admin-init` deja de emitir enlaces en
cuanto existe una passkey administrativa.

## Uso administrativo

```bash
hermes mobile doctor --profile default
hermes mobile pair --profile default --display-name Efraín
hermes mobile pair --profile default --json
hermes mobile devices --profile default
hermes mobile revoke-device dev_xxx --profile default
```

`pair` dibuja directamente un QR escaneable. Para automatizaciones, `--json` emite el token y la URI
en formato estructurado; `--qr` permite documentar explícitamente el formato interactivo. El secreto es
de un solo uso y expira a los diez minutos por defecto. Trátalo como una credencial temporal.

Dentro de cada perfil, un token push sólo puede pertenecer a un dispositivo activo. Si iOS vuelve a
registrar el mismo token con otro `installation_id`, la recepción push se transfiere al registro más
reciente para evitar notificaciones duplicadas. Al arrancar también se normalizan registros antiguos.

Cuando `public_base_url` está configurada, la URI incluye también el host y la app puede completar el
emparejamiento escaneando el QR sin pedir al usuario que copie una dirección. Los clientes antiguos
pueden ignorar ese parámetro adicional.

## Modelos y razonamiento

`GET /p/{profile}/v1/mobile/models` devuelve el catálogo real del proveedor
activo. Cada modelo incluye `reasoning.supported`, `reasoning.can_disable` y
los esfuerzos seleccionables (`none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`, `ultra`). `default` y `default_reasoning_effort` reflejan las
preferencias efectivas del perfil; un esfuerzo nulo significa que se usa el
valor predeterminado del proveedor.

Al crear una conversación, `model` y `reasoning_effort` fijan esos valores para
la conversación y actualizan a la vez `model.default` y
`agent.reasoning_effort` del perfil. Sólo se modifica una preferencia cuando su
campo aparece en la petición; `reasoning_effort: null` elimina la preferencia
global explícita. Los mismos campos en `PATCH /conversations/{id}` cambian
únicamente esa conversación y nunca alteran el perfil. En un `PATCH`, enviar
`reasoning_effort: null` elimina el override de la conversación.

## Modo rápido (quick)

Las consultas simples (conocimiento general o respuestas que dependen de Internet)
no necesitan el harness completo de Hermes: memoria, archivos de contexto, skills,
MCP y la revisión de fin de turno. El modo rápido ejecuta el turno con un agente
in-process reducido y sólo las tools de búsqueda web configuradas.

La app decide por turno con el campo `mode` de `POST /conversations/{id}/runs`:

```json
{
  "client_message_id": "msg-123",
  "input": [{"type": "text", "text": "¿Cuál es la capital de Francia?"}],
  "mode": "quick"
}
```

El valor por defecto es `"full"`. `"mode": "quick"`:

- Crea el run en el historial con el mismo ciclo de vida que un run normal
  (`run.queued` → `run.started` → `message.delta`/`tool.*` → `run.completed`),
  la misma SSE en `/runs/{id}/events`, el mismo `/sync`, unread y push.
- Persiste los turnos en la **misma sesión nativa**, por lo que la conversación
  aparece en `/conversations` y sus mensajes en `/messages` como cualquier otra.
  Un turno rápido puede continuarse después en modo `full` (y viceversa): el
  historial se carga desde la sesión nativa.
- Omite memoria (`MEMORY.md`/`USER.md` y proveedor externo), archivos de contexto
  (`SOUL.md`/`AGENTS.md`/`CLAUDE.md`), el índice de skills y la revisión
  automática de fin de turno.
- Sólo habilita los toolsets de `quick.toolsets` (por defecto `search`, que expone
  `web_search`). Sin MCP ni tools de terminal/archivos.
- Usa como system prompt fecha/hora, la zona horaria y el idioma del dispositivo
  emparejado, más una instrucción de respuesta concisa.
- No admite adjuntos (devuelve `400`), ni `steer` ni aprobaciones (devuelve `409`).
  `POST /runs/{id}/cancel` interrumpe el agente in-process.
- `POST /conversations/{id}/runs` y `GET /runs/{id}` devuelven `mode`, y
  `GET /capabilities` expone `quick`, de modo que la app puede distinguir los turnos
  rápidos y detectar cuándo una petición ha caído al flujo completo.
- `POST /runs/{id}/retry` conserva el modo del run original (reintentar un turno
  rápido vuelve a usar el agente in-process).
- Si un reinicio del Gateway deja un run rápido sin terminar, el reconciliador lo
  marca `failed` con `quick_orphaned`.

Configuración (bloque `quick` en `plugins.entries.hermes-mobile`):

```yaml
plugins:
  entries:
    hermes-mobile:
      settings:
        quick:
          enabled: true
          toolsets: [search]   # "web" añade web_extract
          max_iterations: 8
          timeout_seconds: 180
```

Si `quick.enabled` es `false`, una petición `mode: "quick"` cae de forma
transparente al flujo completo.

Internamente el plugin construye un `AIAgent` en el proceso del Gateway con
`skip_memory`, `skip_context_files`, `skip_background_review`, `enabled_toolsets`
acotado y `ephemeral_system_prompt`, dentro del scope del perfil multiplexado, y
reutiliza el mismo bucle de eventos del store que los runs nativos. Esto depende
de la API interna del `AIAgent` de Hermes; si esa API no está disponible en la
versión instalada, la petición cae al flujo nativo completo.

## Hub de tareas programadas

La API móvil proyecta el CRON nativo de Hermes en `/v1/mobile/scheduled-tasks`. Hermes sigue siendo
responsable de interpretar horarios, registrar jobs, reclamar ejecuciones, reintentar y guardar cada
output; el plugin sólo añade identificadores públicos, no-leídos, notificaciones y la política de copia
a conversación. Las tareas creadas por el agente desde una conversación móvil usan `deliver=local` por
defecto, por lo que Telegram nunca se hereda como destino implícito.

Todo resultado aparece en el hub. `delivery.conversation.mode` controla únicamente una copia adicional:

- `agent`: Hermes decide mediante su campo nativo `attach_to_session`.
- `hub_only`: no se copia a la conversación.
- `origin`: se copia a la conversación móvil donde se creó la tarea, cuando sigue disponible.

Un destino externo solicitado expresamente se conserva y se muestra en `delivery.external`. El primer
descubrimiento de historial existente no reproduce notificaciones ni llena conversaciones antiguas;
las ejecuciones siguen apareciendo como no leídas en el hub.

## Bandeja unificada de actividad

`GET /p/{profile}/v1/mobile/inbox` es la fuente durable para avisos que antes dependían de un
canal de mensajería: ciclo de vida del Gateway, fallos de persistencia detectados al arrancar,
respuestas fallidas, aprobaciones y resultados programados. Las respuestas normales permanecen
en su conversación y pueden generar una notificación push, pero no crean elementos en la bandeja
general. Admite `unread=true`
(pendientes), `unread=false` (leídos),
`kind=<tipo o prefijo>`, `limit` y `cursor`, y
devuelve `unread_count` global además de las acciones válidas para cada elemento.

```http
GET  /p/{profile}/v1/mobile/inbox
GET  /p/{profile}/v1/mobile/inbox/{inbox_item_id}
POST /p/{profile}/v1/mobile/inbox/{inbox_item_id}/read
POST /p/{profile}/v1/mobile/inbox/read-all
POST /p/{profile}/v1/mobile/inbox/{inbox_item_id}/conversation
POST /p/{profile}/v1/mobile/inbox/{inbox_item_id}/reply
```

La bandeja no convierte los avisos técnicos en mensajes falsos de una conversación. Un elemento
puede apuntar a su conversación de origen; si no existe, `conversation` la crea de forma estable.
`reply` hace ambas cosas en una sola operación, exige `Idempotency-Key`, acepta el mismo `input` que
un run normal y pasa al agente el contexto estructurado del aviso como instrucciones de sólo lectura.
Los valores procedentes de ejecuciones se marcan expresamente como datos no confiables.

Los cambios de bandeja también se publican en el `sync_journal`, de modo que `/sync` entrega eventos
`inbox_item.created` e `inbox_item.updated`. El push contiene `inbox_item_id` para abrir directamente
el elemento cuando procede de la bandeja, pero SQLite y `/sync` siguen siendo la fuente de verdad.

El listado evita incluir resultados extensos; `GET /inbox/{id}` devuelve el contexto completo del
elemento. Marcar un resultado programado como leído, desde la bandeja o desde el hub, mantiene ambos
estados sincronizados.

Durante el cierre del adaptador HTTP se confirma `gateway.stopping` antes de cerrar SQLite. En el
siguiente arranque se transforma en un único `gateway.restarted` con la duración de la interrupción;
un arranque sin parada previa aparece como `gateway.started`. No se afirma que una tarea fue
interrumpida si Hermes no dispone de esa evidencia. Como el API forma parte del Gateway, una caída
prolongada no puede emitir push por sí misma: el cliente debe mostrar el estado sin conexión, y una
alerta durante toda la caída requiere un monitor externo al contenedor.

Preferencias push nuevas:

- `system_lifecycle`: arranques y reinicios.
- `system_critical`: diagnósticos persistentes críticos, como un `state.db` no disponible.

Para retirar Telegram, cambia primero los cron con `deliver=telegram` a `deliver=local`. Sus outputs
seguirán apareciendo en la bandeja y en el hub programado. Sustituye también cualquier
`hermes send --to telegram` por un productor local de actividad antes de eliminar las credenciales y
variables `TELEGRAM_HOME_CHANNEL*`/`TELEGRAM_CRON_THREAD_ID`.

## Datos, backup y recuperación

- Control compartido: `~/.hermes/plugin-data/hermes-mobile/control.db`.
- Datos de perfil: `<PROFILE_HERMES_HOME>/plugin-data/hermes-mobile/profile.db`.
- Originales/extracciones: `<PROFILE_HERMES_HOME>/plugin-data/hermes-mobile/files/`.
- Claves: `~/.hermes/plugin-data/hermes-mobile/keys/` (`0600`).

Para un backup consistente, detén el Gateway o usa `sqlite3 ... '.backup ...'` para cada DB; copia también
`files/` y `keys/`. Sin las claves no pueden verificarse access tokens existentes ni descifrarse los push
tokens. Para recuperar, restaura con el Gateway parado, conserva permisos `0700` en directorios y `0600`
en DB/claves, arranca y ejecuta `hermes mobile doctor`.

Las migraciones son versionadas e idempotentes. Un fallo deja el plugin degradado; no detiene el Gateway.
No se hace downgrade automático: restaura un backup previo si vuelves a una versión antigua.

## Desarrollo y pruebas

```bash
uv sync --all-extras
uv run pytest
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
```

La segunda orden levanta un Hermes real con perfiles `default` y `mujer`, un proveedor LLM compatible
simulado y Expo Push simulado. Comprueba además el portal raíz y una ceremonia WebAuthn real hasta la
entrega de opciones de registro. Ejecuta pairing, aislamiento cruzado, conversaciones, run real, SSE,
adjuntos, sync, push, CLI, modo rápido y fallos de dependencias. El contenedor `tests` falla si el Gateway
deja de estar vivo durante las pruebas de resiliencia.

El contrato está en `openapi/hermes-mobile-v1.yaml`; el cliente Expo generado vive en
`generated/hermes-mobile-client.ts`. CI comprueba que el generador no deja cambios y que TypeScript
compila.
