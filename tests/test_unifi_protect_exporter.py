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
    {
        "id": "deadbeef00000000",
        "modelKey": "sensor",
        "state": "DISCONNECTED",
        "name": "Server Room",
        "batteryStatus": {"percentage": 50, "isLow": True},
        "stats": {"light": {"value": None}, "humidity": {"value": None}, "temperature": {"value": None}},
        "wirelessConnectionState": {"signalState": {"signalStrength": -90}},
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

    def test_disconnected_sensor_connected_is_zero(self):
        # Third fixture sensor is DISCONNECTED; its connected value must be 0.0
        disconnected = [
            s for s in self.samples
            if s["metric"] == "unifi_sensor_connected" and s["labels"]["id"] == "deadbeef00000000"
        ]
        self.assertEqual(len(disconnected), 1)
        self.assertEqual(disconnected[0]["value"], 0.0)

    def test_duplicate_name_sensors_have_distinct_id_labels(self):
        # Two sensors share name "Server Room" but must have different id labels
        connected_samples = [
            s for s in self.samples if s["metric"] == "unifi_sensor_connected"
            and s["labels"]["name"] == "Server Room"
        ]
        self.assertEqual(len(connected_samples), 2)
        ids = [s["labels"]["id"] for s in connected_samples]
        self.assertEqual(len(set(ids)), 2, "duplicate-label collision: both Server Room sensors share the same id label")

    def test_label_includes_id(self):
        # Every sample must carry an 'id' label
        for s in self.samples:
            self.assertIn("id", s["labels"], f"Missing id label in sample: {s}")

    def test_escape_double_quote(self):
        self.assertEqual(self.mod._escape('a"b'), 'a\\"b')


class RenderMetrics(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_render_groups_metric_families(self):
        mod = self.mod
        with mod._lock:
            mod._state["samples"] = mod.decode_sensors(SAMPLE)
            mod._state["ok"] = True
            mod._state["last_success"] = 1.0
        text = mod._render_metrics().decode()
        # Every metric family appears as exactly one contiguous run of lines
        for metric in ["unifi_sensor_temperature_celsius", "unifi_sensor_battery_percent"]:
            idxs = [i for i, ln in enumerate(text.splitlines())
                    if ln.startswith(metric + "{")]
            self.assertTrue(idxs, f"No lines found for {metric}")
            self.assertEqual(
                idxs, list(range(idxs[0], idxs[0] + len(idxs))),
                f"{metric} not contiguous in output",
            )


if __name__ == "__main__":
    unittest.main()
