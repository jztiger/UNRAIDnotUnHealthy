import importlib.util
import pathlib
import unittest
from importlib.machinery import SourceFileLoader

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "rootfs/usr/local/bin/acinfinity-exporter"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "acinfinity_exporter", SCRIPT,
        loader=SourceFileLoader("acinfinity_exporter", str(SCRIPT)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Synthetic fixture matching the documented devInfoListAll schema. Values are
# x100 for temperature/humidity/vpd; port "speak" is the 0-10 current power.
SAMPLE = [
    {
        "devName": "Closet",
        "devId": "12345",
        "deviceInfo": {
            "temperature": 2670,
            "humidity": 4400,
            "vpdnums": 152,
            "ports": [
                {"port": 1, "portName": "Exhaust Fan", "speak": 6, "online": 1},
                {"port": 2, "portName": "Empty", "speak": 0, "online": 0},
            ],
        },
    }
]


class DecodeDevices(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.samples = self.mod.decode_devices(SAMPLE)

    def _find(self, metric, **labels):
        out = []
        for s in self.samples:
            if s["metric"] != metric:
                continue
            if all(s["labels"].get(k) == v for k, v in labels.items()):
                out.append(s)
        return out

    def test_temperature_scaled_to_celsius(self):
        self.assertAlmostEqual(self._find("acinfinity_temperature_celsius", device="Closet")[0]["value"], 26.70)

    def test_humidity_scaled(self):
        self.assertAlmostEqual(self._find("acinfinity_humidity_percent", device="Closet")[0]["value"], 44.00)

    def test_vpd_scaled(self):
        self.assertAlmostEqual(self._find("acinfinity_vpd_kpa", device="Closet")[0]["value"], 1.52)

    def test_fan_power_per_port(self):
        self.assertEqual(self._find("acinfinity_fan_power", device="Closet", port="1")[0]["value"], 6.0)
        self.assertEqual(self._find("acinfinity_fan_power", device="Closet", port="2")[0]["value"], 0.0)

    def test_port_online(self):
        self.assertEqual(self._find("acinfinity_port_online", device="Closet", port="1")[0]["value"], 1.0)

    def test_empty_payload_is_empty(self):
        self.assertEqual(self.mod.decode_devices([]), [])
        self.assertEqual(self.mod.decode_devices(None), [])


if __name__ == "__main__":
    unittest.main()
