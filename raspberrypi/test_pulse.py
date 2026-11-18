"""Pruebas de pulse.py sin hardware: un sensor falso simula dedo fuera y
fallos de I2C. Se ejecutan en la Pi o en cualquier PC:

    python -m unittest test_pulse -v
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import pulse

FINGER_IR, NO_FINGER_IR = 110_000, 5_000


class FakeSensor:
    """Entrega 5 muestras por lectura. `finger(i)` dice si hay dedo en la
    muestra i; `fails` son los números de lectura que lanzan Errno 121;
    `reconnect_fails` cuántas reconexiones fallan antes de volver."""

    def __init__(self, finger=lambda i: True, fails=(), reconnect_fails=0):
        self.finger, self.fails, self.reconnect_fails = finger, set(fails), reconnect_fails
        self.i = self.reads = self.reconnects = 0

    def read_fifo(self):
        self.reads += 1
        if self.reads in self.fails:
            raise OSError(121, "Remote I/O error")
        out = []
        for _ in range(5):
            ir = FINGER_IR if self.finger(self.i) else NO_FINGER_IR
            out.append((ir - 20_000, ir))
            self.i += 1
        return out

    def reconnect(self):
        self.reconnects += 1
        if self.reconnect_fails is None or self.reconnects <= self.reconnect_fails:
            raise OSError(121, "Remote I/O error")

    def clear_fifo(self):
        pass


class Silent(pulse.Display):
    def __init__(self):
        super().__init__(None)
        self.lines = []

    def show(self, line1, line2="", echo=True):
        self.lines.append(line2)


def run(sensor, seconds=4, finger_timeout=0.3, sensor_timeout=0.3):
    cap = pulse.Captura()
    display = Silent()
    with mock.patch("pulse.time.sleep"), mock.patch("builtins.print"):
        pulse.capture(sensor, display, "012345", seconds, cap, 50_000,
                      finger_timeout, sensor_timeout)
    return cap, display


class CaptureTests(unittest.TestCase):
    def test_sesion_normal(self):
        cap, _ = run(FakeSensor())
        self.assertIsNone(cap.motivo)
        self.assertEqual(len(cap.samples), 4 * pulse.SAMPLE_RATE_HZ)
        self.assertEqual([s["t"] for s in cap.samples[:3]], [0, 20, 40])

    def test_dedo_fuera_y_vuelve(self):
        # Sin dedo entre las muestras 100 y 300: pausa y luego completa.
        cap, display = run(FakeSensor(finger=lambda i: not 100 <= i < 300))
        self.assertIsNone(cap.motivo)
        self.assertEqual(cap.pausas, 1)
        self.assertIn("Vuelve a poner", display.lines)
        self.assertEqual(len(cap.samples), 200)
        self.assertTrue(all(s["ir"] == FINGER_IR for s in cap.samples))
        self.assertEqual(cap.samples[-1]["t"], 199 * pulse.PERIOD_MS)

    def test_dedo_no_vuelve(self):
        cap, _ = run(FakeSensor(finger=lambda i: i < 150))
        self.assertEqual(cap.motivo, "dedo")
        self.assertTrue(0 < len(cap.samples) < 150)

    def test_fallo_i2c_puntual_se_reconecta(self):
        cap, display = run(FakeSensor(fails={10}, reconnect_fails=1), sensor_timeout=5)
        self.assertIsNone(cap.motivo)
        self.assertIn("Sensor perdido", display.lines)
        self.assertEqual(len(cap.samples), 200)

    def test_sensor_no_vuelve(self):
        sensor = FakeSensor(fails={20}, reconnect_fails=None)
        cap, _ = run(sensor)  # no debe lanzar excepción
        self.assertEqual(cap.motivo, "sensor")
        self.assertGreater(len(cap.samples), 0)

    def test_ctrl_c_conserva_lo_capturado(self):
        sensor = FakeSensor()
        original = sensor.read_fifo

        def read():
            if sensor.reads == 30:
                raise KeyboardInterrupt
            return original()

        sensor.read_fifo = read
        cap = pulse.Captura()
        with self.assertRaises(KeyboardInterrupt), mock.patch("pulse.time.sleep"), \
                mock.patch("builtins.print"):
            pulse.capture(sensor, Silent(), "012345", 4, cap, 50_000, 0.3, 0.3)
        self.assertGreater(len(cap.samples), 0)
        self.assertGreater(cap.finished_at, 0)


class RateTests(unittest.TestCase):
    def test_avisa_si_la_pi_lee_lento(self):
        # Cada lectura tarda 0,3 s y trae 5 muestras: ~17 muestras/s en vez de 50.
        real_sleep = pulse.time.sleep
        with mock.patch("pulse.time.sleep", lambda _: real_sleep(0.3)), \
                mock.patch("builtins.print") as out:
            pulse.capture(FakeSensor(), Silent(), "012345", 1, pulse.Captura(), 50_000, 1, 1)
        self.assertTrue(any("AVISO" in str(c) for c in out.call_args_list))

    def test_no_avisa_a_ritmo_normal(self):
        with mock.patch("pulse.time.sleep"), mock.patch("builtins.print") as out:
            pulse.capture(FakeSensor(), Silent(), "012345", 4, pulse.Captura(), 50_000, 1, 1)
        self.assertFalse(any("AVISO" in str(c) for c in out.call_args_list))


class MessageTests(unittest.TestCase):
    def test_sesion_completa_son_16_mensajes(self):
        samples, started, finished = pulse.simulate(300)
        messages = pulse.split_message(pulse.build_message("012345", samples, started, finished))
        self.assertEqual(len(messages), 16)
        self.assertTrue(all(len(m) < 64 * 1024 for m in messages))
        self.assertEqual(json.loads(messages[-1])["values"]["parts"], 15)


if __name__ == "__main__":
    unittest.main()
