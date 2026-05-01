import unittest

from mas004_rpi_databridge import rpiplc_compat


class FakeRpiplcLib:
    def __init__(self):
        self.modes = []
        self.writes = []
        self.read_values = {}

    def initPins(self):
        return 0

    def pinMode(self, pin, mode):
        self.modes.append((pin, mode))
        return 0

    def digitalRead(self, pin):
        return self.read_values.get(pin, 0)

    def digitalWrite(self, pin, value):
        self.writes.append((pin, value))
        self.read_values[pin] = value
        return 0

    def analogRead(self, pin):
        return self.read_values.get(pin, 0)

    def analogWrite(self, pin, value):
        self.writes.append((pin, value))
        self.read_values[pin] = value
        return 0


class RpiplcCompatTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRpiplcLib()
        self.original_load = rpiplc_compat._load_library
        self.original_lib = rpiplc_compat._lib
        self.original_pins = rpiplc_compat._pins
        self.original_model = rpiplc_compat._model
        rpiplc_compat._load_library = lambda: self.fake
        rpiplc_compat._lib = None
        rpiplc_compat._pins = {}
        rpiplc_compat._model = ""

    def tearDown(self):
        rpiplc_compat._load_library = self.original_load
        rpiplc_compat._lib = self.original_lib
        rpiplc_compat._pins = self.original_pins
        rpiplc_compat._model = self.original_model

    def test_rpiplc21_mapping_reads_inputs_and_writes_outputs(self):
        rpiplc_compat.init("RPIPLC_21")

        rpiplc_compat.pin_mode("I0.6", rpiplc_compat.INPUT)
        self.fake.read_values[12] = 1
        self.assertEqual(1, rpiplc_compat.digital_read("I0.6"))

        rpiplc_compat.pin_mode("Q0.0", rpiplc_compat.OUTPUT)
        rpiplc_compat.digital_write("Q0.0", rpiplc_compat.HIGH)
        self.assertIn((0x0000410C, rpiplc_compat.HIGH), self.fake.writes)

    def test_unknown_pin_fails_loudly(self):
        rpiplc_compat.init("RPIPLC_21")

        with self.assertRaises(ValueError):
            rpiplc_compat.digital_read("I9.9")


if __name__ == "__main__":
    unittest.main()
