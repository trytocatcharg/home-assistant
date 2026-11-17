"""Bot de Telegram para controlar enchufes inteligentes Garza (firmware Tuya)
en red local, con programaciones horarias en Europe/Madrid."""

import asyncio
import json
import logging
import os
from datetime import datetime
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
        "/schedule <nombre> <HH:MM> <on|off> [diario|una\\_vez] — programar "
        "(por defecto, cada día a esa hora de Madrid)\n"
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


async def recover_schedules():
    """Al arrancar: recrea los jobs desde disco y ejecuta los que debieron
    dispararse mientras el bot estaba parado."""
    ahora = datetime.now(MADRID)
    for s in load_schedules():
        keys = [k for k in s["keys"] if k in DEVICES]
        if not keys:
            remove_schedule(s["id"])
            continue
        if s["mode"] == "diario":
            hoy = ahora.replace(hour=s["hour"], minute=s["minute"], second=0, microsecond=0)
            ultima = None
            if s.get("last_run"):
                ultima = datetime.fromisoformat(s["last_run"]).astimezone(MADRID)
            if hoy <= ahora and (ultima is None or ultima < hoy):
                log.info("Recuperando schedule perdido: %s", s["id"])
                update_schedule(s["id"], last_run=ahora.isoformat())
                await _apply_to_keys(keys, s["action"])
            trigger = CronTrigger(hour=s["hour"], minute=s["minute"], timezone=MADRID)
        else:
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


async def _apply_to_keys(keys: list, action: str):
    for key in keys:
        try:
            await set_switch(DEVICES[key]["device"], action)
        except Exception as e:
            log.warning("No pude aplicar %s a %s: %s", action, key, e)


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
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


async def schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    jobs = scheduler.get_jobs()
    if not jobs:
        await update.message.reply_text("📭 No hay programaciones activas.")
        return
    lineas = ["📅 *Programaciones activas (hora de Madrid):*"]
    for j in jobs:
        proxima = j.next_run_time.astimezone(MADRID).strftime("%d/%m %H:%M")
        lineas.append(f"- `{j.id}` → {j.name} (próxima: {proxima})")
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
    job = scheduler.get_job(context.args[0])
    if job is None:
        await update.message.reply_text(f"❌ No existe `{context.args[0]}`.", parse_mode="Markdown")
        return
    job.remove()
    remove_schedule(job.id)
    await update.message.reply_text(f"🗑️ Eliminada `{job.id}`.", parse_mode="Markdown")


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
            ("schedule", "Programar: /schedule <nombre> <HH:MM> <on|off> [diario|una_vez]"),
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

    log.info("Arrancando bot...")
    app.run_polling()


if __name__ == "__main__":
    main()
