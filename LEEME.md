# opencode-db-prune

*[English version](README.md)*

> **Esta versión está probada solo en macOS.** Ver [Plataformas](#plataformas).

Tu `opencode.db` probablemente pesa muchísimo, y lo más probable es que el 90 %
sea una sola tabla que no necesitas.

Es un script de Python de un solo archivo, sin dependencias, que recupera ese
espacio **sin borrar ni una sesión, ni un mensaje, ni un archivo**.

```
database : /Users/tu-usuario/.local/share/opencode/opencode.db
size     : 39.37 GB

event table: 953,214 rows, 35.42 GB
  message.updated.1            228,444    33.87 GB
  message.part.updated.1       659,569     1.51 GB
  session.updated.1             63,095     0.05 GB

would prune (all but the 5 newest sessions): 949,404 rows, 35.42 GB
estimated size afterwards: ~3.96 GB
```

## Qué está pasando en realidad

OpenCode guarda **una copia completa del mensaje cada vez que lo actualiza**.
Mientras una respuesta se va escribiendo, eso ocurre muchas veces, y cada uno de
esos eventos arrastra otra vez el texto acumulado entero — no una diferencia.

Medido en una instalación real (unas 2.100 sesiones, semanas de uso intensivo
con agentes):

| | |
|---|---|
| Mensaje final medio (tabla `message`) | 35,6 KB |
| Evento `message.updated` medio | **155,6 KB** — 4,4× más grande |
| Mensajes reales | 59.176 |
| Eventos de actualización de esos mensajes | **228.444**, en total 33,87 GB |

O sea que las conversaciones ocupaban unos 3 GB. Los otros 35 GB eran copias de
un texto que ya estaba guardado en otro sitio.

Tras ejecutar el script, esa base pasó de **39,37 GB a 3,15 GB** (36,23 GB
recuperados), `PRAGMA integrity_check` devolvió `ok`, y las 2.106 sesiones,
59.294 mensajes y 293.777 partes seguían ahí — comprobado releyendo una sesión
cuyos datos se anotaron antes de la limpieza y comparándolos uno a uno.

## Por qué es seguro borrarlo

**1. El contenido vive en otro sitio.** El estado definitivo de cada mensaje está
en las tablas `message` y `part`. El script lo verifica antes de tocar nada: abre
la sesión más antigua de tu base y comprueba que su texto siga siendo legible
fuera de `event`. Si no lo estuviera, se niega a ejecutarse.

**2. OpenCode borra esa tabla él mismo.** Su propia migración de esquema
`reset_v2_session_state` ejecuta:

```sql
DELETE FROM `session_context_epoch`;
DELETE FROM `session_input`;
DELETE FROM `session_message`;
DELETE FROM `event`;
DELETE FROM `event_sequence`;
DELETE FROM `workspace`;
```

Fíjate en las tablas que **no** toca: `session`, `message`, `part`. Es la propia
declaración del proyecto sobre dónde viven tus datos.

**3. Los índices dicen para qué sirve la tabla.** Son `(aggregate_id, seq)` y
`(aggregate_id, type, seq)` — hechos para responder *«dame los eventos de esta
sesión a partir del número N»*. Así es como un cliente que se reconecta se pone
al día con una sesión **que sigue en marcha**. Para una sesión terminada no tiene
lector.

El script es a propósito más conservador que lo que OpenCode se hace a sí mismo:
borra filas de `event` únicamente, deja `event_sequence` intacta para que la
numeración siga como está, y no toca las sesiones más recientes.

## Uso

```bash
python3 opencode-db-prune.py                          # solo informa, no cambia nada
python3 opencode-db-prune.py --apply                  # limpia, respaldando antes
python3 opencode-db-prune.py --apply --no-backup      # sin copia de respaldo
python3 opencode-db-prune.py --apply --keep 20        # deja intactas las 20 más recientes
python3 opencode-db-prune.py --db /ruta/a/opencode.db
```

En Windows también puedes usar el lanzador de Python:

```powershell
py opencode-db-prune.py
py opencode-db-prune.py --apply
```

**Cierra OpenCode antes.** El script comprueba si el archivo está abierto y se
niega a ejecutarse si lo está.

Sin dependencias. Python 3.8 o superior.

## Salvaguardas

- Se niega a ejecutarse si otro proceso tiene el archivo abierto.
- Se niega si el contenido de los mensajes no es legible fuera de `event`.
- Se niega si la base ya venía dañada.
- Respalda antes, salvo que pases `--no-backup` (la copia necesita tanto espacio
  libre como ocupa la base).
- Ejecuta `PRAGMA integrity_check` antes y después.
- Informa de cuántas sesiones, mensajes y partes sobrevivieron, para que puedas
  comprobar que no se perdió nada.
- Activa `auto_vacuum = INCREMENTAL` para que el crecimiento futuro se pueda
  recuperar.

## Plataformas

**Probado únicamente en macOS.** Es donde se encontró el problema, donde se midió
y donde se verificó el resultado.

El soporte para Linux está escrito pero sin probar. Windows ahora sigue la ruta
de datos documentada por OpenCode y usa la API nativa de archivos de Windows
para la salvaguarda que detecta si la base está en uso, aunque todavía necesita
confirmación en una instalación real de Windows:

| Sistema | Estado | Rutas |
|---|---|---|
| macOS | **probado** | `~/.local/share/opencode/opencode.db`, `~/Library/Application Support/opencode/opencode.db` |
| Linux | escrito, sin probar | `$XDG_DATA_HOME/opencode/opencode.db`, `~/.local/share/opencode/opencode.db` |
| Windows | implementado, pendiente de confirmación | `%USERPROFILE%\.local\share\opencode\opencode.db` (CLI), `%LOCALAPPDATA%\opencode\data\opencode.db` (app de escritorio) |

Si te topaste con el mismo problema en Linux o en Windows y quieres mejorar esa
parte, **las contribuciones son bienvenidas**. Lo que más ayuda:

- Confirmar dónde guarda OpenCode la base en tu sistema.
- Comprobar la detección de «archivo en uso». En Windows solicita un handle
  exclusivo mediante `CreateFileW`; en Linux se usa `lsof`, que no siempre está
  instalado.
- Decir si las cifras se parecen a las de aquí o si el reparto de la tabla
  `event` es distinto.

Basta con abrir un *issue* con la salida del informe (el comando sin `--apply`,
que no cambia nada) o mandar un *pull request*.

Si existen varias bases, se elige la mayor. Se respeta `OPENCODE_DB`, que gana
sobre todo lo demás, y en cualquier sistema puedes saltarte la detección con
`--db /ruta/al/archivo`.

## En qué se diferencia de un VACUUM

Otras herramientas ejecutan `VACUUM`, que recupera **páginas libres** — espacio
que se liberó dentro del archivo pero que nunca se devolvió al sistema. Es un
problema real y esas herramientas lo resuelven.

Pero no resuelven *este*. Aquí el espacio son **filas vivas**. Un VACUUM solo no
recupera nada mientras las filas sigan ahí. Este script borra primero las filas
redundantes y después ejecuta VACUUM para encoger el archivo.

Si tu base está inflada pero la tabla `event` es pequeña, lo que necesitas es una
herramienta de VACUUM, no esta. Ejecuta el informe primero y verás en qué caso
estás.

## Issues relacionados en OpenCode

- [#22110 — Session storage grows unboundedly](https://github.com/anomalyco/opencode/issues/22110)
- [#31391 — why opencode.db so large?](https://github.com/anomalyco/opencode/issues/31391)
- [#16777 — High memory usage and database bloat](https://github.com/anomalyco/opencode/issues/16777)

El arreglo de fondo le corresponde al proyecto: no guardar una copia completa por
cada actualización del streaming, o podar el registro de cambios cuando una
sesión termina. Mientras tanto, esto.

## Licencia

MIT

## ¿Quieres liberar más espacio?

Si además quieres recuperar el espacio de tus **conversaciones de todas las
herramientas** (Claude Code, Codex, Antigravity, Command Code) recortando lo
anterior a la última compactación — con respaldo previo y skill para agentes —
mira **[conversation-reclaim](https://github.com/ANGELBERRIOS23/conversation-reclaim)**,
que integra esta poda de la DB junto con la de las demás herramientas.

