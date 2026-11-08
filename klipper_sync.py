# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Klipper Sync"
# description = "Checks the selected printer profile against the real Klipper printer over Moonraker, flags the settings that stop prints or skew time estimates, and fixes them in one click."
# author = "Ahmed Bebars"
# version = "0.1.0"
# ///
"""Klipper Sync for OrcaSlicer.

Run "Klipper Sync" from the Plugins window or the Speed Dial (press Space). A panel opens beside
the 3D view. It reads the selected printer, process and filament presets, asks the printer's
Moonraker (the print host set in the printer preset) for Klipper's real configuration, and lists
what does not match:

  * machine limits (speed, acceleration, square corner velocity) that make the time estimate
    wrong or let a print run faster than printer.cfg allows,
  * bed size, build height and nozzle that can stop a print with "Move out of range",
  * temperatures outside Klipper's min_extrude_temp / max_temp,
  * line width x layer height above max_extrude_cross_section,
  * G10/G11 retraction without [firmware_retraction], object labels without [exclude_object],
  * custom G-code commands (PRINT_START and friends) that Klipper does not know.

"Fix" writes Klipper's values into the preset. Nothing is saved until you save the preset in
OrcaSlicer as usual.

The plugin only reads from the printer (HTTP GET). It never sends G-code or changes the
printer's configuration.
"""

import http.client
import json
import os
import queue
import re
import time
import urllib.parse

try:
    import orca
except ImportError:  # running the checks outside OrcaSlicer (tests)
    orca = None

VERSION = "0.1.0"
HTTP_TIMEOUT = 6.0
PERMISSION_WAIT = 90.0  # seconds to keep retrying while OrcaSlicer's permission prompt is open

# --------------------------------------------------------------------------------------------
# Settings read from OrcaSlicer
# --------------------------------------------------------------------------------------------

MACHINE_LIMIT_KEYS = [
    "machine_max_speed_x", "machine_max_speed_y", "machine_max_speed_z",
    "machine_max_acceleration_x", "machine_max_acceleration_y", "machine_max_acceleration_z",
    "machine_max_acceleration_extruding", "machine_max_acceleration_travel",
    "machine_max_jerk_x", "machine_max_jerk_y",
]

PROCESS_ACCELS = [
    # key, label, percent-of key (for FloatsOrPercents options)
    ("default_acceleration", "Normal printing", None),
    ("outer_wall_acceleration", "Outer wall", None),
    ("inner_wall_acceleration", "Inner wall", None),
    ("top_surface_acceleration", "Top surface", None),
    ("initial_layer_acceleration", "First layer", None),
    ("sparse_infill_acceleration", "Sparse infill", "default_acceleration"),
    ("internal_solid_infill_acceleration", "Internal solid infill", "default_acceleration"),
    ("bridge_acceleration", "Bridge", "default_acceleration"),
]
TRAVEL_ACCELS = [
    ("travel_acceleration", "Travel", None),
    ("initial_layer_travel_acceleration", "First layer travel", "travel_acceleration"),
]

PROCESS_SPEEDS = [
    ("outer_wall_speed", "Outer wall"),
    ("inner_wall_speed", "Inner wall"),
    ("sparse_infill_speed", "Sparse infill"),
    ("internal_solid_infill_speed", "Internal solid infill"),
    ("top_surface_speed", "Top surface"),
    ("gap_infill_speed", "Gap infill"),
    ("support_speed", "Support"),
    ("support_interface_speed", "Support interface"),
    ("bridge_speed", "Bridge"),
    ("travel_speed", "Travel"),
]

LINE_WIDTHS = [
    ("outer_wall_line_width", "Outer wall"),
    ("inner_wall_line_width", "Inner wall"),
    ("sparse_infill_line_width", "Sparse infill"),
    ("internal_solid_infill_line_width", "Internal solid infill"),
    ("top_surface_line_width", "Top surface"),
    ("support_line_width", "Support"),
]

PLATE_TEMP_KEYS = {
    "cool plate": "cool_plate_temp",
    "engineering plate": "eng_plate_temp",
    "high temp plate": "hot_plate_temp",
    "textured pei plate": "textured_plate_temp",
    "textured cool plate": "textured_cool_plate_temp",
    "supertack plate": "supertack_plate_temp",
}

GCODE_KEYS = [
    ("machine_start_gcode", "Machine start G-code"),
    ("machine_end_gcode", "Machine end G-code"),
    ("before_layer_change_gcode", "Before layer change G-code"),
    ("layer_change_gcode", "Layer change G-code"),
    ("change_filament_gcode", "Change filament G-code"),
    ("machine_pause_gcode", "Pause G-code"),
    ("time_lapse_gcode", "Timelapse G-code"),
    ("template_custom_gcode", "Template custom G-code"),
    ("printing_by_object_gcode", "Between objects G-code"),
    ("filament_start_gcode", "Filament start G-code"),
    ("filament_end_gcode", "Filament end G-code"),
]

# M-codes that Klipper only understands when a macro of that name exists.
MACRO_ONLY_MCODES = {"M600", "M601", "M602", "M125", "M701", "M702", "M900"}

SNAPSHOT_KEYS = sorted(set(
    ["print_host", "printhost_apikey", "host_type", "gcode_flavor", "printable_area",
     "printable_height", "nozzle_diameter", "use_firmware_retraction", "default_bed_type",
     "curr_bed_type", "exclude_object", "layer_height", "initial_layer_print_height",
     "initial_layer_height", "line_width", "initial_layer_line_width", "default_jerk",
     "nozzle_temperature", "nozzle_temperature_initial_layer", "thumbnails"]
    + MACHINE_LIMIT_KEYS
    + [k for k, _, _ in PROCESS_ACCELS + TRAVEL_ACCELS]
    + [k for k, _ in PROCESS_SPEEDS + LINE_WIDTHS + GCODE_KEYS]
    + [v for v in PLATE_TEMP_KEYS.values()]
    + [v + "_initial_layer" for v in PLATE_TEMP_KEYS.values()]
))


# --------------------------------------------------------------------------------------------
# Small value helpers. OrcaSlicer may hand values over as Python lists or as serialized strings
# ("300,200", "0x0,235x0,..."), so everything goes through these.
# --------------------------------------------------------------------------------------------

def plain(value):
    """Turn a host value into plain Python data (str, int, float, bool, list)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return str(value)


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value]
    text = str(value).strip()
    if not text:
        return []
    sep = ";" if ";" in text else ","
    return [p.strip().strip('"') for p in text.split(sep)]


def num(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("%").strip()
    try:
        return float(text)
    except ValueError:
        return None


def first(values, key):
    items = as_list(values.get(key))
    return num(items[0]) if items else None


def first_raw(values, key):
    items = as_list(values.get(key))
    return items[0] if items else None


def resolve_percent(raw, base):
    """'50%' of base, or a plain number. None when unknown."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text.endswith("%"):
        pct = num(text)
        return None if pct is None or base is None else base * pct / 100.0
    return num(text)


def truthy(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def fmt(value):
    if value is None:
        return "not set"
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-6:
            return str(int(round(value)))
        return ("%.3f" % value).rstrip("0").rstrip(".")
    return str(value)


def same(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol


def gcode_text(value):
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return "" if value is None else str(value)


def area_bbox(value):
    """Bounding box (min_x, min_y, max_x, max_y) of OrcaSlicer's printable_area."""
    xs, ys = [], []
    for point in as_list(value):
        nums = re.findall(r"-?\d+(?:\.\d+)?", str(point))
        if len(nums) >= 2:
            xs.append(float(nums[0]))
            ys.append(float(nums[1]))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------------------------
# Moonraker
# --------------------------------------------------------------------------------------------

class MoonrakerError(Exception):
    pass


class Moonraker:
    """Minimal read-only Moonraker client on http.client (standard library only)."""

    def __init__(self, print_host, api_key="", timeout=HTTP_TIMEOUT):
        url = (print_host or "").strip()
        if not url:
            raise MoonrakerError("The printer preset has no print host. Set the printer's "
                                 "address in the printer settings (the Wi-Fi icon next to the "
                                 "printer name).")
        if "://" not in url:
            url = "http://" + url
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            raise MoonrakerError("Can't read the print host address %r." % print_host)
        self.scheme = (parts.scheme or "http").lower()
        self.host = parts.hostname
        self.port = parts.port
        self.prefix = parts.path.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    @property
    def label(self):
        return self.host + (":%d" % self.port if self.port else "")

    def get(self, path):
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout)
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        try:
            conn.request("GET", self.prefix + path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        except PermissionError:
            raise
        except OSError as error:
            raise MoonrakerError("Can't reach %s (%s). Is the printer on, and is the print "
                                 "host address in the printer preset right?" % (self.label, error))
        finally:
            conn.close()
        if resp.status in (401, 403):
            raise MoonrakerError("Moonraker on %s refused the request (HTTP %d). Add this "
                                 "computer to [authorization] trusted_clients in "
                                 "moonraker.conf, or put an API key in the printer preset."
                                 % (self.label, resp.status))
        if resp.status == 404 and path.startswith("/server/info"):
            raise MoonrakerError("%s answered, but it isn't Moonraker. Klipper Sync needs "
                                 "the Moonraker address (often port 7125, or 80 behind "
                                 "Mainsail/Fluidd)." % self.label)
        if resp.status != 200:
            raise MoonrakerError("Moonraker on %s answered HTTP %d for %s."
                                 % (self.label, resp.status, path))
        try:
            return json.loads(body.decode("utf-8"))["result"]
        except (ValueError, KeyError):
            raise MoonrakerError("%s answered something that isn't Moonraker JSON." % self.label)

    def fetch_all(self):
        server = self.get("/server/info")
        info = self.get("/printer/info")
        data = {"server": server, "info": info, "ready": info.get("state") == "ready"}
        if not data["ready"]:
            return data
        objects = self.get("/printer/objects/list").get("objects", [])
        query = self.get("/printer/objects/query?configfile=settings&toolhead&print_stats"
                         "&virtual_sdcard")
        status = query.get("status", {})
        data.update({
            "objects": objects,
            "settings": status.get("configfile", {}).get("settings", {}),
            "toolhead": status.get("toolhead", {}),
            "print_stats": status.get("print_stats", {}),
            "virtual_sdcard": status.get("virtual_sdcard", {}),
            "commands": sorted(self.get("/printer/gcode/help").keys()),
        })
        return data


# --------------------------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------------------------

LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2, "ok": 3}


def known_commands(kl):
    known = {c.upper() for c in kl.get("commands", [])}
    for name in kl.get("objects", []):
        if name.lower().startswith("gcode_macro "):
            known.add(name.split(" ", 1)[1].strip().upper())
    return known


def macro_uses(settings, word):
    """True when any [gcode_macro] body contains word (case-insensitive)."""
    word = word.lower()
    for section, options in settings.items():
        if section.startswith("gcode_macro ") and isinstance(options, dict):
            if word in str(options.get("gcode", "")).lower():
                return True
    return False


def extended_commands(text):
    """First word of every G-code line that isn't a plain G/M/T code, plus macro-only M-codes."""
    found = []
    for line in text.splitlines():
        line = line.split(";", 1)[0].strip()
        if not line or line[0] in "{[":
            continue
        word = line.split()[0]
        upper = word.upper()
        if re.fullmatch(r"[GMTD]\d+(\.\d+)?", upper):
            if upper in MACRO_ONLY_MCODES:
                found.append(upper)
            continue
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", upper) and any(c.isalpha() for c in upper):
            found.append(upper)
    return found


def build_report(snap, kl):
    """Compare an OrcaSlicer snapshot with Moonraker data. Returns a list of items."""
    v = snap["values"]
    items = []

    def add(item_id, level, title, orca_value, klipper_value, detail="", fixes=None):
        items.append({
            "id": item_id, "level": level, "title": title,
            "orca": orca_value, "klipper": klipper_value, "detail": detail,
            "fixes": fixes or [],
        })

    settings = kl.get("settings", {})
    printer = settings.get("printer", {})
    extruder = settings.get("extruder", {})
    bed = settings.get("heater_bed", {})
    toolhead = kl.get("toolhead", {})
    objects = {o.lower() for o in kl.get("objects", [])}

    # --- G-code flavor ------------------------------------------------------------------------
    flavor = str(v.get("gcode_flavor") or "").strip().lower()
    if flavor and flavor != "klipper":
        add("flavor", "warn", "G-code flavor isn't Klipper", flavor, "klipper",
            "Orca writes commands for %s and estimates print time with that firmware's "
            "planner. With the Klipper flavor, Orca sets accelerations with "
            "SET_VELOCITY_LIMIT and uses Klipper's own planner for the time estimate." % flavor,
            [fix("printer", "gcode_flavor", "scalar", "klipper")])
    elif flavor:
        add("flavor", "ok", "G-code flavor", "klipper", "klipper")

    # --- Speed --------------------------------------------------------------------------------
    k_vel = num(printer.get("max_velocity"))
    o_vx, o_vy = first(v, "machine_max_speed_x"), first(v, "machine_max_speed_y")
    if k_vel is not None and o_vx is not None:
        orca_txt = fmt(o_vx) if same(o_vx, o_vy) else "X %s, Y %s" % (fmt(o_vx), fmt(o_vy))
        if same(o_vx, k_vel) and same(o_vy, k_vel):
            add("speed", "ok", "Maximum speed", orca_txt + " mm/s", fmt(k_vel) + " mm/s")
        else:
            higher = max(o_vx or 0, o_vy or 0) > k_vel
            add("speed", "warn" if higher else "info", "Maximum speed",
                orca_txt + " mm/s", fmt(k_vel) + " mm/s",
                ("Klipper caps every move at max_velocity %s mm/s, so moves Orca plans faster "
                 "than that take longer than the estimate says." % fmt(k_vel)) if higher else
                ("Orca's limit is lower than Klipper's, so the time estimate assumes slower "
                 "moves than the printer makes."),
                [fix("printer", "machine_max_speed_x", "even", k_vel),
                 fix("printer", "machine_max_speed_y", "even", k_vel)])

    # --- Acceleration -------------------------------------------------------------------------
    k_acc = num(printer.get("max_accel"))
    o_ax, o_ay = first(v, "machine_max_acceleration_x"), first(v, "machine_max_acceleration_y")
    o_aext = first(v, "machine_max_acceleration_extruding")
    o_atr = first(v, "machine_max_acceleration_travel")
    if k_acc is not None and None not in (o_ax, o_ay, o_aext):
        # OrcaSlicer caps the accelerations it writes for Klipper at the lowest of the
        # extruding and X/Y limits (and the travel limit for travel moves).
        print_cap = min(x for x in (o_ax, o_ay, o_aext) if x > 0) if any(
            x > 0 for x in (o_ax, o_ay, o_aext)) else None
        travel_cap = o_atr if o_atr and o_atr > 0 else print_cap
        over = []
        default_acc = first(v, "default_acceleration")
        for key, label, base_key in PROCESS_ACCELS + TRAVEL_ACCELS:
            base = first(v, base_key) if base_key else None
            if base_key == "default_acceleration":
                base = default_acc
            val = resolve_percent(first_raw(v, key), base)
            if not val:
                continue
            cap = travel_cap if (key, label, base_key) in TRAVEL_ACCELS else print_cap
            effective = min(val, cap) if cap else val
            if effective > k_acc + 0.5:
                over.append("%s %s" % (label, fmt(effective)))
        limits = [o_ax, o_ay, o_aext] + ([o_atr] if o_atr is not None else [])
        orca_txt = fmt(o_ax) if all(same(x, o_ax) for x in limits) else ", ".join(
            "%s %s" % (n, fmt(x)) for n, x in zip(("X", "Y", "extruding", "travel"), limits))
        fixes = [fix("printer", "machine_max_acceleration_x", "even", k_acc),
                 fix("printer", "machine_max_acceleration_y", "even", k_acc),
                 fix("printer", "machine_max_acceleration_extruding", "even", k_acc)]
        if o_atr is not None:
            fixes.append(fix("printer", "machine_max_acceleration_travel", "even", k_acc))
        if all(same(x, k_acc) for x in limits):
            add("accel", "ok", "Maximum acceleration", orca_txt + " mm/s²",
                fmt(k_acc) + " mm/s²")
        elif over:
            add("accel", "error", "Prints faster than printer.cfg allows",
                orca_txt + " mm/s²", fmt(k_acc) + " mm/s²",
                "This process asks for %s mm/s², above Klipper's max_accel. Klipper takes "
                "accelerations sent in G-code as they are, so the printer really runs at these "
                "values. Setting Orca's machine limits to Klipper's makes Orca cap them."
                % "; ".join(over), fixes)
        else:
            named = list(zip(("X", "Y", "extruding", "travel"), limits))
            higher = ["%s %s" % (n, fmt(x)) for n, x in named if x > k_acc + 0.5]
            lower = ["%s %s" % (n, fmt(x)) for n, x in named if 0 < x < k_acc - 0.5]
            if higher:
                detail = ("Orca's limit (%s) is above Klipper's max_accel. This process stays "
                          "under %s today, but a faster process would run above it, because "
                          "Klipper takes accelerations sent in G-code as they are."
                          % (", ".join(higher), fmt(k_acc)))
            else:
                detail = ("Orca caps %s below Klipper's %s, so those moves run slower than "
                          "printer.cfg allows. Keep it if you lowered it on purpose."
                          % (", ".join(lower), fmt(k_acc)))
            add("accel", "warn" if higher else "info", "Maximum acceleration",
                orca_txt + " mm/s²", fmt(k_acc) + " mm/s²", detail, fixes)

    # --- Square corner velocity (jerk) --------------------------------------------------------
    k_scv = num(printer.get("square_corner_velocity"))
    o_jx, o_jy = first(v, "machine_max_jerk_x"), first(v, "machine_max_jerk_y")
    if k_scv is not None and o_jx is not None:
        orca_txt = fmt(o_jx) if same(o_jx, o_jy) else "X %s, Y %s" % (fmt(o_jx), fmt(o_jy))
        jerk_note = ""
        d_jerk = first(v, "default_jerk")
        if d_jerk:
            jerk_note = (" This process also sets jerk to %s, which Orca sends to Klipper as "
                         "SQUARE_CORNER_VELOCITY (capped at the machine limit)." % fmt(d_jerk))
        if same(o_jx, k_scv) and same(o_jy, k_scv):
            add("scv", "ok", "Square corner velocity (jerk)", orca_txt + " mm/s",
                fmt(k_scv) + " mm/s", jerk_note.strip())
        else:
            add("scv", "warn", "Square corner velocity (jerk)", orca_txt + " mm/s",
                fmt(k_scv) + " mm/s",
                ("For Klipper, Orca uses the machine jerk X/Y as square_corner_velocity when "
                 "it estimates print time%s." % (
                     ". At 0 it assumes the head stops at every corner, so estimates run long"
                     if not o_jx else "")) + jerk_note,
                [fix("printer", "machine_max_jerk_x", "even", k_scv),
                 fix("printer", "machine_max_jerk_y", "even", k_scv)])

    # --- Z limits -----------------------------------------------------------------------------
    k_vz = num(printer.get("max_z_velocity")) or k_vel
    k_az = num(printer.get("max_z_accel")) or k_acc
    o_vz, o_az = first(v, "machine_max_speed_z"), first(v, "machine_max_acceleration_z")
    if k_vz is not None and k_az is not None and o_vz is not None and o_az is not None:
        if same(o_vz, k_vz) and same(o_az, k_az):
            add("z", "ok", "Z speed and acceleration", "%s mm/s, %s mm/s²" % (fmt(o_vz), fmt(o_az)),
                "%s mm/s, %s mm/s²" % (fmt(k_vz), fmt(k_az)))
        else:
            add("z", "info", "Z speed and acceleration",
                "%s mm/s, %s mm/s²" % (fmt(o_vz), fmt(o_az)),
                "%s mm/s, %s mm/s²" % (fmt(k_vz), fmt(k_az)),
                "Only the time estimate uses these (layer changes and Z hops).",
                [fix("printer", "machine_max_speed_z", "even", k_vz),
                 fix("printer", "machine_max_acceleration_z", "even", k_az)])

    # --- Bed area and height --------------------------------------------------------------------
    box = area_bbox(v.get("printable_area"))
    amin, amax = toolhead.get("axis_minimum"), toolhead.get("axis_maximum")
    if box and amin and amax and len(amin) >= 3 and len(amax) >= 3:
        kx0, ky0, kz0 = (num(x) for x in amin[:3])
        kx1, ky1, kz1 = (num(x) for x in amax[:3])
        ox0, oy0, ox1, oy1 = box
        orca_txt = "X %s…%s, Y %s…%s" % (fmt(ox0), fmt(ox1), fmt(oy0), fmt(oy1))
        kl_txt = "X %s…%s, Y %s…%s" % (fmt(kx0), fmt(kx1), fmt(ky0), fmt(ky1))
        tol = 0.05
        outside = []
        if ox0 < kx0 - tol:
            outside.append("X %s (Klipper's minimum is %s)" % (fmt(ox0), fmt(kx0)))
        if ox1 > kx1 + tol:
            outside.append("X %s (Klipper's maximum is %s)" % (fmt(ox1), fmt(kx1)))
        if oy0 < ky0 - tol:
            outside.append("Y %s (Klipper's minimum is %s)" % (fmt(oy0), fmt(ky0)))
        if oy1 > ky1 + tol:
            outside.append("Y %s (Klipper's maximum is %s)" % (fmt(oy1), fmt(ky1)))
        if outside:
            add("bed", "error", "Bed reaches past Klipper's travel", orca_txt, kl_txt,
                "Orca's bed goes to %s. A part placed there stops the print with \"Move out "
                "of range\". Set the printable area in the printer settings to fit inside "
                "Klipper's position_min/position_max." % "; ".join(outside))
        else:
            add("bed", "ok", "Bed area inside Klipper's travel", orca_txt, kl_txt)
        o_h = num(v.get("printable_height"))
        if o_h is not None and kz1 is not None:
            if o_h > kz1 + tol:
                add("height", "error", "Build height above Klipper's Z travel",
                    fmt(o_h) + " mm", fmt(kz1) + " mm",
                    "A part taller than %s mm stops the print with \"Move out of range\". "
                    "Set the printable height in the printer settings to %s or less."
                    % (fmt(kz1), fmt(kz1)),
                    [fix("printer", "printable_height", "scalar", kz1)])
            else:
                add("height", "ok", "Build height", fmt(o_h) + " mm", fmt(kz1) + " mm")

    # --- Nozzle -----------------------------------------------------------------------------------
    o_noz = first(v, "nozzle_diameter")
    k_noz = num(extruder.get("nozzle_diameter"))
    if o_noz is not None and k_noz is not None:
        if same(o_noz, k_noz, 0.001):
            add("nozzle", "ok", "Nozzle diameter", fmt(o_noz) + " mm", fmt(k_noz) + " mm")
        else:
            add("nozzle", "warn", "Nozzle diameter differs", fmt(o_noz) + " mm",
                fmt(k_noz) + " mm",
                "Orca slices for a %s mm nozzle; printer.cfg says %s mm. If you changed the "
                "nozzle, update the one that is wrong: Klipper uses nozzle_diameter for its "
                "extrusion limit." % (fmt(o_noz), fmt(k_noz)))

    # --- Extrusion cross-section -------------------------------------------------------------------
    k_cross = num(extruder.get("max_extrude_cross_section"))
    if k_cross is None and k_noz:
        k_cross = 4.0 * k_noz * k_noz
    layer_h = num(v.get("layer_height"))
    first_h = num(v.get("initial_layer_print_height")) or num(v.get("initial_layer_height"))
    noz = o_noz or k_noz
    if k_cross and noz and layer_h:
        base_w = resolve_percent(v.get("line_width"), noz) or noz * 1.125
        worst = None
        candidates = [(label, resolve_percent(v.get(key), noz) or base_w, layer_h)
                      for key, label in LINE_WIDTHS]
        if first_h:
            candidates.append(("First layer",
                               resolve_percent(v.get("initial_layer_line_width"), noz) or base_w,
                               first_h))
        for label, width, height in candidates:
            area = width * height
            if worst is None or area > worst[3]:
                worst = (label, width, height, area)
        if worst:
            label, width, height, area = worst
            txt = "%s × %s mm = %s mm²" % (fmt(width), fmt(height), fmt(round(area, 3)))
            if area > k_cross:
                add("cross", "error", "Lines too thick for Klipper's extrusion limit",
                    "%s: %s" % (label, txt), fmt(k_cross) + " mm²",
                    "Klipper stops the print with \"Move exceeds maximum extrusion\" when a "
                    "line's cross-section is above max_extrude_cross_section. Lower the line "
                    "width or layer height, or raise max_extrude_cross_section in [extruder].")
            else:
                add("cross", "ok", "Extrusion cross-section", "%s: %s" % (label, txt),
                    "max " + fmt(k_cross) + " mm²")

    # --- Temperatures ------------------------------------------------------------------------------
    k_tmax, k_tmin = num(extruder.get("max_temp")), num(extruder.get("min_extrude_temp"))
    temps = [num(t) for t in as_list(v.get("nozzle_temperature"))
             + as_list(v.get("nozzle_temperature_initial_layer"))]
    temps = [t for t in temps if t]
    if temps and (k_tmax or k_tmin is not None):
        hi, lo = max(temps), min(temps)
        orca_txt = fmt(lo) if lo == hi else "%s…%s" % (fmt(lo), fmt(hi))
        kl_txt = "min_extrude_temp %s, max_temp %s" % (fmt(k_tmin), fmt(k_tmax))
        if k_tmax and hi > k_tmax:
            add("hotend", "error", "Nozzle temperature above Klipper's max_temp",
                orca_txt + " °C", kl_txt,
                "Klipper refuses targets above max_temp, so the print stops at the first "
                "heating command.")
        elif k_tmin is not None and lo < k_tmin:
            add("hotend", "error", "Nozzle temperature below min_extrude_temp",
                orca_txt + " °C", kl_txt,
                "Klipper won't extrude below %s °C, so the print stops with \"Extrude below "
                "minimum temp\"." % fmt(k_tmin))
        elif k_tmax and hi > k_tmax - 5:
            add("hotend", "warn", "Nozzle temperature close to max_temp", orca_txt + " °C", kl_txt,
                "A few degrees of overshoot past max_temp shuts Klipper down mid-print. Keep at "
                "least 5 °C between the filament temperature and max_temp.")
        elif k_tmin is not None and lo - k_tmin < 10:
            add("hotend", "warn", "Small margin above min_extrude_temp", orca_txt + " °C", kl_txt,
                "A short dip or a noisy thermistor reading below %s °C stops the print with "
                "\"Extrude below minimum temp\"." % fmt(k_tmin))
        else:
            add("hotend", "ok", "Nozzle temperature", orca_txt + " °C", kl_txt)

    bed_type = str(v.get("curr_bed_type") or "").strip()
    if not bed_type or bed_type.lower() == "default plate":
        bed_type = str(v.get("default_bed_type") or "").strip()
    plate_key = PLATE_TEMP_KEYS.get(bed_type.lower())
    k_bmax = num(bed.get("max_temp"))
    if plate_key and k_bmax:
        btemps = [num(t) for t in as_list(v.get(plate_key))
                  + as_list(v.get(plate_key + "_initial_layer"))]
        btemps = [t for t in btemps if t]
        if btemps:
            hi = max(btemps)
            if hi > k_bmax:
                add("bedtemp", "error", "Bed temperature above Klipper's max_temp",
                    "%s °C (%s)" % (fmt(hi), bed_type), "max_temp %s" % fmt(k_bmax),
                    "Klipper refuses the bed target, so the print stops at the start.")
            elif hi > k_bmax - 5:
                add("bedtemp", "warn", "Bed temperature close to max_temp",
                    "%s °C (%s)" % (fmt(hi), bed_type), "max_temp %s" % fmt(k_bmax),
                    "Overshoot past max_temp shuts Klipper down.")
            else:
                add("bedtemp", "ok", "Bed temperature", "%s °C (%s)" % (fmt(hi), bed_type),
                    "max_temp %s" % fmt(k_bmax))

    # --- Firmware retraction -------------------------------------------------------------------------
    if truthy(v.get("use_firmware_retraction")):
        if "firmware_retraction" not in objects:
            add("fwretract", "error", "G10/G11 retraction without [firmware_retraction]",
                "firmware retraction on", "no [firmware_retraction]",
                "Orca retracts with G10/G11, but Klipper has no [firmware_retraction] section "
                "and ignores them, so nothing retracts (stringing, blobs). Add "
                "[firmware_retraction] to printer.cfg or turn off \"Use firmware retraction\".")
        else:
            length = num(settings.get("firmware_retraction", {}).get("retract_length"))
            orca_gcode = "\n".join(gcode_text(v.get(key)) for key, _ in GCODE_KEYS)
            set_later = "set_retraction" in orca_gcode.lower() or macro_uses(settings,
                                                                              "set_retraction")
            if length is not None and length <= 0 and set_later:
                add("fwretract", "ok", "Firmware retraction", "on",
                    "set by SET_RETRACTION in a macro")
            elif length is not None and length <= 0:
                add("fwretract", "warn", "Firmware retraction length is 0",
                    "firmware retraction on", "retract_length 0",
                    "Klipper's [firmware_retraction] has retract_length 0, so G10 does "
                    "nothing until a macro sets it with SET_RETRACTION.")
            else:
                add("fwretract", "ok", "Firmware retraction", "on",
                    "retract_length %s mm" % fmt(length))

    # --- Exclude objects --------------------------------------------------------------------------------
    if v.get("exclude_object") is not None:
        o_ex = truthy(v.get("exclude_object"))
        k_ex = "exclude_object" in objects
        if o_ex and not k_ex:
            add("exclude", "warn", "Object labels without [exclude_object]",
                "Exclude objects on", "no [exclude_object]",
                "Orca writes EXCLUDE_OBJECT commands, but Klipper has no [exclude_object] "
                "section, so each one prints \"Unknown command\", you can't cancel a single "
                "object, and adaptive bed meshing can't see the parts. Add [exclude_object] "
                "to printer.cfg.")
        elif k_ex and not o_ex:
            add("exclude", "info", "Exclude objects is off in Orca", "off",
                "[exclude_object] present",
                "Turn it on to cancel single objects from Mainsail/Fluidd and to let adaptive "
                "bed meshing probe only where the parts are. Note: this changes the process "
                "preset, which other printers may share.",
                [fix("process", "exclude_object", "scalar", "1")])
        elif o_ex and k_ex:
            add("exclude", "ok", "Exclude objects", "on", "[exclude_object] present")

    # --- Custom G-code commands ----------------------------------------------------------------------------
    known = known_commands(kl)
    if known:
        missing = {}
        for key, label in GCODE_KEYS:
            for cmd in extended_commands(gcode_text(v.get(key))):
                if cmd not in known:
                    missing.setdefault(cmd, [])
                    if label not in missing[cmd]:
                        missing[cmd].append(label)
        if missing:
            in_start = any("Machine start G-code" in where for where in missing.values())
            add("gcode", "error" if in_start else "warn",
                "Klipper doesn't know %d command%s in your G-code"
                % (len(missing), "" if len(missing) == 1 else "s"),
                ", ".join(sorted(missing)), "not in Klipper's command list",
                "Klipper answers \"Unknown command\" and skips them: %s.%s" % (
                    "; ".join("%s (%s)" % (cmd, ", ".join(where))
                              for cmd, where in sorted(missing.items())),
                    " With the Klipper flavor Orca doesn't add its own heating commands, so a "
                    "missing start macro can leave the printer cold." if in_start else ""))
        else:
            add("gcode", "ok", "Custom G-code commands", "all found", "%d commands" % len(known))

    # --- Thumbnails ------------------------------------------------------------------------------------------
    if "thumbnails" in v:
        sizes = [s for s in as_list(v.get("thumbnails")) if re.search(r"\d+\s*x\s*\d+", s)]
        if not sizes:
            add("thumbs", "info", "No G-code thumbnails", "none", "Mainsail/Fluidd show a preview",
                "Without thumbnails Mainsail, Fluidd and KlipperScreen show no picture of the "
                "part. Orca's default is 48x48/PNG,300x300/PNG.",
                [fix("printer", "thumbnails", "scalar", "48x48/PNG,300x300/PNG")])
        elif all("qoi" in s.lower() for s in sizes):
            add("thumbs", "info", "Thumbnails are QOI only", ", ".join(sizes),
                "Moonraker reads PNG and JPG",
                "Moonraker extracts PNG and JPG thumbnails; with QOI only, the web interface "
                "shows no picture. Add a PNG size, for example 300x300/PNG.",
                [fix("printer", "thumbnails", "scalar", ",".join(sizes + ["300x300/PNG"]))])
        else:
            add("thumbs", "ok", "G-code thumbnails", ", ".join(sizes), "Moonraker reads them")

    # --- Speeds Klipper will cap ------------------------------------------------------------------------------
    if k_vel:
        capped = []
        for key, label in PROCESS_SPEEDS:
            speed = first(v, key)
            if speed and speed > k_vel + 0.5:
                capped.append("%s %s" % (label, fmt(speed)))
        if capped:
            add("procspeed", "info", "Process speeds above Klipper's max_velocity",
                "; ".join(capped) + " mm/s", fmt(k_vel) + " mm/s",
                "Klipper caps these at %s mm/s, so the print takes longer than Orca's "
                "estimate." % fmt(k_vel))

    items.sort(key=lambda it: LEVEL_ORDER.get(it["level"], 9))
    return items


def fix(category, key, kind, value):
    return {"category": category, "key": key, "kind": kind, "value": value}


def serialize_fix(f, values):
    """Serialized value for one fix, built on the preset's current value."""
    value = f["value"]
    text = fmt(value) if isinstance(value, float) else str(value)
    if f["kind"] == "scalar":
        return text
    current = as_list(values.get(f["key"]))
    if not current:
        return text
    # Machine limit arrays hold [normal, silent] pairs (one pair per extruder variant):
    # only the normal-mode entries are Klipper's.
    return ",".join(text if i % 2 == 0 else current[i] for i in range(len(current)))


# Other addresses for a printer (a Tailscale or VPN address, say), by printer preset name. They
# live next to the plugin, inside OrcaSlicer's data folder, and never change the preset.
def address_file():
    """Where the "other address" of each printer is kept: the plugin's storage folder in
    OrcaSlicer, which survives plugin updates, or next to this file when that isn't available."""
    folder = None
    storage = getattr(getattr(getattr(orca, "host", None), "plugin", None), "storage", None)
    if callable(storage):
        try:
            folder = storage()
        except Exception:
            folder = None
    if not isinstance(folder, (str, os.PathLike)) or not os.path.isdir(folder):
        folder = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(folder, "addresses.json")


def load_addresses(path=None):
    try:
        with open(path or address_file(), encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_addresses(addresses, path=None):
    path = path or address_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(addresses, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def summarize(kl, moon):
    info, server = kl.get("info", {}), kl.get("server", {})
    stats, sd = kl.get("print_stats", {}), kl.get("virtual_sdcard", {})
    state = stats.get("state") or ""
    progress = sd.get("progress")
    return {
        "host": moon.label,
        "klipper_version": info.get("software_version", ""),
        "moonraker_version": server.get("moonraker_version", ""),
        "hostname": info.get("hostname", ""),
        "klippy_state": info.get("state", ""),
        "state_message": (info.get("state_message") or "").strip(),
        "print_state": state,
        "progress": round(progress * 100) if isinstance(progress, (int, float)) else None,
    }


# --------------------------------------------------------------------------------------------
# The panel
# --------------------------------------------------------------------------------------------

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:var(--orca-bg,#fff);--fg:var(--orca-fg,#1d2227);--muted:var(--orca-muted,#6b737b);
--line:var(--orca-border,rgba(127,127,127,.28));--accent:var(--orca-accent,#009688);
--soft:rgba(127,127,127,.09);--err:#d64545;--warn:#d98a00;--info:#3a7bd5;--ok:#2e9e5b}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;padding:12px;font:13px/1.45 var(--orca-font,system-ui,-apple-system,"Segoe UI",sans-serif);
color:var(--fg);background:var(--bg)}
h1{font-size:15px;margin:0}
.sub{color:var(--muted);font-size:12px;margin:2px 0 10px;overflow-wrap:anywhere}
.conn{display:flex;gap:8px;align-items:flex-start;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;margin-bottom:10px;font-size:12px}
.conn .dot{width:9px;height:9px;border-radius:50%;margin-top:4px;flex:none;background:var(--muted)}
.conn.ok .dot{background:var(--ok)}.conn.bad .dot{background:var(--err)}.conn.wait .dot{background:var(--warn)}
.actions{display:flex;gap:6px;margin-bottom:10px}
button{font:inherit;color:inherit;background:transparent;border:1px solid var(--line);border-radius:6px;
padding:5px 12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:disabled{opacity:.45;cursor:default}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;font-size:12px}
.chip{padding:2px 9px;border-radius:20px;border:1px solid var(--line)}
.chip b{font-weight:600}
.item{border:1px solid var(--line);border-left:4px solid var(--muted);border-radius:7px;padding:8px 10px;
margin-bottom:8px}
.item.error{border-left-color:var(--err)}.item.warn{border-left-color:var(--warn)}
.item.info{border-left-color:var(--info)}.item.ok{border-left-color:var(--ok)}
.item .t{font-weight:600}
.vals{display:grid;grid-template-columns:auto 1fr;gap:1px 8px;margin:5px 0;font-size:12px}
.vals span:nth-child(odd){color:var(--muted)}
.vals span:nth-child(even){overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.detail{font-size:12px;color:var(--muted);margin:4px 0 0}
.item button{margin-top:6px;padding:3px 10px;font-size:12px}
details{margin-top:6px}summary{cursor:pointer;color:var(--muted);font-size:12px;margin-bottom:8px}
.okrow{display:flex;justify-content:space-between;gap:8px;padding:3px 2px;font-size:12px;
border-bottom:1px solid var(--soft)}
.okrow span:last-child{color:var(--muted);text-align:right;overflow-wrap:anywhere}
.note{font-size:12px;padding:8px 10px;border-radius:7px;background:var(--soft);margin-bottom:10px}
.note.err{color:var(--err)}
.foot{color:var(--muted);font-size:11px;margin-top:12px}
.addr{border:1px dashed var(--line);border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:12px}
.addr p{margin:0 0 6px;color:var(--muted)}
.addrrow{display:flex;gap:6px}
.addrrow input{flex:1;min-width:0;font:inherit;color:inherit;background:transparent;
border:1px solid var(--line);border-radius:6px;padding:4px 8px}
button.link{border:0;padding:0;margin-top:6px;color:var(--accent);text-decoration:underline;font-size:12px}
</style></head><body>
<h1>Klipper Sync</h1>
<div class="sub" id="sub">Reading the selected presets…</div>
<div class="conn wait" id="conn"><div class="dot"></div><div id="conntext">Not checked yet</div></div>
<div class="addr" id="addr" hidden>
  <p id="addrnote">Can't reach it from here? Enter another address for this printer, for example its
  Tailscale or VPN address. The printer preset stays as it is.</p>
  <div class="addrrow"><input id="addrin" placeholder="e.g. 100.64.0.12 or printer.local:7125">
  <button id="addruse">Use</button></div>
  <button class="link" id="addrreset" hidden>Use the preset's address again</button>
</div>
<div class="actions">
  <button class="primary" id="check">Check</button>
  <button id="fixall" disabled>Fix all</button>
</div>
<div id="note" class="note" hidden></div>
<div class="chips" id="chips"></div>
<div id="items"></div>
<details id="okbox" hidden><summary id="oksum"></summary><div id="okitems"></div></details>
<div class="foot">Klipper Sync only reads from the printer. Fixes change the preset in Orca; save the preset to keep them.</div>
<script>
(function(){
var $=function(id){return document.getElementById(id)};
var busy=false,report=null;
function esc(x){return String(x==null?"":x).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function setBusy(b){busy=b;$("check").disabled=b;
  $("fixall").disabled=b||!report||!report.items.some(function(i){return i.fixable&&i.level!=="ok"})}
function note(text,isErr){var n=$("note");if(!text){n.hidden=true;return}
  n.hidden=false;n.textContent=text;n.className="note"+(isErr?" err":"")}
function conn(cls,text){$("conn").className="conn "+cls;$("conntext").textContent=text}
function check(keepNote){if(keepNote!==true)note("");setBusy(true);conn("wait","Asking the printer…");
  orca.postMessage({command:"check",keep_note:keepNote===true})}
function render(r){
  report=r;
  $("sub").textContent="Printer preset: "+r.printer+(r.printer_dirty?" (unsaved changes)":"");
  var k=r.klipper,st=k.print_state||"";
  var txt=(k.hostname?k.hostname+" · ":"")+k.host+" · Klipper "+(k.klipper_version||"?")+
    " · "+(st==="printing"?"printing"+(k.progress!=null?" "+k.progress+"%":""):(st||k.klippy_state));
  conn("ok",txt+(k.other_address?" (other address)":""));
  showAddr(false,k.other_address);
  var counts={error:0,warn:0,info:0,ok:0};
  r.items.forEach(function(i){counts[i.level]=(counts[i.level]||0)+1});
  var names={error:["problem","problems"],warn:["warning","warnings"],info:["note","notes"],
    ok:["match","match"]};
  $("chips").innerHTML=["error","warn","info","ok"].filter(function(l){return counts[l]})
    .map(function(l){return '<span class="chip"><b>'+counts[l]+"</b> "+
      names[l][counts[l]===1?0:1]+"</span>"}).join("");
  var open=r.items.filter(function(i){return i.level!=="ok"});
  $("items").innerHTML=open.length?open.map(function(i){
    return '<div class="item '+i.level+'"><div class="t">'+esc(i.title)+'</div>'+
      '<div class="vals"><span>Orca</span><span>'+esc(i.orca)+'</span><span>Klipper</span><span>'+
      esc(i.klipper)+'</span></div>'+(i.detail?'<p class="detail">'+esc(i.detail)+'</p>':"")+
      (i.fixable?'<button data-fix="'+esc(i.id)+'">Use Klipper\'s value</button>':"")+"</div>"}).join("")
    :'<div class="note">Everything Klipper Sync checks matches this printer.</div>';
  var ok=r.items.filter(function(i){return i.level==="ok"});
  $("okbox").hidden=!ok.length;$("oksum").textContent=ok.length+" settings match";
  $("okitems").innerHTML=ok.map(function(i){return '<div class="okrow"><span>'+esc(i.title)+
    "</span><span>"+esc(i.orca)+"</span></div>"}).join("");
  setBusy(false);
}
orca.onMessage(function(m){
  if(!m)return;
  if(m.command==="report"){if(m.notice!=null)note(m.notice);render(m.report)}
  else if(m.command==="status"){conn(m.cls||"wait",m.text)}
  else if(m.command==="error"){report=null;$("items").innerHTML="";$("chips").innerHTML="";
    $("okbox").hidden=true;if(m.printer)$("sub").textContent="Printer preset: "+m.printer;
    conn("bad",m.text);if(m.printer!==undefined)showAddr(true,m.other_address||"");setBusy(false)}
  else if(m.command==="fixed"){note(m.text,!!m.failed);setBusy(true);
    conn("wait","Checking again…");orca.postMessage({command:"check",keep_note:true})}
  else if(m.command==="autocheck"){clearTimeout(autoTimer);autoTimer=setTimeout(auto,700)}
});
var autoTimer=null;
// The "other address" box: open after a failed connection, and a reset link while one is in use.
function showAddr(failed,other){
  $("addr").hidden=!failed&&!other;
  $("addrnote").hidden=!failed;$("addrin").parentNode.hidden=!failed;
  if(failed)$("addrin").value=other||"";
  $("addrreset").hidden=!other;
  $("addrreset").textContent=other?"Using "+other+". Use the preset's address again":"";
}
function setAddress(a){if(busy)return;note("");setBusy(true);conn("wait","Asking the printer…");
  orca.postMessage({command:"set_address",address:a})}
$("addruse").addEventListener("click",function(){var a=$("addrin").value.trim();if(a)setAddress(a)});
$("addrin").addEventListener("keydown",function(e){if(e.key==="Enter"){var a=this.value.trim();if(a)setAddress(a)}});
$("addrreset").addEventListener("click",function(){setAddress("")});
// Switching printers fires several preset events in a row; wait for them to settle, and wait
// for a running check or fix to finish instead of starting a second one.
function auto(){if(busy){autoTimer=setTimeout(auto,700);return}check(true)}
$("check").addEventListener("click",function(){check(false)});
$("fixall").addEventListener("click",function(){if(busy||!report)return;
  var ids=report.items.filter(function(i){return i.fixable&&i.level!=="ok"}).map(function(i){return i.id});
  if(ids.length){setBusy(true);orca.postMessage({command:"fix",ids:ids})}});
$("items").addEventListener("click",function(e){var b=e.target.closest("[data-fix]");
  if(!b||busy)return;setBusy(true);orca.postMessage({command:"fix",ids:[b.getAttribute("data-fix")]})});
check();
})();
</script></body></html>"""


def snapshot_orca():
    """Read the selected presets. Must run on the UI thread (see the Preset Update demo)."""
    bundle = orca.host.preset_bundle()
    printer = bundle.current_printer_preset()
    process = bundle.current_process_preset()
    values = {}
    for key in SNAPSHOT_KEYS:
        try:
            values[key] = plain(bundle.full_config_value(key))
        except Exception:
            values[key] = None
    return {
        "printer": printer.name if printer is not None else "",
        "printer_dirty": bool(printer.is_dirty) if printer is not None else False,
        "process": process.name if process is not None else "",
        "values": values,
    }


if orca is not None:

    class KlipperSync(orca.script.ScriptPluginCapabilityBase):
        panel = None
        _jobs = None
        _cancelled = False
        _last = None  # {"printer": name, "items": {id: item}}

        def get_name(self):
            return "Klipper Sync"

        # Runs on the Script worker. It owns the panel and does all network and preset work, so
        # the UI thread never waits on the printer.
        def execute(self):
            if self.panel is not None and self.panel.is_open():
                self.panel.show()
                return orca.ExecutionResult.success("Klipper Sync is already open.")
            jobs = queue.Queue()
            self._jobs = jobs
            self._cancelled = False
            try:
                self.panel = orca.host.ui.create_dock_panel(
                    html=PAGE, title="Klipper Sync", width=360, height=640,
                    on_message=self._on_message, on_close=self._on_close, dock="right")
                while True:
                    job = jobs.get()
                    if self._cancelled or job.get("command") == "close":
                        return orca.ExecutionResult.success("Klipper Sync closed.")
                    try:
                        if job["command"] == "check":
                            self._check(job)
                        elif job["command"] == "fix":
                            self._fix(job)
                        elif job["command"] == "set_address":
                            self._set_address(job)
                    except Exception as error:  # never let one job kill the panel
                        self._post({"command": "error", "text": "Klipper Sync hit an error: %s"
                                    % error})
            except Exception as error:
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError,
                                                    "Klipper Sync failed: %s" % error)
            finally:
                if self._jobs is jobs:
                    self._jobs = None
                self.panel = None

        # UI thread: take the preset snapshot here, then hand the job to the worker.
        def _on_message(self, message):
            message = dict(message) if isinstance(message, dict) else {}
            command = message.get("command")
            if command not in ("check", "fix", "set_address"):
                return
            try:
                snap = snapshot_orca()
            except Exception as error:
                self._post({"command": "error", "text": "Couldn't read the presets: %s" % error})
                return
            job = {"command": command, "snap": snap, "keep_note": bool(message.get("keep_note"))}
            if command == "fix":
                job["ids"] = [str(i) for i in (message.get("ids") or [])]
            elif command == "set_address":
                job["address"] = str(message.get("address") or "")[:200]
            self._enqueue(job)

        def _on_close(self):
            self._enqueue({"command": "close"})

        def on_cancelled(self):
            self._cancelled = True
            self._enqueue({"command": "close"})

        # Re-check when another printer, process or filament is picked, or a preset is saved.
        # The handler must return at once, so it only nudges the page; the page debounces and
        # sends a normal "check", whose snapshot is then taken on the UI thread.
        def on_lifecycle_event(self, event, ctx):
            events = getattr(orca, "LifecycleEvent", None)
            if events is None:
                return
            if event in (getattr(events, "PresetSelected", None),
                         getattr(events, "PresetSaved", None)):
                self._post({"command": "autocheck"})

        def _enqueue(self, job):
            jobs = self._jobs
            if jobs is not None:
                jobs.put(job)

        def _post(self, message):
            panel = self.panel
            if panel is not None and panel.is_open():
                panel.post(message)

        def _fetch(self, moon):
            """fetch_all, waiting while OrcaSlicer asks the user for network permission."""
            deadline = time.monotonic() + PERMISSION_WAIT
            asked = False
            while True:
                try:
                    return moon.fetch_all()
                except PermissionError:
                    if self._cancelled or time.monotonic() > deadline:
                        raise MoonrakerError(
                            "OrcaSlicer didn't allow Klipper Sync to connect to %s. Allow it "
                            "in the prompt (or in Plugins → Klipper Sync → Permissions) and "
                            "press Check." % moon.label)
                    if not asked:
                        asked = True
                        self._post({"command": "status", "cls": "wait",
                                    "text": "OrcaSlicer is asking whether Klipper Sync may "
                                            "connect to %s. Choose Yes and the check goes on by "
                                            "itself." % moon.label})
                    time.sleep(1.5)

        def _set_address(self, job):
            addresses = load_addresses()
            address = (job.get("address") or "").strip()
            if address:
                addresses[job["snap"]["printer"]] = address
            else:
                addresses.pop(job["snap"]["printer"], None)
            try:
                save_addresses(addresses)
            except OSError as error:
                self._post({"command": "error", "printer": job["snap"]["printer"],
                            "text": "Couldn't save the address: %s" % error})
                return
            self._check(job)

        def _check(self, job):
            snap = job["snap"]
            values = snap["values"]
            other = load_addresses().get(snap["printer"], "")
            preset_host = values.get("print_host") or ""
            where = {"printer": snap["printer"], "other_address": other,
                     "preset_address": preset_host}
            try:
                moon = Moonraker(other or preset_host, values.get("printhost_apikey") or "")
                kl = self._fetch(moon)
            except MoonrakerError as error:
                self._last = None
                self._post(dict(where, command="error", text=str(error)))
                return
            summary = summarize(kl, moon)
            if not kl.get("ready"):
                self._last = None
                self._post(dict(where, command="error",
                                text="Klipper on %s isn't ready (%s). %s" % (
                                    moon.label, summary["klippy_state"],
                                    summary["state_message"])))
                return
            summary["other_address"] = other
            items = build_report(snap, kl)
            self._last = {"printer": snap["printer"], "items": {i["id"]: i for i in items}}
            self._post({"command": "report", "notice": None if job.get("keep_note") else "",
                        "report": {
                            "printer": snap["printer"], "printer_dirty": snap["printer_dirty"],
                            "klipper": summary,
                            "items": [{"id": i["id"], "level": i["level"], "title": i["title"],
                                       "orca": i["orca"], "klipper": i["klipper"],
                                       "detail": i["detail"], "fixable": bool(i["fixes"])}
                                      for i in items]}})

        def _fix(self, job):
            snap, last = job["snap"], self._last
            if last is None or last["printer"] != snap["printer"]:
                self._post({"command": "fixed", "failed": True,
                            "text": "The selected printer changed since the last check. "
                                    "Check again before fixing."})
                return
            fixes = []
            for item_id in job.get("ids", []):
                item = last["items"].get(item_id)
                if item:
                    fixes.extend(item["fixes"])
            if not fixes:
                return
            done, failed = [], []
            for category in ("printer", "process"):
                group = [f for f in fixes if f["category"] == category]
                if not group:
                    continue
                try:
                    tx = getattr(orca.host.preset, category).begin_update("")
                    for f in group:
                        tx.set(f["key"], serialize_fix(f, snap["values"]))
                    result = tx.commit()
                    done.append('%d setting%s in the %s preset "%s"' % (
                        len(result.keys), "" if len(result.keys) == 1 else "s", category,
                        result.target_name))
                except Exception as error:
                    failed.append("%s preset: %s" % (category, error))
            text = ""
            if done:
                text = ("Changed " + " and ".join(done) + ". Save the preset in Orca to keep "
                        "the change.")
            if failed:
                text += (" " if text else "") + "Couldn't change the " + "; ".join(failed)
            self._post({"command": "fixed", "text": text, "failed": bool(failed)})

    @orca.plugin
    class KlipperSyncPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(KlipperSync)
