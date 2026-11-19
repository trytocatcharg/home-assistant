"""Bot de Telegram para controlar enchufes inteligentes Garza (firmware Tuya)
en red local, con programaciones horarias en Europe/Madrid."""

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tinytuya
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DEVICES_PATH = BASE_DIR / "devices.json"
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
SCHEDULES_PATH = DATA_DIR / "schedules.json"
MADRID = ZoneInfo("Europe/Madrid")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx loguea las URLs de las llamadas a la API de Telegram con el token
# en claro (INFO "HTTP Request..."); subirlo a WARNING para no exponerlo
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("garza-bot")

# Config por variables de entorno (Docker) con fallback a config.json local
_cfg = {}
if CONFIG_PATH.exists():
    _cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", _cfg.get("telegram_token", ""))
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", _cfg.get("allowed_user_id", 0)))


def normalize(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def load_devices() -> dict:
    """Carga devices.json (o la variable DEVICES_JSON en Docker) y crea un
    objeto tinytuya por enchufe, indexado por nombre."""
    if os.environ.get("DEVICES_JSON"):
        raw = json.loads(os.environ["DEVICES_JSON"])
    else:
        raw = json.loads(DEVICES_PATH.read_text(encoding="utf-8"))
    devices = {}
    for d in raw:
        name = normalize(d["name"])
        ip = d.get("ip") or d.get("address")
        dev = tinytuya.OutletDevice(d["id"], ip, d["key"])
        dev.set_version(float(d.get("version", 3.3)))
        devices[name] = {"device": dev, "label": d["name"]}
    return devices


DEVICES = load_devices()
scheduler = AsyncIOScheduler(timezone=MADRID)


def is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


def find_device(name: str):
    """Devuelve la entrada del dispositivo o None. Acepta coincidencia exacta
    o única por prefijo (p.ej. 'lamp' -> 'lampara_salon')."""
    key = normalize(name)
    if key in DEVICES:
        return DEVICES[key]
    matches = [v for k, v in DEVICES.items() if k.startswith(key)]
    return matches[0] if len(matches) == 1 else None


def resolve_devices(name: str) -> list | None:
    """Lista de entradas de dispositivo a partir de un nombre,
    o None si no se encuentra. 'todos' selecciona todo."""
    key = normalize(name)
    if key in ("todos", "todo", "all"):
        return list(DEVICES.values())
    entry = find_device(name)
    return [entry] if entry else None


async def read_status(dev: tinytuya.OutletDevice) -> dict:
    return await asyncio.to_thread(dev.status)


async def set_switch(dev: tinytuya.OutletDevice, action: str) -> None:
    if action == "on":
        await asyncio.to_thread(dev.turn_on)
    else:
        await asyncio.to_thread(dev.turn_off)


async def read_device_state(entry: dict) -> bool | None:
    """True=encendido, False=apagado, None=sin respuesta."""
    try:
        dps = (await read_status(entry["device"])).get("dps", {})
        return bool(dps.get("1")) if dps else None
    except Exception:
        return None


async def build_switch_keyboard() -> InlineKeyboardMarkup:
    """Teclado de alternancia: un botón por enchufe con su estado real."""
    rows = []
    for key, entry in DEVICES.items():
        estado = await read_device_state(entry)
        icon = "💡" if estado else ("⚪" if estado is False else "❓")
        rows.append(
            [InlineKeyboardButton(f"{icon} {entry['label']}", callback_data=f"sw:{key}")]
        )
    rows.append([InlineKeyboardButton("Todos", callback_data="sw:todos")])
    return InlineKeyboardMarkup(rows)


def format_status(label: str, dps: dict) -> str:
    estado = "🟢 ENCENDIDO" if dps.get("1") else "🔴 APAGADO"
    potencia = dps.get("19", 0) / 10.0
    voltaje = dps.get("20", 0) / 10.0
    corriente = dps.get("18", 0)
    linea = f"*{label}*: {estado} — {potencia} W, {voltaje} V ({corriente} mA)"
    if not dps.get("1"):
        linea = f"*{label}*: {estado}"
    return linea


# --- Comandos de Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    nombres = ", ".join(f"`{k}`" for k in DEVICES)
    msg = (
        "🔌 *Control Garza Smart*\n\n"
        "/status — estado y consumo de todos los enchufes\n"
        "/switch [nombre] — alternar encendido/apagado\n"
        "/schedule — programar (presencia aleatoria u horario fijo)\n"
        "/schedules — ver programaciones activas\n"
        "/unschedule <id> — cancelar una programación\n\n"
        f"Enchufes disponibles: {nombres}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    lineas = []
    for key, entry in DEVICES.items():
        try:
            data = await read_status(entry["device"])
            dps = data.get("dps", {})
            if not dps:
                lineas.append(f"*{entry['label']}*: ⚠️ sin respuesta en la red local")
            else:
                lineas.append(format_status(entry["label"], dps))
        except Exception as e:
            log.warning("Error leyendo %s: %s", key, e)
            lineas.append(f"*{entry['label']}*: ⚠️ error de conexión")
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


async def toggle_devices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Pulsa un enchufe para *alternar* su estado:",
            reply_markup=await build_switch_keyboard(),
            parse_mode="Markdown",
        )
        return
    entries = resolve_devices(context.args[0])
    if entries is None:
        nombres = ", ".join(DEVICES.keys())
        await update.message.reply_text(
            f"❌ No encuentro `{context.args[0]}`. Enchufes: {nombres}",
            parse_mode="Markdown",
        )
        return
    fallos, resultados = await do_toggle(entries)
    texto = "\n".join(resultados) if resultados else "⚠️ Nadie respondió."
    if fallos:
        texto += f"\n⚠️ Sin respuesta: {', '.join(fallos)}"
    await update.message.reply_text(texto, parse_mode="Markdown")


async def do_toggle(entries: list) -> tuple[list, list]:
    """Alterna cada enchufe según su estado actual.
    Devuelve (fallos, líneas de resultado)."""
    fallos, resultados = [], []
    for entry in entries:
        try:
            estado = await read_device_state(entry)
            if estado is None:
                raise ConnectionError("sin respuesta")
            accion = "off" if estado else "on"
            await set_switch(entry["device"], accion)
            icono = "🔴" if accion == "off" else "🟢"
            verbo = "apagado" if accion == "off" else "encendido"
            resultados.append(f"{icono} {entry['label']} *{verbo}*")
        except Exception as e:
            log.warning("Error alternando %s: %s", entry["label"], e)
            fallos.append(entry["label"])
    return fallos, resultados


async def switch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(update):
        await query.answer("No autorizado", show_alert=True)
        return
    await query.answer()
    nombre = query.data.split(":", 1)[1]
    if nombre == "todos":
        entries = list(DEVICES.values())
        estados = {e["label"]: await read_device_state(e) for e in entries}
        todos_on = all(s is True for s in estados.values()) and bool(estados)
        accion = "off" if todos_on else "on"
        for entry in entries:
            try:
                await set_switch(entry["device"], accion)
            except Exception as e:
                log.warning("Error en %s: %s", entry["label"], e)
        await query.answer("Todos apagados" if accion == "off" else "Todos encendidos")
    else:
        entry = find_device(nombre)
        if entry is None:
            await query.answer("Enchufe no encontrado", show_alert=True)
            return
        estado = await read_device_state(entry)
        if estado is None:
            await query.answer(f"⚠️ {entry['label']} no responde", show_alert=True)
            return
        accion = "off" if estado else "on"
        try:
            await set_switch(entry["device"], accion)
        except Exception as e:
            log.warning("Error en %s: %s", entry["label"], e)
            await query.answer(f"⚠️ {entry['label']} no responde", show_alert=True)
            return
        await query.answer(
            f"{entry['label']}: {'apagado' if accion == 'off' else 'encendido'}"
        )
    await query.edit_message_reply_markup(await build_switch_keyboard())


async def _run_scheduled(entry: dict, action: str, job_id: str, mode: str):
    # Persistir antes de ejecutar: garantiza ejecucion como mucho una vez
    if mode == "una_vez":
        remove_schedule(job_id)
    else:
        update_schedule(job_id, last_run=datetime.now(MADRID).isoformat())
    await set_switch(entry["device"], action)


# --- Persistencia de programaciones (schedules.json) ---

def load_schedules() -> list:
    if SCHEDULES_PATH.exists():
        try:
            return json.loads(SCHEDULES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_schedules(schedules: list) -> None:
    SCHEDULES_PATH.write_text(
        json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def persist_schedule(job_id, keys, action, mode, hour, minute, run_date=None):
    schedules = [s for s in load_schedules() if s["id"] != job_id]
    entry = {
        "id": job_id,
        "keys": keys,
        "action": action,
        "mode": mode,
        "hour": hour,
        "minute": minute,
        "last_run": None,
    }
    if run_date:
        entry["run_date"] = run_date.isoformat()
    schedules.append(entry)
    save_schedules(schedules)


def update_schedule(job_id: str, **fields):
    schedules = load_schedules()
    for s in schedules:
        if s["id"] == job_id:
            s.update(fields)
    save_schedules(schedules)


def remove_schedule(job_id: str):
    save_schedules([s for s in load_schedules() if s["id"] != job_id])


def roll_presencia(base: datetime) -> tuple[datetime, datetime]:
    """Tira los dados del dia: ON entre 18:00 y 18:29,
    OFF entre 19:30 y 19:59 (independiente por enchufe)."""
    on_time = base.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(
        minutes=random.randint(0, 29)
    )
    off_time = base.replace(hour=19, minute=30, second=0, microsecond=0) + timedelta(
        minutes=random.randint(0, 29)
    )
    if off_time <= on_time:
        off_time = on_time + timedelta(minutes=1)
    return on_time, off_time


async def _run_planner():
    update_schedule("presencia-planner", last_run=datetime.now(MADRID).isoformat())
    await planificar_hoy()


async def planificar_hoy() -> dict:
    """Para cada entrada 'presencia' sin planificar hoy: tira los dados y crea
    los dos eventos puntuales del dia. Devuelve {key: (on, off)}."""
    ahora = datetime.now(MADRID)
    hoy = ahora.date().isoformat()
    veces = {}
    for s in load_schedules():
        if s["mode"] != "presencia" or s.get("today") == hoy:
            continue
        keys = [k for k in s["keys"] if k in DEVICES]
        for key in keys:
            on_time, off_time = roll_presencia(ahora)
            if off_time <= ahora:
                continue  # ventana de hoy ya pasada
            veces[key] = (on_time, off_time)
            for accion, cuando in (("on", on_time), ("off", off_time)):
                job_id = f"{key}-presencia-{hoy}-{accion}"
                persist_schedule(
                    job_id, [key], accion, "una_vez",
                    cuando.hour, cuando.minute, run_date=cuando,
                )
                scheduler.add_job(
                    _run_scheduled,
                    DateTrigger(run_date=cuando),
                    args=[DEVICES[key], accion, job_id, "una_vez"],
                    id=job_id,
                    replace_existing=True,
                    name=job_id,
                )
        cambios = {"today": hoy}
        if len(keys) == 1 and keys[0] in veces:
            cambios["today_on"] = veces[keys[0]][0].isoformat()
            cambios["today_off"] = veces[keys[0]][1].isoformat()
        update_schedule(s["id"], **cambios)
    return veces


async def recover_schedules():
    """Al arrancar: recrea los jobs desde disco y ejecuta los que debieron
    dispararse mientras el bot estaba parado."""
    ahora = datetime.now(MADRID)
    hoy = ahora.date().isoformat()
    hay_que_planificar = False
    for s in load_schedules():
        mode = s["mode"]
        keys = [k for k in s.get("keys", []) if k in DEVICES]
        if mode == "planner":
            scheduler.add_job(
                _run_planner,
                CronTrigger(hour=s["hour"], minute=s["minute"], timezone=MADRID),
                id=s["id"],
                replace_existing=True,
                name=s["id"],
            )
            ultima = s.get("last_run")
            if not ultima or datetime.fromisoformat(ultima).astimezone(MADRID).date().isoformat() < hoy:
                hay_que_planificar = True
            continue
        if mode == "presencia":
            if s.get("today") != hoy:
                hay_que_planificar = True
            continue
        if not keys:
            remove_schedule(s["id"])
            continue
        if mode == "diario":
            hoy_dt = ahora.replace(hour=s["hour"], minute=s["minute"], second=0, microsecond=0)
            ultima = None
            if s.get("last_run"):
                ultima = datetime.fromisoformat(s["last_run"]).astimezone(MADRID)
            if hoy_dt <= ahora and (ultima is None or ultima < hoy_dt):
                log.info("Recuperando schedule perdido: %s", s["id"])
                update_schedule(s["id"], last_run=ahora.isoformat())
                await _apply_to_keys(keys, s["action"])
            trigger = CronTrigger(hour=s["hour"], minute=s["minute"], timezone=MADRID)
        else:  # una_vez
            run_date = datetime.fromisoformat(s["run_date"]).astimezone(MADRID)
            if run_date <= ahora:
                log.info("Recuperando schedule unico perdido: %s", s["id"])
                remove_schedule(s["id"])
                await _apply_to_keys(keys, s["action"])
                continue
            trigger = DateTrigger(run_date=run_date)
        for key in keys:
            scheduler.add_job(
                _run_scheduled,
                trigger,
                args=[DEVICES[key], s["action"], s["id"], s["mode"]],
                id=s["id"],
                replace_existing=True,
                name=s["id"],
            )
    if hay_que_planificar:
        await planificar_hoy()


async def _apply_to_keys(keys: list, action: str):
    for key in keys:
        try:
            await set_switch(DEVICES[key]["device"], action)
        except Exception as e:
            log.warning("No pude aplicar %s a %s: %s", action, key, e)


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        teclado = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(
                    "🎲 Presencia diaria (ON 18:00–18:29 / OFF 19:30–19:59)",
                    callback_data="sch:menu:presencia")],
                [InlineKeyboardButton("✏️ Horario custom",
                                      callback_data="sch:menu:custom")],
            ]
        )
        await update.message.reply_text(
            "¿Qué programación quieres crear?", reply_markup=teclado
        )
        return
    uso = (
        "❌ Uso: `/schedule <nombre> <HH:MM> <on|off> [diario|una_vez]`\n"
        "Ejemplo: `/schedule lampara 22:30 off` (todos los días a las 22:30 de Madrid)"
    )
    try:
        nombre, hora_str, accion = context.args[0], context.args[1], context.args[2].lower()
        modo = context.args[3].lower() if len(context.args) > 3 else "diario"
        hora, minuto = map(int, hora_str.split(":"))
        if not (0 <= hora <= 23 and 0 <= minuto <= 59):
            raise ValueError
        if accion not in ("on", "off") or modo not in ("diario", "una_vez"):
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(uso, parse_mode="Markdown")
        return

    entries = resolve_devices(nombre)
    if entries is None:
        nombres = ", ".join(DEVICES.keys())
        await update.message.reply_text(
            f"❌ No encuentro `{nombre}`. Enchufes: {nombres}", parse_mode="Markdown"
        )
        return

    creadas = []
    for entry in entries:
        key = next(k for k, v in DEVICES.items() if v is entry)
        job_id = f"{key}-{accion}-{hora_str.replace(':', 'h')}-{modo}"
        run_date = None
        if modo == "diario":
            trigger = CronTrigger(hour=hora, minute=minuto, timezone=MADRID)
        else:
            run_date = datetime.now(MADRID).replace(
                hour=hora, minute=minuto, second=0, microsecond=0
            )
            if run_date <= datetime.now(MADRID):
                await update.message.reply_text(
                    f"⚠️ {entry['label']}: esa hora ya pasó hoy.", parse_mode="Markdown"
                )
                continue
            trigger = DateTrigger(run_date=run_date)
        scheduler.add_job(
            _run_scheduled,
            trigger,
            args=[entry, accion, job_id, modo],
            id=job_id,
            replace_existing=True,
            name=f"{entry['label']} {accion} {hora_str} ({modo})",
        )
        persist_schedule(job_id, [key], accion, modo, hora, minuto, run_date=run_date)
        creadas.append(f"⏰ *{entry['label']}*: `{accion}` a las `{hora_str}` ({modo})")

    if creadas:
        await update.message.reply_text("\n".join(creadas), parse_mode="Markdown")


async def schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(update):
        await query.answer("No autorizado", show_alert=True)
        return
    await query.answer()
    opcion = query.data.split(":")[2]
    if opcion == "custom":
        await query.edit_message_text(
            "✏️ *Horario custom* — escribe el comando directamente:\n"
            "`/schedule <nombre> <HH:MM> <on|off> [diario|una_vez]`\n"
            "Ejemplo: `/schedule lámpara_salon 22:30 off`",
            parse_mode="Markdown",
        )
        return
    # Activar presencia para TODOS los enchufes, dados independientes por uno
    for key in DEVICES:
        persist_schedule(f"{key}-presencia", [key], "presencia", "presencia", 18, 0)
    persist_schedule("presencia-planner", [], "plan", "planner", 0, 5)
    scheduler.add_job(
        _run_planner,
        CronTrigger(hour=0, minute=5, timezone=MADRID),
        id="presencia-planner",
        replace_existing=True,
        name="presencia-planner",
    )
    veces = await planificar_hoy()
    lineas = ["🎲 *Presencia diaria activada* — dados independientes por enchufe:"]
    for key, (on_time, off_time) in veces.items():
        lineas.append(
            f"- {DEVICES[key]['label']}: ON {on_time:%H:%M} / OFF {off_time:%H:%M} (hoy)"
        )
    if not veces:
        lineas.append("- Hoy la ventana ya pasó; los dados se tiran esta noche a las 00:05.")
    lineas.append("\nCada día cambia. Cancelar: `/unschedule <nombre>-presencia`")
    await query.edit_message_text("\n".join(lineas), parse_mode="Markdown")


async def schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    lineas = ["📅 *Programaciones activas (hora de Madrid):*"]
    nombres_vistos = set()
    for j in scheduler.get_jobs():
        if j.id == "presencia-planner" or re.search(r"-presencia-\d{4}-\d{2}-\d{2}-", j.id):
            continue  # ruido interno del modo presencia
        nombres_vistos.add(j.id)
        proxima = j.next_run_time.astimezone(MADRID).strftime("%d/%m %H:%M")
        lineas.append(f"- `{j.id}` → {j.name} (próxima: {proxima})")
    for s in load_schedules():
        if s["mode"] != "presencia" or s["id"] in nombres_vistos:
            continue
        detalle = ""
        if s.get("today_on") and s.get("today_off"):
            on = datetime.fromisoformat(s["today_on"]).astimezone(MADRID)
            off = datetime.fromisoformat(s["today_off"]).astimezone(MADRID)
            detalle = f" — hoy ON {on:%H:%M} / OFF {off:%H:%M}"
        lineas.append(f"- `{s['id']}` → presencia diaria{detalle} (dados a las 00:05)")
    if len(lineas) == 1:
        await update.message.reply_text("📭 No hay programaciones activas.")
        return
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


async def unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text(
            "❌ Uso: `/unschedule <id>` (el id lo ves con /schedules)",
            parse_mode="Markdown",
        )
        return
    job_id = context.args[0]
    job = scheduler.get_job(job_id)
    if job is not None:
        job.remove()
    elif not any(s["id"] == job_id for s in load_schedules()):
        await update.message.reply_text(f"❌ No existe `{job_id}`.", parse_mode="Markdown")
        return
    remove_schedule(job_id)
    # Si es una entrada presencia, limpiar tambien los eventos del dia
    prefijo = job_id + "-"
    for s in load_schedules():
        if s["id"].startswith(prefijo):
            j = scheduler.get_job(s["id"])
            if j:
                j.remove()
            remove_schedule(s["id"])
    await update.message.reply_text(f"🗑️ Eliminada `{job_id}`.", parse_mode="Markdown")


_scheduler_started = False


async def on_startup(app: Application):
    global _scheduler_started
    if not _scheduler_started:
        scheduler.start()
        _scheduler_started = True
        await recover_schedules()
    await app.bot.set_my_commands(
        [
            ("start", "Ayuda y lista de enchufes"),
            ("status", "Estado y consumo de todos los enchufes"),
            ("switch", "Alternar enchufes: /switch o /switch <nombre>"),
            ("schedule", "Programar: presencia aleatoria u horario custom"),
            ("schedules", "Ver programaciones activas"),
            ("unschedule", "Cancelar: /unschedule <id>"),
        ]
    )
    log.info("Bot en ejecución con %d enchufe(s): %s", len(DEVICES), ", ".join(DEVICES))
    try:
        lista = "\n".join(f"- {e['label']}" for e in DEVICES.values())
        await app.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=f"✅ *Bot iniciado* con {len(DEVICES)} enchufe(s):\n{lista}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("No pude enviar el mensaje de arranque a Telegram: %s", e)


async def on_shutdown(app: Application):
    """Al detener el bot (p. ej. el NAS se apaga), intenta apagar todos
    los enchufes. Solo funciona en cierres limpios (SIGTERM/SIGINT)."""
    log.info("Cierre del bot: apagando todos los enchufes...")
    for key, entry in DEVICES.items():
        try:
            await set_switch(entry["device"], "off")
            log.info("%s apagado", key)
        except Exception as e:
            log.warning("No pude apagar %s: %s", key, e)


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Falta TELEGRAM_TOKEN: config.json o variable de entorno")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("switch", toggle_devices))
    app.add_handler(CommandHandler("schedule", schedule))
    app.add_handler(CommandHandler("schedules", schedules))
    app.add_handler(CommandHandler("unschedule", unschedule))
    app.add_handler(CallbackQueryHandler(switch_callback, pattern=r"^sw:"))
    app.add_handler(CallbackQueryHandler(schedule_callback, pattern=r"^sch:"))

    log.info("Arrancando bot...")
    app.run_polling()


if __name__ == "__main__":
    main()
