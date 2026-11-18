# PulsePPG · Raspberry Pi

Código del lado IoT: captura la señal del sensor, genera el PIN, lo muestra en el
LCD y publica la sesión en ThingsBoard (dispositivo `test-pi`) por MQTT con QoS 1.

> **Hardware supuesto (por confirmar):** Raspberry Pi 5 + sensor **MAX30102** +
> LCD **16x2 con adaptador I2C PCF8574**. Conexiones en [CONEXIONES.md](CONEXIONES.md).

```
raspberrypi/
  pulse.py          una sesión: PIN -> dedo -> 5 min de captura -> publicación
  max30102.py       driver mínimo del sensor por I2C (50 muestras/s, rojo + IR)
  .env.example      host de ThingsBoard, token de test-pi y ajustes
  requirements.txt  paho-mqtt, python-dotenv, smbus2, RPLCD
  CONEXIONES.md     cableado y comprobación del I2C
  sesiones/         copia local de cada sesión enviada (se crea sola, no se sube a git)
```

## Instalación (una vez)

```bash
# 1. Activa el I2C y comprueba que se ven el sensor y el LCD (ver CONEXIONES.md)
sudo raspi-config            # Interface Options > I2C > Enable
i2cdetect -y 1               # deben aparecer 57 y 27 (o 3f)

# 2. Entorno de Python (Raspberry Pi OS Bookworm exige un venv)
cd raspberrypi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configuración
cp .env.example .env
nano .env                    # pon TB_HOST y TB_DEVICE_TOKEN de test-pi
```

## Una sesión de la demo

```bash
source .venv/bin/activate
python pulse.py
```

1. El LCD muestra `PIN: 123456` y `Pon tu dedo...`.
2. Al apoyar el dedo empieza la cuenta atrás `Midiendo 4:59`. No te muevas.
3. Al terminar, `Enviando...` y luego `Listo! Ve a web`. El PIN sigue en pantalla.
4. En la web, teclea ese PIN y verás el resultado.

Para otra persona, vuelve a ejecutar `python pulse.py` (cada ejecución es una sesión
con un PIN nuevo).

Otras opciones útiles:

| Comando | Para qué |
|---|---|
| `python pulse.py --segundos 30` | sesión corta para probar el montaje |
| `python pulse.py --simular` | sin sensor: genera una señal sintética de 5 min y la publica. Sirve para probar ThingsBoard + web antes de cablear nada |
| `python pulse.py --simular estres` | simulación con pulso rápido y latidos muy regulares: la web muestra **estrés** |
| `python pulse.py --simular sin_estres` | igual que `--simular`: pulso tranquilo, la web muestra **sin estrés** |
| `python pulse.py --simular mala_calidad` | dedo mal apoyado (IR bajo y ruido): la web muestra **lectura no válida** |
| `python pulse.py --sin-enviar` | captura y guarda en `sesiones/`, sin publicar |
| `python pulse.py --reenviar sesiones/123456.json` | vuelve a publicar una sesión guardada (p. ej. si falló la red) |

## Qué se publica

Una sesión de 5 minutos a 50 Hz son 15 000 muestras, unos **625 KB**. ThingsBoard
Cloud **no acepta un mensaje MQTT de ese tamaño** (comprobado: corta la conexión; en
una instalación propia el límite es `NETTY_MAX_PAYLOAD_SIZE`, 64 KB por defecto). Por
eso la Pi trocea la sesión y la manda en **16 mensajes MQTT con QoS 1**, esperando la
confirmación (PUBACK) de cada uno antes de mandar el siguiente:

1. **15 partes** de 1000 muestras (~42 KB cada una), en la clave `samples_part`, con
   `ts` = ts de la sesión + 1, + 2, ... + 15:
   ```json
   {"ts": 1731200000001, "values": {"samples_part": "[{\"t\":0,\"ir\":110250,\"red\":90310},...]"}}
   ```
2. **La cabecera**, al final, con el `ts` de la sesión (= `finished_at`):
   ```json
   {"ts": 1731200000000, "values": {"pin": "012345", "sample_count": 15000,
     "started_at": 1731199700000, "finished_at": 1731200000000, "parts": 15}}
   ```

La cabecera va la última a propósito: si falla alguna parte no se publica, así que la
web nunca encuentra un PIN con la sesión a medias. La web busca el PIN en las
cabeceras, pide las `parts` partes siguientes y las une en orden antes de analizar.

- `pin` va como **texto** para no perder los ceros a la izquierda.
- `t` son milisegundos desde el inicio (0, 20, 40...); `ir` y `red` son las lecturas
  crudas de 18 bits.

Solo se usa MQTT. Si ThingsBoard no confirma algún mensaje (red caída, token mal),
la sesión no se pierde: se guarda siempre en `sesiones/<PIN>.json` y se reenvía con
`--reenviar`.

## Si se despega el dedo o falla el sensor

- **Dedo fuera a mitad de sesión:** el LCD muestra `Vuelve a poner` y la cuenta atrás
  se pausa. Se descarta el último segundo (el movimiento del dedo) y, al volver a
  apoyarlo, la captura sigue donde iba. Si en `FINGER_TIMEOUT` segundos (20) no vuelve,
  la sesión termina con lo capturado.
- **El sensor deja de responder** (`Errno 121`, normalmente un cable flojo que se movió):
  el LCD muestra `Sensor perdido` y se intenta reconectar durante `SENSOR_TIMEOUT`
  segundos (10). Si vuelve, se espera el dedo y se sigue. Si no, la sesión termina con
  lo capturado.
- **Ctrl+C:** se guarda lo capturado y no se publica.

Una sesión que no llega a los 5 minutos se guarda como `sesiones/<PIN>-incompleta.json`
y se publica igual si tiene al menos `MIN_SESSION_SECONDS` (60 s) de señal; si no, el
LCD muestra `Incompleta` y hay que repetirla.

## Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| `No se detecta el MAX30102` al arrancar | I2C desactivado o cable SDA/SCL mal; revisa `i2cdetect -y 1` |
| `Sensor perdido` a mitad de sesión | un cable o el módulo se mueve al apoyar o quitar el dedo: suelda los pines o fija el sensor con cinta |
| Se queda en `Pon tu dedo...` con el dedo puesto | IR por debajo de `FINGER_IR_MIN`; sube `LED_CURRENT` (0x30, 0x3F) o baja el umbral |
| `[LCD] no disponible` | dirección equivocada (`LCD_ADDRESS=0x3F`) o LCD sin alimentar |
| LCD encendido pero sin texto | gira el potenciómetro de contraste del adaptador |
| `conexión rechazada` por MQTT | token o `TB_HOST` incorrectos (Cloud EU: `eu.thingsboard.cloud`) |
| La web dice "lectura no válida" | movimiento o poca presión del dedo; repite la sesión quieto |
| Pulso raro en la web | algunos módulos traen los LED rojo/IR invertidos: si `red` es mayor que `ir`, avisa |
