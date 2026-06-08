import importlib.util
import pathlib
import unittest
from importlib.machinery import SourceFileLoader

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "rootfs/usr/local/bin/unifi-protect-exporter"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "unifi_protect_exporter", SCRIPT,
        loader=SourceFileLoader("unifi_protect_exporter", str(SCRIPT)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE = [
    {
        "id": "68cbf99d02732c03e443099b",
        "modelKey": "sensor",
        "state": "CONNECTED",
        "name": "Server Room",
        "batteryStatus": {"percentage": 95, "isLow": False},
        "stats": {
            "light": {"value": 25, "status": "neutral"},
            "humidity": {"value": 44, "status": "neutral"},
            "temperature": {"value": 26.7, "status": "neutral"},
        },
        "wirelessConnectionState": {"signalState": {"signalQuality": 88, "signalStrength": -66}},
    },
    {
        "id": "68868e9f01b1cc03e4001c11",
        "modelKey": "sensor",
        "state": "CONNECTED",
        "name": "Front Door",
        "batteryStatus": {"percentage": 85, "isLow": False},
        "stats": {
            "light": {"value": None, "status": "unknown"},
            "humidity": {"value": None, "status": "unknown"},
            "temperature": {"value": 22.58, "status": "neutral"},
        },
        "wirelessConnectionState": {"signalState": {"signalQuality": 80, "signalStrength": -58}},
    },
]


class DecodeSensors(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.samples = self.mod.decode_sensors(SAMPLE)

    def _find(self, metric, name):
        return [s for s in self.samples if s["metric"] == metric and s["labels"]["name"] == name]

    def test_temperature_celsius_emitted_for_both(self):
        self.assertEqual(self._find("unifi_sensor_temperature_celsius", "Server Room")[0]["value"], 26.7)
        self.assertEqual(self._find("unifi_sensor_temperature_celsius", "Front Door")[0]["value"], 22.58)

    def test_humidity_skipped_when_null(self):
        self.assertEqual(len(self._find("unifi_sensor_humidity_percent", "Server Room")), 1)
        self.assertEqual(len(self._find("unifi_sensor_humidity_percent", "Front Door")), 0)

    def test_light_skipped_when_null(self):
        self.assertEqual(self._find("unifi_sensor_light_lux", "Server Room")[0]["value"], 25.0)
        self.assertEqual(len(self._find("unifi_sensor_light_lux", "Front Door")), 0)

    def test_battery_and_signal(self):
        self.assertEqual(self._find("unifi_sensor_battery_percent", "Server Room")[0]["value"], 95.0)
        self.assertEqual(self._find("unifi_sensor_signal_dbm", "Server Room")[0]["value"], -66.0)

    def test_connected_flag(self):
        self.assertEqual(self._find("unifi_sensor_connected", "Server Room")[0]["value"], 1.0)

    def test_empty_payload_is_empty(self):
        self.assertEqual(self.mod.decode_sensors([]), [])
        self.assertEqual(self.mod.decode_sensors(None), [])


if __name__ == "__main__":
    unittest.main()
