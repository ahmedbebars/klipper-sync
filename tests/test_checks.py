"""Tests for the Klipper Sync checks. They need no OrcaSlicer and no printer:

    python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import klipper_sync as ks  # noqa: E402


def orca_values(**changes):
    values = {
        "gcode_flavor": "klipper",
        "machine_max_speed_x": ["300", "200"], "machine_max_speed_y": ["300", "200"],
        "machine_max_speed_z": ["15", "12"],
        "machine_max_acceleration_x": ["3000", "1000"], "machine_max_acceleration_y": ["3000", "1000"],
        "machine_max_acceleration_extruding": ["3000", "1000"],
        "machine_max_acceleration_travel": ["3000", "1000"],
        "machine_max_acceleration_z": ["100", "100"],
        "machine_max_jerk_x": ["5", "9"], "machine_max_jerk_y": ["5", "9"],
        "default_acceleration": ["2500"], "outer_wall_acceleration": ["2000"],
        "sparse_infill_acceleration": ["100%"], "travel_acceleration": ["3000"],
        "printable_area": ["0x0", "235x0", "235x235", "0x235"], "printable_height": "250",
        "nozzle_diameter": ["0.4"], "layer_height": "0.2", "initial_layer_print_height": "0.2",
        "line_width": "0.42", "initial_layer_line_width": "0.5",
        "nozzle_temperature": ["210"], "nozzle_temperature_initial_layer": ["215"],
        "curr_bed_type": "Textured PEI Plate", "textured_plate_temp": ["60"],
        "textured_plate_temp_initial_layer": ["60"],
        "use_firmware_retraction": "0", "exclude_object": "1",
        "machine_start_gcode": "PRINT_START BED=[bed_temperature_initial_layer_single]\nG28",
        "machine_end_gcode": "PRINT_END", "thumbnails": "48x48/PNG,300x300/PNG",
        "outer_wall_speed": ["200"], "travel_speed": ["300"],
    }
    values.update(changes)
    return {"printer": "Test printer", "printer_dirty": False, "process": "p", "values": values}


def klipper(**printer_changes):
    printer = {"kinematics": "cartesian", "max_velocity": 300.0, "max_accel": 3000.0,
               "square_corner_velocity": 5.0, "max_z_velocity": 15.0, "max_z_accel": 100.0}
    printer.update(printer_changes)
    return {
        "ready": True,
        "settings": {
            "printer": printer,
            "extruder": {"nozzle_diameter": 0.4, "max_temp": 300.0, "min_extrude_temp": 170.0,
                         "max_extrude_cross_section": 0.64},
            "heater_bed": {"max_temp": 120.0},
            "gcode_macro print_start": {"gcode": "G28"},
            "gcode_macro print_end": {"gcode": "M84"},
        },
        "toolhead": {"axis_minimum": [0.0, 0.0, -2.0, 0.0], "axis_maximum": [235.0, 235.0, 250.0, 0.0]},
        "objects": ["toolhead", "exclude_object", "gcode_macro PRINT_START", "gcode_macro PRINT_END"],
        "commands": ["BED_MESH_CALIBRATE", "PRINT_START", "PRINT_END", "EXCLUDE_OBJECT_DEFINE"],
    }


def by_id(items):
    return {item["id"]: item for item in items}


class ChecksTest(unittest.TestCase):
    def test_matching_profile_is_all_ok(self):
        items = ks.build_report(orca_values(), klipper())
        bad = [(i["id"], i["level"], i["detail"]) for i in items if i["level"] != "ok"]
        self.assertEqual(bad, [])

    def test_acceleration_above_klipper_is_an_error(self):
        snap = orca_values(machine_max_acceleration_x=["20000", "20000"],
                           machine_max_acceleration_y=["20000", "20000"],
                           machine_max_acceleration_extruding=["20000", "20000"],
                           default_acceleration=["5000"])
        item = by_id(ks.build_report(snap, klipper()))["accel"]
        self.assertEqual(item["level"], "error")
        self.assertIn("Normal printing 5000", item["detail"])

    def test_orca_caps_acceleration_so_no_error(self):
        snap = orca_values(default_acceleration=["5000"])  # machine limit 3000 caps it
        self.assertEqual(by_id(ks.build_report(snap, klipper()))["accel"]["level"], "ok")

    def test_build_height_above_z_travel(self):
        item = by_id(ks.build_report(orca_values(printable_height="300"), klipper()))["height"]
        self.assertEqual(item["level"], "error")
        self.assertEqual(ks.serialize_fix(item["fixes"][0], {}), "250")

    def test_bed_outside_travel(self):
        snap = orca_values(printable_area=["0x0", "250x0", "250x235", "0x235"])
        self.assertEqual(by_id(ks.build_report(snap, klipper()))["bed"]["level"], "error")

    def test_unknown_start_macro(self):
        snap = orca_values(machine_start_gcode="START_PRINT EXTRUDER=200\nG28")
        item = by_id(ks.build_report(snap, klipper()))["gcode"]
        self.assertEqual(item["level"], "error")
        self.assertIn("START_PRINT", item["orca"])

    def test_firmware_retraction_without_section(self):
        snap = orca_values(use_firmware_retraction="1")
        self.assertEqual(by_id(ks.build_report(snap, klipper()))["fwretract"]["level"], "error")

    def test_temperature_limits(self):
        hot = orca_values(nozzle_temperature=["305"])
        self.assertEqual(by_id(ks.build_report(hot, klipper()))["hotend"]["level"], "error")
        tight = orca_values(nozzle_temperature=["175"], nozzle_temperature_initial_layer=["175"])
        self.assertEqual(by_id(ks.build_report(tight, klipper()))["hotend"]["level"], "warn")

    def test_cross_section(self):
        snap = orca_values(layer_height="0.6", line_width="1.2", initial_layer_line_width="0")
        self.assertEqual(by_id(ks.build_report(snap, klipper()))["cross"]["level"], "error")

    def test_jerk_fix_keeps_silent_mode(self):
        snap = orca_values(machine_max_jerk_x=["8", "9", "8", "9"], machine_max_jerk_y=["8", "9", "8", "9"])
        item = by_id(ks.build_report(snap, klipper()))["scv"]
        self.assertEqual(item["level"], "warn")
        self.assertEqual(ks.serialize_fix(item["fixes"][0], snap["values"]), "5,9,5,9")

    def test_serialized_strings_from_orca(self):
        snap = orca_values(machine_max_speed_x="300,200", machine_max_speed_y="300,200",
                           printable_area="0x0,235x0,235x235,0x235")
        found = by_id(ks.build_report(snap, klipper()))
        self.assertEqual(found["speed"]["level"], "ok")
        self.assertEqual(found["bed"]["level"], "ok")

    def test_extended_commands(self):
        text = "; comment\n{if x}\nPRINT_START A=1\nG1 X1\nM600\nset_fan_speed FAN=x\n[foo]"
        self.assertEqual(ks.extended_commands(text), ["PRINT_START", "M600", "SET_FAN_SPEED"])

    def test_moonraker_address_parsing(self):
        m = ks.Moonraker("http://printer.local:7125/")
        self.assertEqual((m.host, m.port, m.prefix), ("printer.local", 7125, ""))
        m = ks.Moonraker("192.168.1.20")
        self.assertEqual((m.scheme, m.host, m.port), ("http", "192.168.1.20", None))
        with self.assertRaises(ks.MoonrakerError):
            ks.Moonraker("")

    def test_other_addresses_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "addresses.json")
            self.assertEqual(ks.load_addresses(path), {})  # no file yet
            ks.save_addresses({"Big printer": "100.64.0.12", "Mini": ""}, path)
            self.assertEqual(ks.load_addresses(path), {"Big printer": "100.64.0.12"})
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json")
            self.assertEqual(ks.load_addresses(path), {})  # a broken file is ignored


if __name__ == "__main__":
    unittest.main()
