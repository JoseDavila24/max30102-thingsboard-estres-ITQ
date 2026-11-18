"""PulsePPG en la Raspberry Pi: captura una sesión y la publica en ThingsBoard.

Flujo de una sesión (una ejecución = una sesión):
  1. Genera un PIN aleatorio de 6 dígitos y lo muestra en el LCD.
  2. Espera a que la persona apoye el dedo en el MAX30102.
  3. Captura 5 minutos de señal (rojo + IR) a 50 muestras/s. Si se despega el
     dedo, la cuenta atrás se pausa hasta que vuelva; si el sensor deja de
     responder por I2C, se reconecta. Si no vuelven a tiempo, la sesión acaba
     con lo capturado (se guarda como `sesiones/<PIN>-incompleta.json`).
  4. Publica la sesión por MQTT (QoS 1) al dispositivo test-pi, troceada en
     mensajes de ~44 KB porque ThingsBoard no acepta mensajes MQTT grandes:
     primero las partes (`samples_part`) y al final la cabecera con pin,
     sample_count, started_at, finished_at y parts. La web las vuelve a unir.
  5. Deja el PIN en el LCD para que la persona lo teclee en la web.

Solo se usa MQTT. Si falla la red, la sesión queda en sesiones/ y se reenvía
con --reenviar.

Uso:
    python pulse.py                  # sesión real de 5 minutos
    python pulse.py --segundos 60    # sesión corta para probar
    python pulse.py --simular        # sin sensor: señal sintética (prueba ThingsBoard + web)
    python pulse.py --simular estres        # simulación que da "estrés"
    python pulse.py --simular sin_estres    # simulación que da "sin estrés" (= --simular)
    python pulse.py --simular mala_calidad  # simulación que da "lectura no válida"
    python pulse.py --sin-enviar     # captura y guarda en sesiones/, no publica
    python pulse.py --reenviar sesiones/123456.json   # vuelve a publicar una sesión guardada

Configuración en .env (ver .env.example).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "sesiones"
TOPIC = "v1/devices/me/telemetry"
SAMPLE_RATE_HZ = 50
PERIOD_MS = 1000 // SAMPLE_RATE_HZ  # 20 ms entre muestras


# --------------------------------------------------------------------------- LCD


class Display:
    """LCD 16x2 por I2C (PCF8574). Si no hay LCD, escribe en la consola."""

    def __init__(self, address: int | None):
        self.lcd = None
        self.last = ("", "")
        self.failed = False
        if address is None:
            return
        try:
            from RPLCD.i2c import CharLCD

            self.lcd = CharLCD("PCF8574", address, port=1, cols=16, rows=2,
                               charmap="A00", auto_linebreaks=False)
            self.lcd.clear()
        except Exception as exc:  # sin LCD conectado, dirección errónea, etc.
            print(f"[LCD] no disponible ({exc}); sigo solo con la consola")
            self.lcd = None

    def show(self, line1: str, line2: str = "", echo: bool = True) -> None:
        line1, line2 = line1[:16], line2[:16]
        if (line1, line2) == self.last:
            return
        self.last = (line1, line2)
        if echo:
            print(f"[LCD] {line1:<16} | {line2}")
        if self.lcd:
            # El LCD comparte el bus I2C con el sensor: si un cable falla, que
            # no tumbe la sesión. Se reintenta en el siguiente mensaje.
            try:
                self.lcd.cursor_pos = (0, 0)
                self.lcd.write_string(line1.ljust(16))
                self.lcd.cursor_pos = (1, 0)
                self.lcd.write_string(line2.ljust(16))
            except OSError as exc:
                self.last = ("", "")
                if not self.failed:
                    print(f"[LCD] no responde ({exc}); sigo con la consola")
                self.failed = True
            else:
                self.failed = False


# ------------------------------------------------------------------- captura


def mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# Ventana con la que se decide si hay dedo durante la captura: 0,5 s.
FINGER_WINDOW = SAMPLE_RATE_HZ // 2
# Al despegar el dedo se descarta también el último segundo, que es la
# transición (el dedo moviéndose) y estropearía la detección de latidos.
DISCARD_ON_LIFT = SAMPLE_RATE_HZ


class SensorPerdido(Exception):
    """El MAX30102 dejó de responder por I2C y no volvió a tiempo."""


@dataclass
class Captura:
    """Lo capturado hasta ahora. `motivo` es None si la sesión se completó;
    si no, dice por qué se cortó: "dedo", "sensor" o "cancelada"."""

    samples: list[dict] = field(default_factory=list)
    started_at: int = 0
    finished_at: int = 0
    motivo: str | None = None
    pausas: int = 0


def read_sensor(sensor, display: Display, pin: str, sensor_timeout: float):
    """Lee la FIFO. Si el sensor deja de responder, intenta reconectarlo
    durante `sensor_timeout` segundos.

    Devuelve (muestras, reconectado). Lanza SensorPerdido si no vuelve.
    """
    try:
        return sensor.read_fifo(), False
    except OSError as exc:
        print(f"AVISO: el sensor dejó de responder ({exc}); intento reconectarlo")
    display.show(f"PIN: {pin}", "Sensor perdido")
    deadline = time.monotonic() + sensor_timeout
    while True:
        try:
            sensor.reconnect()
            print("Sensor reconectado")
            return [], True
        except (OSError, RuntimeError) as exc:
            if time.monotonic() >= deadline:
                raise SensorPerdido(str(exc)) from None
        time.sleep(0.5)


def wait_for_finger(sensor, display: Display, pin: str, ir_min: int,
                    timeout: float | None = None, sensor_timeout: float = 10,
                    message: str = "Pon tu dedo...") -> bool:
    """Espera a que el IR medio de ~1 s supere `ir_min` (dedo apoyado).

    Devuelve False si pasan `timeout` segundos sin dedo (None = sin límite).
    """
    display.show(f"PIN: {pin}", message)
    deadline = None if timeout is None else time.monotonic() + timeout
    window: list[int] = []
    while True:
        samples, reconnected = read_sensor(sensor, display, pin, sensor_timeout)
        if reconnected:
            window = []
            display.show(f"PIN: {pin}", message)
        window.extend(ir for _, ir in samples)
        window = window[-SAMPLE_RATE_HZ:]
        if len(window) == SAMPLE_RATE_HZ and sum(window) / len(window) >= ir_min:
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def capture(sensor, display: Display, pin: str, seconds: int, cap: Captura,
            ir_min: int, finger_timeout: float, sensor_timeout: float) -> None:
    """Captura `seconds` segundos de señal con dedo en `cap`.

    - `t` son milisegundos calculados con el índice de la muestra (el sensor
      muestrea a ritmo fijo de 50 Hz), así los intervalos son exactos aunque
      la Pi lea la FIFO a trompicones.
    - Si se despega el dedo, la cuenta atrás se pausa, se descarta el último
      segundo y se espera hasta `finger_timeout` s a que vuelva. Si no vuelve,
      la sesión termina con lo capturado (cap.motivo = "dedo").
    - Si el sensor deja de responder, se intenta reconectar; tras reconectar
      se espera el dedo como en una pausa. Si no vuelve, cap.motivo = "sensor".
    - Todo lo capturado queda en `cap` aunque salga una excepción (Ctrl+C).
    """
    target = seconds * SAMPLE_RATE_HZ
    samples = cap.samples
    try:
        sensor.clear_fifo()
    except OSError:
        pass  # la primera lectura lo detecta e intenta reconectar
    cap.started_at = int(time.time() * 1000)
    # Tramo continuo actual (entre pausas), para comprobar que llegan 50 muestras/s.
    seg_start, seg_first = time.monotonic(), 0

    def check_rate() -> None:
        expected = (time.monotonic() - seg_start) * SAMPLE_RATE_HZ
        got = len(samples) - seg_first
        if expected - got > max(SAMPLE_RATE_HZ, 0.05 * expected):
            print(f"AVISO: en {expected / SAMPLE_RATE_HZ:.0f} s llegaron {got} muestras en vez de "
                  f"~{expected:.0f}; la Pi no leyó el sensor a tiempo y los tiempos quedan desplazados")

    try:
        while len(samples) < target:
            try:
                batch, reconnected = read_sensor(sensor, display, pin, sensor_timeout)
            except SensorPerdido as exc:
                print(f"El sensor no volvió en {sensor_timeout:.0f} s ({exc})")
                cap.motivo = "sensor"
                return
            for red, ir in batch:
                if len(samples) >= target:
                    break
                samples.append({"t": len(samples) * PERIOD_MS, "ir": ir, "red": red})

            recent = samples[-FINGER_WINDOW:]
            finger_lost = (len(recent) == FINGER_WINDOW
                           and sum(s["ir"] for s in recent) / FINGER_WINDOW < ir_min)
            if finger_lost or reconnected:
                if not reconnected:
                    check_rate()
                # Fuera la transición; la cuenta atrás se basa en las muestras,
                # así que queda pausada mientras no haya dedo.
                del samples[max(0, len(samples) - DISCARD_ON_LIFT):]
                print(f"Pausa ({'sin dedo' if finger_lost else 'sensor reconectado'}) "
                      f"con {mmss(len(samples) / SAMPLE_RATE_HZ)} capturados")
                try:
                    back = wait_for_finger(sensor, display, pin, ir_min, finger_timeout,
                                           sensor_timeout, message="Vuelve a poner")
                except SensorPerdido as exc:
                    print(f"El sensor no volvió en {sensor_timeout:.0f} s ({exc})")
                    cap.motivo = "sensor"
                    return
                if not back:
                    print(f"El dedo no volvió en {finger_timeout:.0f} s")
                    cap.motivo = "dedo"
                    return
                cap.pausas += 1
                seg_start, seg_first = time.monotonic(), len(samples)
                continue

            left = (target - len(samples)) / SAMPLE_RATE_HZ
            display.show(f"PIN: {pin}", f"Midiendo {mmss(left)}", echo=int(left) % 30 == 0)
            time.sleep(0.02)
        check_rate()
    finally:
        cap.finished_at = int(time.time() * 1000)


# Escenarios de --simular: intervalo medio entre latidos (ms) y su desviación.
#   sin_estres:   ~72 lpm con buena variabilidad -> 0 de 3 reglas
#   estres:       ~100 lpm y latidos muy regulares -> 3 de 3 reglas
#   mala_calidad: dedo mal apoyado (IR bajo y ruidoso) -> lectura no válida
SIM_MODES = {
    "sin_estres": (830, 45),
    "estres": (600, 8),
    "mala_calidad": (830, 45),
}


def simulate(seconds: int, mode: str = "sin_estres") -> tuple[list[dict], int, int]:
    """Señal PPG sintética del escenario `mode` (ver SIM_MODES), sin esperar."""
    rng = random.Random()
    ibi_mean, ibi_sd = SIM_MODES[mode]
    bad = mode == "mala_calidad"
    # Con el dedo mal apoyado vuelve poca luz IR y casi todo es ruido.
    ir_base, red_base, amp, noise = (20_000, 15_000, 150, 400) if bad else (110_000, 90_000, 900, 40)
    n = seconds * SAMPLE_RATE_HZ
    beats, t_beat = [], 300.0
    while t_beat < seconds * 1000 + 2000:
        beats.append(t_beat)
        t_beat += rng.gauss(ibi_mean, ibi_sd)
    samples, b = [], 0
    for i in range(n):
        t = i * PERIOD_MS
        while b + 1 < len(beats) and beats[b + 1] <= t:
            b += 1
        dt = (t - beats[b]) / 1000
        pulse = math.exp(-((dt - 0.12) / 0.06) ** 2) + 0.3 * math.exp(-((dt - 0.38) / 0.07) ** 2)
        drift = 600 * math.sin(2 * math.pi * t / 9000)
        ir = ir_base + drift - amp * pulse + rng.gauss(0, noise)
        red = red_base + drift * 0.8 - amp * 0.66 * pulse + rng.gauss(0, noise)
        samples.append({"t": t, "ir": int(ir), "red": int(red)})
    finished_at = int(time.time() * 1000)
    return samples, finished_at - seconds * 1000, finished_at


# ------------------------------------------------------------------- envío


def build_message(pin: str, samples: list[dict], started_at: int, finished_at: int) -> bytes:
    """Un solo mensaje con ts común. JSON compacto para que pese lo mínimo."""
    compact = (",", ":")
    message = {
        "ts": finished_at,
        "values": {
            "pin": pin,  # string: conserva los ceros a la izquierda
            "samples": json.dumps(samples, separators=compact),
            "sample_count": len(samples),
            "started_at": started_at,
            "finished_at": finished_at,
        },
    }
    return json.dumps(message, separators=compact).encode()


# Muestras por parte. Una muestra ocupa como mucho ~44 bytes dentro del
# mensaje, así que 1000 muestras son ~44 KB: por debajo de los 64 KB que
# ThingsBoard acepta por defecto en un mensaje MQTT.
PART_SAMPLES = 1000


def split_message(payload: bytes) -> list[bytes]:
    """Trocea la sesión en mensajes MQTT pequeños.

    - Parte i (i = 1..N): ts = ts_sesión + i, clave `samples_part` con
      1000 muestras (string JSON).
    - Cabecera, al final: ts = ts_sesión, claves pin, sample_count,
      started_at, finished_at y `parts` = N.
    La cabecera va la última para que la web no encuentre el PIN hasta que
    ThingsBoard haya confirmado todas las partes.
    """
    compact = (",", ":")
    message = json.loads(payload)
    ts, values = message["ts"], message["values"]
    samples = json.loads(values["samples"])
    chunks = [samples[i:i + PART_SAMPLES] for i in range(0, len(samples), PART_SAMPLES)]
    out = [
        json.dumps({"ts": ts + i, "values": {"samples_part": json.dumps(chunk, separators=compact)}},
                   separators=compact).encode()
        for i, chunk in enumerate(chunks, start=1)
    ]
    header = {k: v for k, v in values.items() if k != "samples"}
    header["parts"] = len(chunks)
    out.append(json.dumps({"ts": ts, "values": header}, separators=compact).encode())
    return out


def send_mqtt(host: str, port: int, token: str, messages: list[bytes], timeout: float = 30) -> bool:
    """Publica los mensajes en orden con QoS 1, esperando el PUBACK de cada uno."""
    import paho.mqtt.client as mqtt

    connected = threading.Event()
    lost = threading.Event()  # ThingsBoard cerró la conexión (p. ej. mensaje demasiado grande)
    refused: list[str] = []

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            refused.append(str(reason_code))
        connected.set()

    def on_disconnect(client, userdata, flags, reason_code, properties):
        lost.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(token)  # en ThingsBoard el usuario MQTT es el token del dispositivo
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    try:
        client.connect(host, port, keepalive=60)
    except OSError as exc:
        print(f"MQTT: no se pudo conectar a {host}:{port} ({exc})")
        return False
    client.loop_start()
    try:
        if not connected.wait(15) or refused:
            print(f"MQTT: conexión rechazada ({refused[0] if refused else 'sin respuesta'}); "
                  "revisa TB_HOST y TB_DEVICE_TOKEN")
            return False
        for n, payload in enumerate(messages, start=1):
            info = client.publish(TOPIC, payload, qos=1)
            # Esperamos el PUBACK, pero si ThingsBoard corta la conexión no
            # tiene sentido seguir: paho reintentaría el mensaje una y otra vez.
            deadline = time.monotonic() + timeout
            while not info.is_published() and not lost.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not info.is_published():
                print(f"MQTT: ThingsBoard no confirmó el mensaje {n}/{len(messages)} "
                      f"({len(payload) / 1024:.0f} KB); cerró la conexión o no respondió.")
                return False
            print(f"MQTT: mensaje {n}/{len(messages)} confirmado", end="\r")
        print()
        return True
    finally:
        client.disconnect()
        client.loop_stop()


def publish(payload: bytes) -> bool:
    """Publica la sesión por MQTT, troceada en partes. True si se confirmó todo."""
    host = os.environ.get("TB_HOST", "thingsboard.cloud")
    port = int(os.environ.get("TB_MQTT_PORT", "1883"))
    token = os.environ.get("TB_DEVICE_TOKEN", "")
    if not token or token.startswith("pega-aqui"):
        sys.exit("Falta TB_DEVICE_TOKEN en raspberrypi/.env")
    messages = split_message(payload)
    print(f"Sesión: {len(payload) / 1024:.0f} KB en {len(messages)} mensajes MQTT "
          f"(el mayor de {max(map(len, messages)) / 1024:.0f} KB)")
    return send_mqtt(host, port, token, messages)


def save(pin: str, payload: bytes, incompleta: bool = False) -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    path = SESSIONS_DIR / (f"{pin}-incompleta.json" if incompleta else f"{pin}.json")
    path.write_bytes(payload)
    return path


# ------------------------------------------------------------------- main


def main() -> None:
    load_dotenv(HERE / ".env")
    parser = argparse.ArgumentParser(description="Sesión PulsePPG en la Raspberry Pi")
    parser.add_argument("--segundos", type=int,
                        default=int(os.environ.get("SESSION_SECONDS", "300")),
                        help="duración de la captura (por defecto 300 = 5 min)")
    parser.add_argument("--simular", nargs="?", const="sin_estres", choices=sorted(SIM_MODES),
                        help="sin sensor: señal sintética (por defecto sin_estres)")
    parser.add_argument("--sin-enviar", action="store_true", help="solo guarda en sesiones/")
    parser.add_argument("--reenviar", metavar="ARCHIVO", help="publica una sesión guardada")
    args = parser.parse_args()

    if args.reenviar:
        ok = publish(Path(args.reenviar).read_bytes())
        print("Publicado por MQTT" if ok else "No se pudo publicar")
        sys.exit(0 if ok else 1)

    lcd_addr = os.environ.get("LCD_ADDRESS", "0x27")
    display = Display(None if lcd_addr.lower() in ("", "none") else int(lcd_addr, 16))
    pin = f"{secrets.randbelow(1_000_000):06d}"
    print(f"PIN de la sesión: {pin}")

    if args.simular:
        display.show(f"PIN: {pin}", "Simulando...")
        print(f"Simulación: {args.simular}")
        samples, started_at, finished_at = simulate(args.segundos, args.simular)
        cap = Captura(samples, started_at, finished_at)
    else:
        cap = capture_real(display, pin, args.segundos)

    seconds_ok = len(cap.samples) / SAMPLE_RATE_HZ
    payload = build_message(pin, cap.samples, cap.started_at, cap.finished_at)
    path = save(pin, payload, incompleta=cap.motivo is not None)
    print(f"{len(cap.samples)} muestras ({mmss(seconds_ok)}) guardadas en {path}")

    if cap.motivo:
        min_seconds = int(os.environ.get("MIN_SESSION_SECONDS", "60"))
        why = {"dedo": "el dedo no volvió", "sensor": "el sensor dejó de responder",
               "cancelada": "cancelada con Ctrl+C"}[cap.motivo]
        print(f"Sesión incompleta: {why}.")
        if cap.motivo == "cancelada" or seconds_ok < min_seconds:
            if cap.motivo != "cancelada":
                print(f"Hay menos de {min_seconds} s de señal: no se publica. "
                      "Repite la sesión.")
            display.show(f"PIN: {pin}", "Incompleta")
            sys.exit(1)
        print(f"Se publica igual: hay {mmss(seconds_ok)} de señal (mínimo {min_seconds} s).")
    if args.sin_enviar:
        display.show(f"PIN: {pin}", "Guardado local")
        return

    display.show(f"PIN: {pin}", "Enviando...")
    if publish(payload):
        print(f"Sesión publicada por MQTT. Teclea el PIN {pin} en la web.")
        display.show(f"PIN: {pin}", "Listo! Ve a web")
    else:
        print(f"No se pudo publicar. Reintenta con: python pulse.py --reenviar {path}")
        display.show(f"PIN: {pin}", "Error al enviar")
        sys.exit(1)


def capture_real(display: Display, pin: str, seconds: int) -> Captura:
    """Sesión con el MAX30102. Pase lo que pase devuelve lo capturado."""
    from max30102 import MAX30102

    ir_min = int(os.environ.get("FINGER_IR_MIN", "50000"))
    finger_timeout = float(os.environ.get("FINGER_TIMEOUT", "20"))
    sensor_timeout = float(os.environ.get("SENSOR_TIMEOUT", "10"))
    try:
        sensor = MAX30102(led_current=int(os.environ.get("LED_CURRENT", "0x24"), 16))
    except (OSError, RuntimeError) as exc:
        display.show(f"PIN: {pin}", "Sin sensor")
        sys.exit(f"No se detecta el MAX30102 ({exc}). Revisa los cables y que "
                 "`sudo i2cdetect -y 1` muestre 57.")

    cap = Captura()
    try:
        try:
            wait_for_finger(sensor, display, pin, ir_min, sensor_timeout=sensor_timeout)
        except SensorPerdido as exc:
            display.show(f"PIN: {pin}", "Sin sensor")
            sys.exit(f"El sensor dejó de responder antes de empezar ({exc}). Revisa los cables.")
        capture(sensor, display, pin, seconds, cap, ir_min, finger_timeout, sensor_timeout)
    except KeyboardInterrupt:
        cap.motivo = "cancelada"
        display.show(f"PIN: {pin}", "Cancelado")
        if not cap.samples:
            sys.exit(1)
    finally:
        sensor.close()
    if cap.pausas:
        print(f"La captura se pausó {cap.pausas} vez/veces (dedo fuera o sensor reconectado)")
    return cap

if __name__ == "__main__":
    main()
