---
name: opencode-db-prune
description: >-
  Recupera espacio de la base de datos de OpenCode (opencode.db) eliminando el
  evento-log redundante: OpenCode guarda una copia completa de cada mensaje en
  cada actualización de streaming, por lo que una base de pocos GB puede tener
  el 90%+ de puro duplicado (una sesión larga genera cientos de miles de
  snapshots). Úsala cuando el usuario diga "el opencode.db está enorme",
  "opencode me ocupa demasiado", "liberar espacio de opencode", "podar la base
  de opencode", "reducir el tamaño de mi opencode.db" o quiera diagnosticar el
  tamaño de la base (--stats). No borra sesiones ni mensajes: solo snapshots
  redundantes que ya viven en las tablas message/part.
---

# opencode-db-prune

Herramienta CLI en Python (solo stdlib) que recorta el **evento-log redundante**
de `opencode.db` sin borrar ni una sesión, ni un mensaje, ni una part.

## El problema

OpenCode hace event-sourcing: cada actualización de streaming de una respuesta
escribe un snapshot completo del mensaje en la tabla `event`
(`message.part.updated.1`, `message.updated.1`, `session.updated.1`). Una sola
respuesta genera decenas de snapshots intermedios y cada uno lleva el texto
acumulado completo. Resultado: bases donde el 90%+ es pura duplicación
(verificado: 147k filas / 4.4 GB en una base de 4.9 GB).

## Dónde está

- Script: repo `ANGELBERRIOS23/opencode-db-prune` → `opencode-db-prune.py`.
  Si no está, clonar: `git clone https://github.com/ANGELBERRIOS23/opencode-db-prune`.
- Base: `~/.local/share/opencode/opencode.db` (macOS/Linux) o
  `%USERPROFILE%\.local\share\opencode\opencode.db` /
  `%LOCALAPPDATA%\opencode\data` en Windows (o `$OPENCODE_DB`).

## Comandos (paso a paso)

```bash
python3 opencode-db-prune.py                    # reporte completo (solo lee)
python3 opencode-db-prune.py --stats            # estadísticas rápidas
python3 opencode-db-prune.py --apply            # poda con respaldo previo
python3 opencode-db-prune.py --apply --session <id>   # solo una sesión
python3 opencode-db-prune.py --apply --tool-output --max-age 30  # + tool-output viejo
python3 opencode-db-prune.py --apply --batch 5000   # lotes grandes sin bloquear
python3 opencode-db-prune.py --schedule 6h      # programado cada 6 h
```

1. **Primero `--stats` o el reporte plano**: mostrar al usuario cuántos eventos
   redundantes hay y cuánto se liberaría (el reporte dice "Would prune: X rows,
   Y MB").
2. **Confirmar con el usuario** antes de `--apply`. Regla: *respaldo antes de
   tocar* — el script copia `opencode.db` a `opencode.db.bak-<fecha>` por
   defecto.
3. **`--apply`**: hace pre-flight (verifica que el contenido viva en
   `message`/`part`, no solo en `event`), borra en lotes, VACUUM, verifica
   integridad y muestra el resultado.

## Seguridad

- **Refusa si opencode está corriendo** (la DB está abierta) — avisar al
  usuario que cierre opencode y reintente.
- Pre-flight: si el contenido solo existe en la tabla `event`, se niega a
  tocar nada.
- Respaldo automático `opencode.db.bak-<fecha>` salvo `--no-backup`.
- `PRAGMA integrity_check` antes/después + verificación de sesiones al azar.
- Respeta agregados sincronizados (workspace/sync) salvo `--force-synced`.
- Por defecto conserva intactas las N sesiones más recientes (`--keep`, por
  defecto 5).

## Si falla / hacerlo a mano

```bash
# diagnóstico: cuánta basura hay
sqlite3 opencode.db "SELECT type, count(*), sum(length(data)) FROM event GROUP BY type"
# limpieza manual (con opencode cerrado y tras respaldar):
sqlite3 opencode.db "DELETE FROM event WHERE type IN ('message.updated.1','message.part.updated.1','session.updated.1'); VACUUM;"
# verificación
sqlite3 opencode.db "PRAGMA integrity_check"   # → ok
```

El respaldo `.bak-<fecha>` se restaura copiándolo sobre `opencode.db` con
opencode cerrado.

## Disclaimer

**Sin garantía, úsalo bajo tu propio riesgo.** El script borra datos
redundantes; quien lo ejecuta es responsable de lo que se elimina. El diseño
conserva todo lo que se ve en el chat (el contenido final vive en
`message`/`part`), pero revisa siempre el reporte antes de `--apply`.

## Contribuciones

Bienvenido cualquier aporte (Windows, Linux, batcheo, dedup...). En el PR:
indicar SO donde se probó, el comando exacto usado, y el antes/después del
`--stats`. **Todo PR pasa revisión humana antes de aprobarse.**
