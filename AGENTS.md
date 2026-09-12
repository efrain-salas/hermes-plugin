# Hermes Mobile

## Servidor de producción

- Host SSH: `efrain@192.168.1.50`
- Repositorio: `https://github.com/efrain-salas/hermes-plugin.git`
- Rama de producción: `main`
- Hermes se ejecuta en Docker mediante el contenedor `hermes-agent`.
- Usar siempre la CLI oficial disponible en `/usr/local/bin/hermes`.
- Datos persistentes: `/home/efrain/.hermes` en el host, montados como `/opt/data` en el contenedor.
- Perfiles conocidos actualmente: `default` y `secureauthv2`. Enumerarlos siempre antes de actualizar.
- El puerto 8642 está publicado en `192.168.1.50`, no necesariamente en `127.0.0.1`.

## Ejecución de comandos en el contenedor (regla de oro)

- Usar SIEMPRE el wrapper `/usr/local/bin/hermes`, que ya ejecuta
  `sudo docker exec -i -u hermes hermes-agent hermes ...`.
- PROHIBIDO ejecutar Hermes como root dentro del contenedor. En particular:
  - `sudo docker exec hermes-agent hermes ...` (sin `-u hermes`).
  - `sudo docker exec -it hermes-agent` y lanzar `hermes` desde ahí.
  - `sudo docker run ... -v /home/efrain/.hermes:/opt/data ... hermes ...`.
- Cualquier proceso de Hermes que corra como root puede reescribir archivos de
  `/opt/data` (sobre todo `auth.json`) con propietario `root:root`, dejándolos
  ilegibles para el usuario `hermes` (uid 10000) y rompiendo todos los turnos
  de conversación.

## Verificación de permisos (obligatoria antes y después de cada despliegue)

Comprobar que las rutas críticas sean legibles por `hermes` y que `auth.json`
pertenezca a `hermes`:

```sh
sudo docker exec -u hermes hermes-agent sh -c '
  for f in /opt/data/auth.json /opt/data/config.yaml /opt/data/.env; do
    test -r "$f" && echo "OK    $f" || echo "FALLO $f (no legible por hermes)"
  done
  stat -c "propietario=%U:%G modo=%a %n" /opt/data/auth.json
'
```

Esperado: `OK` en las tres rutas y `propietario=hermes:hermes modo=600` en
`auth.json`.

Auditar los archivos de estado que Hermes debe leer o escribir (los de
`update/`, los respaldos `config.yaml.before-hermes-mobile-*` y las copias
bajo `projects/` sí pueden pertenecer a root y no afectan al plugin):

```sh
sudo find /home/efrain/.hermes/auth.json \
  /home/efrain/.hermes/.env \
  /home/efrain/.hermes/config.yaml \
  /home/efrain/.hermes/plugins \
  /home/efrain/.hermes/plugin-data \
  /home/efrain/.hermes/profiles \
  -user root -ls
```

Debe salir vacío. Si aparece algo crítico (`auth.json`, `.env`, `config.yaml`,
`plugins/`, `plugin-data/`, `profiles/`), corregirlo y reiniciar el gateway:

```sh
sudo docker exec -u root hermes-agent sh -c '
  chown -R hermes:hermes /opt/data/<ruta>
  chmod 600 /opt/data/auth.json
'
hermes gateway restart
```

## Diagnóstico: fallan los turnos de conversación

Síntoma en `hermes logs errors --since 1h`:
`PermissionError: [Errno 13] Permission denied: '/opt/data/auth.json'`,
`Quick run ... crashed` o
`Provider authentication failed for run=...`.

Causa: un proceso de Hermes corrió como root y recreó `auth.json` como
`root:root` (comprobable con `stat`: fecha de creación reciente y
`Uid: ( 0/ root)`).

Remedio:

```sh
sudo docker exec -u root hermes-agent chown hermes:hermes /opt/data/auth.json
sudo docker exec -u root hermes-agent chmod 600 /opt/data/auth.json
hermes gateway restart
```

Después, confirmar que hay `0` errores nuevos:

```sh
hermes logs errors -n 50 --since 5m | grep -c 'Permission denied'
```

## Instalación o actualización del plugin

Cuando el usuario pida instalar o actualizar el plugin en producción:

1. Verificar que las pruebas pertinentes hayan pasado y que sólo se desplieguen los cambios solicitados.
2. Ejecutar la verificación de permisos de `/opt/data` descrita arriba (pre-flight). Si algo crítico es de `root`, corregirlo antes de continuar.
3. Hacer commit y push a `origin/main`.
4. Confirmar que el commit publicado coincide con `git rev-parse HEAD`.
5. Instalar siempre desde GitHub mediante la lógica nativa de Hermes, fijando el SHA exacto:

   ```sh
   hermes plugins install https://github.com/efrain-salas/hermes-plugin.git \
     --ref <commit-sha-completo> \
     --force \
     --enable
   ```

6. Enumerar los perfiles:

   ```sh
   hermes profile list
   ```

7. Actualizar también cada perfil con una copia aislada del plugin. Por ejemplo:

   ```sh
   hermes --profile secureauthv2 plugins install \
     https://github.com/efrain-salas/hermes-plugin.git \
     --ref <commit-sha-completo> \
     --force \
     --enable
   ```

8. Provisionar todos los perfiles:

   ```sh
   hermes mobile provision
   ```

9. Reiniciar una sola vez después de instalar todos los perfiles:

   ```sh
   hermes gateway restart
   ```

10. Esperar a que el Gateway tenga un nuevo PID estable y verificar:

    ```sh
    hermes gateway status
    hermes mobile doctor --profile default
    hermes --profile secureauthv2 mobile doctor
    ```

11. Comprobar el listener móvil:

    ```sh
    curl -fsS http://192.168.1.50:8642/p/default/v1/mobile/health
    ```

    La ruta protegida de tareas programadas debe responder `401` sin autenticación, no `404`:

    ```sh
    curl -sS -o /dev/null -w '%{http_code}\n' \
      http://192.168.1.50:8642/p/default/v1/mobile/scheduled-tasks
    ```

12. Confirmar que todos los perfiles muestran la versión esperada y que sus metadatos de instalación apuntan al SHA desplegado.

13. Repetir la verificación de permisos de `/opt/data` (post-flight) y confirmar que no quedó ningún `auth.json` propiedad de `root`, ni errores nuevos:

    ```sh
    sudo docker exec -u hermes hermes-agent sh -c '
      for f in /opt/data/auth.json /opt/data/config.yaml /opt/data/.env; do
        test -r "$f" && echo "OK    $f" || echo "FALLO $f (no legible por hermes)"
      done
      stat -c "propietario=%U:%G modo=%a %n" /opt/data/auth.json
    '
    hermes logs errors -n 50 --since 5m | grep -c 'Permission denied'
    ```

    Debe mostrar `propietario=hermes:hermes`, `OK` en las tres rutas y `0`
    errores de permisos. Si algo es de `root`, corregirlo antes de dar el
    despliegue por terminado.

## Restricciones

- No instalar desde `file://`, SCP, copias manuales ni repositorios temporales.
- No desplegar cambios sin commit o sin publicar.
- No mostrar claves, tokens ni el contenido completo de archivos `.env`.
- No instalar dependencias Python manualmente salvo que una comprobación demuestre que faltan.
- No modificar otros plugins, perfiles o servicios del servidor.
- Si aparece un error de permisos, inspeccionar primero el destino exacto. Corregir únicamente el archivo afectado (por ejemplo `auth.json`), el `.install-metadata.json` o el directorio de `hermes-mobile`, asignándolos al usuario `hermes` del contenedor.
- Nunca lanzar Hermes como `root`: siempre a través del wrapper `/usr/local/bin/hermes`. Un `docker exec`/`docker run` como root puede recrear `auth.json` como `root:root` y romper todos los turnos de conversación.
- El instalador nativo es la fuente de verdad para habilitación, versión, revisión y procedencia del plugin.
