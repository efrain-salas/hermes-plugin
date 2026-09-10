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

## Instalación o actualización del plugin

Cuando el usuario pida instalar o actualizar el plugin en producción:

1. Verificar que las pruebas pertinentes hayan pasado y que sólo se desplieguen los cambios solicitados.
2. Hacer commit y push a `origin/main`.
3. Confirmar que el commit publicado coincide con `git rev-parse HEAD`.
4. Instalar siempre desde GitHub mediante la lógica nativa de Hermes, fijando el SHA exacto:

   ```sh
   hermes plugins install https://github.com/efrain-salas/hermes-plugin.git \
     --ref <commit-sha-completo> \
     --force \
     --enable
   ```

5. Enumerar los perfiles:

   ```sh
   hermes profile list
   ```

6. Actualizar también cada perfil con una copia aislada del plugin. Por ejemplo:

   ```sh
   hermes --profile secureauthv2 plugins install \
     https://github.com/efrain-salas/hermes-plugin.git \
     --ref <commit-sha-completo> \
     --force \
     --enable
   ```

7. Provisionar todos los perfiles:

   ```sh
   hermes mobile provision
   ```

8. Reiniciar una sola vez después de instalar todos los perfiles:

   ```sh
   hermes gateway restart
   ```

9. Esperar a que el Gateway tenga un nuevo PID estable y verificar:

   ```sh
   hermes gateway status
   hermes mobile doctor --profile default
   hermes --profile secureauthv2 mobile doctor
   ```

10. Comprobar el listener móvil:

    ```sh
    curl -fsS http://192.168.1.50:8642/p/default/v1/mobile/health
    ```

    La ruta protegida de tareas programadas debe responder `401` sin autenticación, no `404`:

    ```sh
    curl -sS -o /dev/null -w '%{http_code}\n' \
      http://192.168.1.50:8642/p/default/v1/mobile/scheduled-tasks
    ```

11. Confirmar que todos los perfiles muestran la versión esperada y que sus metadatos de instalación apuntan al SHA desplegado.

## Restricciones

- No instalar desde `file://`, SCP, copias manuales ni repositorios temporales.
- No desplegar cambios sin commit o sin publicar.
- No mostrar claves, tokens ni el contenido completo de archivos `.env`.
- No instalar dependencias Python manualmente salvo que una comprobación demuestre que faltan.
- No modificar otros plugins, perfiles o servicios del servidor.
- Si aparece un error de permisos, inspeccionar primero el destino exacto. Corregir únicamente `.install-metadata.json` o el directorio de `hermes-mobile`, asignándolos al usuario `hermes` del contenedor.
- El instalador nativo es la fuente de verdad para habilitación, versión, revisión y procedencia del plugin.
