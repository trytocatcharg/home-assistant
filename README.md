# Garza Smart → Telegram (control local)

Bot de Telegram para controlar enchufes inteligentes **Garza** (modelos 401262D /
401282D / 401262C / 401282C, firmware Tuya) **por red local**, sin depender de la
nube. Incluye estado/consumo, encendido/apagado y programaciones horarias con
zona horaria `Europe/Madrid`.

Arquitectura:

```
[Tú en Telegram] ←→ [bot.py + APScheduler] ←→ (Wi-Fi local) ←→ [enchufe Garza]
```

Requisitos: Python 3.9+, un equipo siempre encendido en la misma red Wi-Fi que
los enchufes, y cada enchufe con **IP local fija** (reserva DHCP en el router).

---

## Fase 1 — Emparejar los enchufes y obtener credenciales

### 1.1 Emparejar con Smart Life (no uses la app de Garza)

1. Instala **Smart Life** (o Tuya Smart) en el móvil.
2. Mantén pulsado el **botón físico del enchufe ~5 segundos** hasta que el LED
   parpadee **rápido** (modo emparejamiento). Si parpadea lento, pulsa de nuevo
   hasta que lo haga rápido.
3. En la app: `+` → **Enchufe (Socket)** → confirma que el LED parpadea rápido →
   introduce la contraseña de tu Wi-Fi **2.4 GHz** (si tu router es mixto, los
   enchufes solo ven la 2.4). El Bluetooth del móvil acelera el emparejamiento,
   pero el control posterior es por Wi-Fi.
4. **Pon nombre a cada enchufe en la app** (p. ej. `lampara_salon`): el nombre
   es lo que luego usarás en los comandos del bot.

### 1.2 Cuenta de desarrollador Tuya

1. Regístrate en [iot.tuya.com](https://iot.tuya.com).
2. Crea un **Cloud Project**: `Cloud` → `Create Cloud Project` → tipo **Smart Home** →
   centro de datos **Central Europe Data Center**.
3. Anota el **API Key** (Access ID) y el **API Secret** (Access Secret) del proyecto.
4. Vincula tu app: en el proyecto, pestaña **Devices** → **Link Tuya App Account** →
   escanea el QR **desde Smart Life** (perfil → escáner QR). Los enchufes aparecerán
   en el proyecto.

### 1.3 Extraer DEVICE_ID, LOCAL_KEY e IP con tinytuya

```bash
pip install tinytuya
python -m tinytuya wizard
```

Introduce la API Key, el API Secret, el centro de datos (`eu`) y las mismas
credenciales de tu app Smart Life cuando lo pida. El asistente genera un archivo
`devices.json` con, por cada enchufe: `id` (device id), `key` (local key),
`address` (IP local) y `version` (protocolo, normalmente 3.3 o 3.4).

> Repite el asistente cada vez que añadas un enchufe nuevo, o edita `devices.json`
> a mano con los datos nuevos.

### 1.4 IP fija

En el router, asigna una **reserva DHCP** (IP fija) a cada enchufe. Si la IP
cambia, el bot deja de encontrarlos.

---

## Fase 2 — Crear el bot de Telegram

1. En Telegram, escribe a **@BotFather** → `/newbot` → elige nombre y username.
   Guarda el **HTTP API Token** que te da.
2. Escribe a **@userinfobot** → `/start` → anota tu **ID numérico** (sirve para
   que solo tú puedas usar el bot).

---

## Fase 3 — Instalación y configuración

```bash
git clone <este-repo> && cd home-assistant
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Edita los dos archivos de configuración:

**`config.json`**

```json
{
  "telegram_token": "123456:ABC-...",
  "allowed_user_id": 123456789
}
```

**`devices.json`** — uno por enchufe (usa los datos del wizard; `name` es el
nombre que pondrás en los comandos, en minúsculas y sin espacios):

```json
[
  {
    "name": "lampara_salon",
    "id": "bf1234...",
    "ip": "192.168.1.41",
    "key": "a1b2c3...",
    "version": "3.3"
  }
]
```

Añadir un enchufe nuevo = añadir otro bloque a `devices.json` (y reiniciar el bot).

Arranca:

```bash
python bot.py
```

---

## Comandos

| Comando | Descripción |
|---|---|
| `/start` | Ayuda y lista de enchufes |
| `/status` | Estado, potencia, voltaje y corriente de **todos** los enchufes |
| `/switch [nombre]` | Alternar enchufes. Sin nombre, muestra teclado con el estado de cada uno (💡/⚪): pulsar enciende si está apagado y apaga si está encendido; el botón *Todos* enciende todo o apaga todo según el estado actual. También admite `/switch <nombre>` por texto |
| `/schedule <nombre> <HH:MM> <on\|off> [diario\|una_vez]` | Programar acción a una hora de Madrid (por defecto, **todos los días**) |
| `/schedules` | Ver programaciones activas |
| `/unschedule <id>` | Cancelar una programación (el `id` sale en `/schedules`) |

Ejemplos:

```
/switch                      # teclado para alternar
/switch lampara_salon        # alterna ese enchufe directamente
/schedule radiador_dormitorio 07:30 on        # cada día a las 7:30 de Madrid
/schedule lampara_salon 23:00 off una_vez     # solo hoy
```

Al detener el bot (cierre limpio, p. ej. `SIGTERM` al apagar el NAS), intenta
apagar todos los enchufes automáticamente. En un corte brusco de luz o un
apagado forzoso no hay software que pueda actuar, claro.

Los nombres admiten prefijo único: `/off lamp` apaga `lampara_salon` si es el
único que empieza por `lamp`.

---

## TODO

- [ ] **Reservas DHCP**: asignar IP fija en el router a cada enchufe
      (MAC `bc:35:1e:87:62:c1` → .131 habitación, `bc:35:1e:87:7b:42` → .130 salón).
      Ya falló una vez: las IPs cambiaron y el bot perdió los enchufes.
- [ ] **Firewall LAN-only**: bloquear en el router la salida a internet de cada enchufe
      (permitir tráfico dentro de la LAN, denegar tráfico saliente) una vez
      extraídas las local keys. Verificar que `/status` y `/on`/`/off` siguen
      funcionando tras el bloqueo.

## Depuración

- **Sin respuesta en `/status`**: cambia `"version"` a `3.4` (o `3.5`) en
  `devices.json` para ese enchufe.
- **Consumo a 0**: los Data Points del medidor pueden variar según firmware.
  Ejecuta esto para ver los índices exactos de tu modelo y ajusta `format_status`
  en `bot.py`:

  ```python
  import tinytuya, json
  d = tinytuya.OutletDevice("ID", "IP", "KEY")
  d.set_version(3.3)
  print(json.dumps(d.status(), indent=2))
  ```

  Los estándar son: `1` interruptor, `18` corriente (mA), `19` potencia (W×10),
  `20` voltaje (V×10).

- **El enchufe no aparece en el wizard**: comprueba que Smart Life está vinculada
  al proyecto (paso 1.2) y que elegiste el centro de datos `eu`.

## Arranque automático

- **Docker / NAS**: ver `docker-compose.yml`. Copia `.env.example` a `.env`,
  rellena `REPO_URL`, `TELEGRAM_TOKEN`, `ALLOWED_USER_ID`, `DEVICES_JSON` (JSON
  en una línea) y `DATA_PATH` (carpeta del NAS para `schedules.json`).
  `network_mode: host` es necesario para alcanzar los enchufes por IP local y
  `stop_grace_period: 30s` da tiempo a apagar los enchufes al detener el contenedor.
  Uso: `docker compose up -d`.
- **Linux / Raspberry Pi (systemd)**: crea `/etc/systemd/system/garza-bot.service`
  con `ExecStart=/ruta/al/venv/bin/python /ruta/a/bot.py`, `WorkingDirectory=/ruta/a`,
  y luego `sudo systemctl enable --now garza-bot`.
- **Windows**: `shell:startup` → acceso directo a `venv\Scripts\pythonw.exe bot.py`.
- **macOS**: `launchd` (plist en `~/Library/LaunchAgents`) con
  `/ruta/a/venv/bin/python /ruta/a/bot.py`.
