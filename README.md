# Hermes Mobile

`hermes-mobile` convierte un Hermes Gateway multiplexado en un backend móvil seguro. Expone únicamente
`/p/{profile}/v1/mobile/*`; el teléfono usa access/refresh tokens del plugin y nunca recibe
`API_SERVER_KEY`.

## Compatibilidad

- Python 3.11–3.13.
- Hermes Agent probado contra `bf53ff00a7360826ec2c9e2949533160068a8fc8`.
- `gateway.multiplex_profiles: true` y API Server de Hermes habilitado.
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

En producción publica el listener solo detrás de HTTPS. CORS permanece bajo el control del API Server de
Hermes y debe estar desactivado salvo una necesidad explícita.

## Uso administrativo

```bash
hermes mobile doctor --profile default
hermes mobile pair --profile default --display-name Efraín
hermes mobile devices --profile default
hermes mobile revoke-device dev_xxx --profile default
```

El pairing imprime un secreto de un solo uso y una URI apta para QR. Expira a los diez minutos por
defecto. Trátalo como una credencial temporal.

Cuando `public_base_url` está configurada, la URI incluye también el host y la app puede completar el
emparejamiento escaneando el QR sin pedir al usuario que copie una dirección. Los clientes antiguos
pueden ignorar ese parámetro adicional.

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
simulado y Expo Push simulado. Ejecuta pairing, aislamiento cruzado, conversaciones, run real, SSE,
adjuntos, sync, push, CLI y fallos de dependencias. El contenedor `tests` falla si el Gateway deja de estar
vivo durante las pruebas de resiliencia.

El contrato está en `openapi/hermes-mobile-v1.yaml`; el cliente Expo generado vive en
`generated/hermes-mobile-client.ts`. CI comprueba que el generador no deja cambios y que TypeScript
compila.
