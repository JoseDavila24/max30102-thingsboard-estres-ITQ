"""Driver mínimo del sensor MAX30102 por I2C (solo lo que usa PulsePPG).

Configuración fija:
  - Modo SpO2: el sensor enciende el LED rojo y el IR y guarda las dos lecturas.
  - 100 muestras/s internas con promedio de 2 -> 50 muestras/s en la FIFO.
  - Pulso de LED de 411 µs (ADC de 18 bits) y rango de 4096 nA.

El sensor guarda las muestras en una FIFO de 32 posiciones (640 ms a 50 Hz),
así que basta con leerla cada pocos milisegundos para no perder ninguna.
"""

from __future__ import annotations

import time

from smbus2 import SMBus

ADDRESS = 0x57
PART_ID = 0x15

REG_INT_STATUS_1 = 0x00
REG_FIFO_WR_PTR = 0x04
REG_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07
REG_FIFO_CONFIG = 0x08
REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA = 0x0C   # LED rojo
REG_LED2_PA = 0x0D   # LED infrarrojo
REG_PART_ID = 0xFF

SAMPLE_RATE_HZ = 50  # muestras por segundo que salen de la FIFO

FIFO_CONFIG = 0b001_1_0000   # promedio de 2 muestras, rollover activado
MODE_SPO2 = 0x03             # rojo + IR
SPO2_CONFIG = 0b0_01_001_11  # rango 4096 nA, 100 muestras/s, pulso 411 µs
LED_CURRENT = 0x24           # ~7 mA; súbelo si el IR con dedo sale bajo

RETRIES = 3          # intentos por operación I2C antes de dar el fallo por bueno
RETRY_DELAY_S = 0.02


class MAX30102:
    def __init__(self, bus: int = 1, address: int = ADDRESS, led_current: int = LED_CURRENT):
        self.bus_number = bus
        self.address = address
        self.led_current = led_current
        self.bus = SMBus(bus)
        self.setup()

    def setup(self) -> None:
        """Comprueba que es un MAX30102, lo reinicia y lo configura."""
        part = self._read(REG_PART_ID)
        if part != PART_ID:
            raise RuntimeError(
                f"En 0x{self.address:02x} no responde un MAX30102 "
                f"(PART_ID=0x{part:02x}, esperado 0x15)"
            )
        self._write(REG_MODE_CONFIG, 0x40)  # reset
        deadline = time.monotonic() + 1
        while self._read(REG_MODE_CONFIG) & 0x40:
            if time.monotonic() > deadline:
                raise RuntimeError("El MAX30102 no termina de reiniciarse")
            time.sleep(0.01)
        self._read(REG_INT_STATUS_1)  # limpia el aviso de encendido
        self._write(REG_FIFO_CONFIG, FIFO_CONFIG)
        self._write(REG_SPO2_CONFIG, SPO2_CONFIG)
        self._write(REG_LED1_PA, self.led_current)
        self._write(REG_LED2_PA, self.led_current)
        self._write(REG_MODE_CONFIG, MODE_SPO2)
        self.clear_fifo()

    def reconnect(self) -> None:
        """Reabre el bus y vuelve a configurar el sensor (tras un fallo de I2C).

        Lanza OSError o RuntimeError si el sensor sigue sin responder.
        """
        try:
            self.bus.close()
        except OSError:
            pass
        self.bus = SMBus(self.bus_number)
        self.setup()

    def _retry(self, op, *args):
        """Repite una operación I2C si falla de forma puntual (Errno 121, 5...).

        Un cable que se mueve o un tirón de corriente pueden hacer que el
        sensor no conteste a una lectura suelta. Si tras RETRIES intentos sigue
        sin contestar, se deja pasar el OSError para que quien llama decida.
        """
        for attempt in range(RETRIES):
            try:
                return op(self.address, *args)
            except OSError:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(RETRY_DELAY_S)

    def _read(self, reg: int) -> int:
        return self._retry(self.bus.read_byte_data, reg)

    def _write(self, reg: int, value: int) -> None:
        self._retry(self.bus.write_byte_data, reg, value)

    def clear_fifo(self) -> None:
        self._write(REG_FIFO_WR_PTR, 0)
        self._write(REG_OVF_COUNTER, 0)
        self._write(REG_FIFO_RD_PTR, 0)

    def read_fifo(self) -> list[tuple[int, int]]:
        """Devuelve [(red, ir), ...] con lo que haya en la FIFO.

        No se usa el contador de desbordamiento (OVF_COUNTER): en los módulos
        probados marca miles de muestras perdidas cuando no se pierde ninguna
        (comprobado con el reloj: 1500 muestras en 29,9 s). Si la Pi se
        retrasa, pulse.py lo detecta comparando muestras con tiempo real.
        """
        wr = self._read(REG_FIFO_WR_PTR)
        rd = self._read(REG_FIFO_RD_PTR)
        pending = (wr - rd) & 0x1F
        out: list[tuple[int, int]] = []
        while pending:
            n = min(pending, 5)  # 5 muestras x 6 bytes = 30 bytes por lectura I2C
            raw = self._retry(self.bus.read_i2c_block_data, REG_FIFO_DATA, n * 6)
            for i in range(n):
                b = raw[i * 6:(i + 1) * 6]
                red = ((b[0] << 16) | (b[1] << 8) | b[2]) & 0x3FFFF
                ir = ((b[3] << 16) | (b[4] << 8) | b[5]) & 0x3FFFF
                out.append((red, ir))
            pending -= n
        return out

    def close(self) -> None:
        """Apaga los LED y cierra el bus. Nunca lanza: si el sensor ya no
        responde, el error importante es el que llevó hasta aquí."""
        try:
            self.bus.write_byte_data(self.address, REG_MODE_CONFIG, 0x80)  # shutdown
        except OSError:
            pass
        try:
            self.bus.close()
        except OSError:
            pass
