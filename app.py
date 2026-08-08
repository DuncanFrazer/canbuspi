from flask import Flask, render_template, jsonify, request
import time
import csv
import threading
import serial
import os
import json
import itertools
import traceback

app = Flask(__name__)
logging_active = False
log_file_can = None
log_file_esp = None
log_file_opt = None
serial_conn = None
log_thread = None
csv_writer_can = None
csv_writer_esp = None
csv_writer_opt = None
csv_file_can = None
csv_file_esp = None
csv_file_opt = None

# ============================================================
# CONFIGURATION
# ============================================================

ESP32_SERIAL_PORT = "/dev/ttyACM0"
ESP32_BAUD_RATE = 921600
LOG_DIR = "/home/duncan/canlogs"

# If no line at all is received for this long while logging is active,
# the serial connection is considered possibly stalled and is reopened.
# Mirrors the same reasoning as the old can0 stall watchdog, applied to
# a serial port instead of a CAN bus socket. Cooldown prevents a tight
# reconnect loop during genuine quiet periods (car asleep).
SERIAL_STALL_TIMEOUT_S = 10.0
STALL_RECONNECT_COOLDOWN_S = 300.0

# ============================================================
# OPTIMIZED LOG CONFIGURATION
# (unchanged from the MCP2515-based design - only the frame source
# changed, not the filtering logic itself)
# ============================================================
CONTINUOUS_WATCH_IDS = {0x3C0, 0x1B000010, 0x30B, 0x1B000069, 0x17F00069}
BURST_WATCH_IDS      = {0x6AF, 0x5BF, 0x17330B00}
OPTIMIZED_IDS = CONTINUOUS_WATCH_IDS | BURST_WATCH_IDS

BYTE_SLICE_DEDUP_IDS = {
    0x3C0: (4, 6),   # hex string slice for byte index 2
}
PRESENCE_ONLY_IDS = {0x30B}

ABSENCE_TIMEOUT_S    = 8.0
HEARTBEAT_INTERVAL_S = 30.0

opt_last_key       = {}
opt_last_seen       = {}
opt_present         = {}
opt_last_heartbeat  = {}
CONTINUOUS_WATCH_ID_STRS = {f"0x{i:X}" for i in CONTINUOUS_WATCH_IDS}

# ============================================================
# LIVE STREAM BUFFER
# ============================================================
seq_counter = itertools.count(1)
recent_messages = []
MAX_RECENT_MESSAGES = 200

def push_message(msg_type, payload):
    entry = {"seq": next(seq_counter), "type": msg_type}
    entry.update(payload)
    recent_messages.append(entry)
    if len(recent_messages) > MAX_RECENT_MESSAGES:
        recent_messages.pop(0)
    return entry

# ============================================================
# HELPERS
# ============================================================

def generate_log_filenames():
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    can_file = f"{LOG_DIR}/log_{timestamp}_can.csv"
    esp_file = f"{LOG_DIR}/log_{timestamp}_esp.csv"
    opt_file = f"{LOG_DIR}/log_{timestamp}_opt.csv"
    return can_file, esp_file, opt_file

def ensure_log_directory():
    os.makedirs(LOG_DIR, exist_ok=True)

def write_event(event):
    """Write a manual event tag to all three log files, and push it to
    the live stream so it appears immediately in the Event window."""
    ts = time.time()
    if csv_writer_can and logging_active:
        csv_writer_can.writerow([ts, "EVENT", event, "", "", ""])
        csv_file_can.flush()
    if csv_writer_esp and logging_active:
        csv_writer_esp.writerow([ts, "EVENT", event, "", "", ""])
        csv_file_esp.flush()
    if csv_writer_opt and logging_active:
        csv_writer_opt.writerow([ts, "EVENT", event, "", "", ""])
        csv_file_opt.flush()

    push_message("EVENT", {"timestamp": ts, "event": event})

def write_diagnostic_event(event):
    """Internal diagnostic event (stall detected, serial reconnected
    etc), written to the main CAN log and live stream regardless of
    the logging_active check since this can happen mid-recovery."""
    ts = time.time()
    print(f"[DIAG] {event}")
    if csv_writer_can:
        csv_writer_can.writerow([ts, "EVENT", event, "", "", ""])
        csv_file_can.flush()
    push_message("EVENT", {"timestamp": ts, "event": event})

def reconnect_serial():
    global serial_conn
    write_diagnostic_event("serial_reconnect_attempt")
    try:
        if serial_conn is not None:
            try:
                serial_conn.close()
            except Exception as e:
                print(f"[SERIAL] Error closing old connection: {e}")
        serial_conn = serial.Serial(ESP32_SERIAL_PORT, ESP32_BAUD_RATE, timeout=1.0)
        write_diagnostic_event("serial_reconnect_success")
        return True
    except Exception as e:
        print(f"[SERIAL] Failed to reconnect: {e}")
        traceback.print_exc()
        write_diagnostic_event(f"serial_reconnect_failed:{e}")
        time.sleep(1.0)
        return False

def reset_optimized_state():
    opt_last_key.clear()
    opt_last_seen.clear()
    opt_present.clear()
    opt_last_heartbeat.clear()

def dedup_key(msg_id_int, data_hex):
    """Return the string used to decide whether this frame represents
    a 'change' worth logging in the optimized file."""
    if msg_id_int in PRESENCE_ONLY_IDS:
        return "PRESENCE_ONLY"
    if msg_id_int in BYTE_SLICE_DEDUP_IDS:
        start, end = BYTE_SLICE_DEDUP_IDS[msg_id_int]
        return data_hex[start:end]
    return data_hex

def handle_optimized_frame(ts, msg_id_int, msg_id_str, dlc, data_hex, extended):
    """Write a row to the optimized log if this frame represents a
    change, a first appearance, or a periodic heartbeat."""
    is_continuous = msg_id_int in CONTINUOUS_WATCH_IDS
    key = dedup_key(msg_id_int, data_hex)

    changed = opt_last_key.get(msg_id_str) != key
    became_present = is_continuous and not opt_present.get(msg_id_str, False)

    should_log = changed or became_present

    if not should_log and is_continuous:
        last_hb = opt_last_heartbeat.get(msg_id_str, 0)
        if ts - last_hb >= HEARTBEAT_INTERVAL_S:
            should_log = True

    if should_log and csv_writer_opt:
        csv_writer_opt.writerow([ts, "CAN", msg_id_str, dlc, data_hex, extended])
        csv_file_opt.flush()
        opt_last_key[msg_id_str] = key
        opt_last_heartbeat[msg_id_str] = ts

    if is_continuous:
        opt_last_seen[msg_id_str] = ts
        opt_present[msg_id_str] = True

def check_optimized_absence(now):
    if not (csv_writer_opt and logging_active):
        return
    for id_str in CONTINUOUS_WATCH_ID_STRS:
        if opt_present.get(id_str, False):
            last_seen = opt_last_seen.get(id_str, now)
            if now - last_seen > ABSENCE_TIMEOUT_S:
                opt_present[id_str] = False
                csv_writer_opt.writerow([now, "EVENT", f"absent_{id_str}", "", "", ""])
                csv_file_opt.flush()

def parse_canrx_line(line):
    """Parse a 'CANRX,0xID,DLC,DATAHEX,EXT' line from the ESP32.
    Returns (id_int, id_str, dlc, data_hex, extended) or None if the
    line is malformed (e.g. a torn line from a mid-write reconnect)."""
    try:
        parts = line.split(",")
        if len(parts) != 5 or parts[0] != "CANRX":
            return None
        id_str = parts[1]
        id_int = int(id_str, 16)
        dlc = int(parts[2])
        data_hex = parts[3]
        extended = int(parts[4])
        return id_int, id_str, dlc, data_hex, extended
    except (ValueError, IndexError):
        return None

# ============================================================
# CAPTURE THREAD
# ============================================================

def log_reader_thread():
    """Single thread reading the ESP32's serial connection, which now
    carries both raw CAN frames (CANRX,... lines) and the ESP32's own
    diagnostic log lines interleaved. Dispatches each line to the
    appropriate log file(s). Replaces the previous two-thread design
    (can0 SocketCAN reader + ESP32-diagnostics-only reader) now that
    both come from the same serial connection.

    Includes the same stall/reconnect watchdog pattern used previously
    for the can0 socket, applied here to the serial port instead.
    """
    global serial_conn

    last_line_time = time.time()
    last_absence_check = time.time()
    last_reconnect_attempt = 0.0

    while logging_active:
        try:
            raw = serial_conn.readline()
            now = time.time()

            if raw:
                last_line_time = now
                line = raw.decode("utf-8", errors="replace").rstrip()

                if line.startswith("CANRX,"):
                    parsed = parse_canrx_line(line)
                    if parsed is None:
                        continue
                    id_int, id_str, dlc, data_hex, extended = parsed

                    if csv_writer_can:
                        csv_writer_can.writerow([now, "CAN", id_str, dlc, data_hex, extended])
                        csv_file_can.flush()

                    decoded = decode_mqb_message(id_str, data_hex)
                    push_message("CAN", {
                        "timestamp": now,
                        "id": id_str,
                        "dlc": dlc,
                        "data": data_hex,
                        "decoded": decoded
                    })

                    if id_int in OPTIMIZED_IDS:
                        handle_optimized_frame(now, id_int, id_str, dlc, data_hex, extended)

                elif line:
                    # ESP32 diagnostic/log line (unchanged handling -
                    # same as the old esp32_logger_thread)
                    if csv_writer_esp:
                        csv_writer_esp.writerow([now, "SERIAL", line, "", "", ""])
                        csv_file_esp.flush()

            else:
                # readline() timed out with nothing received
                if logging_active and (now - last_line_time) > SERIAL_STALL_TIMEOUT_S:
                    cooldown_elapsed = (now - last_reconnect_attempt) > STALL_RECONNECT_COOLDOWN_S
                    if cooldown_elapsed:
                        write_diagnostic_event(
                            f"serial_stall_detected_{now - last_line_time:.1f}s")
                        last_reconnect_attempt = now
                        reconnect_serial()
                        # last_line_time deliberately NOT reset - if the
                        # bus is just asleep, next check waits for the
                        # cooldown, not another short timer

            if now - last_absence_check >= 1.0:
                check_optimized_absence(now)
                last_absence_check = now

        except Exception as e:
            if logging_active:
                print(f"Serial read error: {e}")
                traceback.print_exc()
                write_diagnostic_event(f"serial_read_exception:{e}")
                now = time.time()
                if (now - last_reconnect_attempt) > STALL_RECONNECT_COOLDOWN_S:
                    last_reconnect_attempt = now
                    reconnect_serial()
                last_line_time = time.time()
            time.sleep(0.1)

# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start_log", methods=["POST"])
def start_log():
    global logging_active, serial_conn, log_thread
    global csv_writer_can, csv_writer_esp, csv_writer_opt
    global csv_file_can, csv_file_esp, csv_file_opt
    global log_file_can, log_file_esp, log_file_opt

    if logging_active:
        return jsonify({"status": "already_running"})

    try:
        ensure_log_directory()

        log_file_can, log_file_esp, log_file_opt = generate_log_filenames()
        print(f"CAN log: {log_file_can}")
        print(f"ESP32 log: {log_file_esp}")
        print(f"Optimized log: {log_file_opt}")

        if serial_conn is None:
            print(f"Opening serial connection on {ESP32_SERIAL_PORT} at {ESP32_BAUD_RATE}...")
            serial_conn = serial.Serial(ESP32_SERIAL_PORT, ESP32_BAUD_RATE, timeout=1.0)
            print("Serial connection opened")

        csv_file_can = open(log_file_can, "w", newline='')
        csv_writer_can = csv.writer(csv_file_can)
        csv_writer_can.writerow(["timestamp", "type", "id_or_event", "dlc", "data", "extended"])

        csv_file_esp = open(log_file_esp, "w", newline='')
        csv_writer_esp = csv.writer(csv_file_esp)
        csv_writer_esp.writerow(["timestamp", "type", "message", "", "", ""])

        csv_file_opt = open(log_file_opt, "w", newline='')
        csv_writer_opt = csv.writer(csv_file_opt)
        csv_writer_opt.writerow(["timestamp", "type", "id_or_event", "dlc", "data", "extended"])

        reset_optimized_state()

        logging_active = True

        ts = time.time()
        for w, f in [(csv_writer_can, csv_file_can), (csv_writer_esp, csv_file_esp), (csv_writer_opt, csv_file_opt)]:
            w.writerow([ts, "EVENT", "start_log", "", "", ""])
            f.flush()
        push_message("EVENT", {"timestamp": ts, "event": "start_log"})

        log_thread = threading.Thread(target=log_reader_thread, daemon=True)
        log_thread.start()

        print("Capture thread started")

        return jsonify({
            "status": "started",
            "can_log": log_file_can,
            "esp_log": log_file_esp,
            "opt_log": log_file_opt
        })

    except Exception as e:
        logging_active = False
        print(f"ERROR in start_log: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stop_log", methods=["POST"])
def stop_log():
    global logging_active, log_thread
    global csv_writer_can, csv_writer_esp, csv_writer_opt
    global csv_file_can, csv_file_esp, csv_file_opt

    if not logging_active:
        return jsonify({"status": "not_running"})

    ts = time.time()
    for w, f in [(csv_writer_can, csv_file_can), (csv_writer_esp, csv_file_esp), (csv_writer_opt, csv_file_opt)]:
        if w:
            w.writerow([ts, "EVENT", "stop_log", "", "", ""])
            f.flush()
    push_message("EVENT", {"timestamp": ts, "event": "stop_log"})

    logging_active = False

    if log_thread:
        log_thread.join(timeout=2.0)
        log_thread = None

    if csv_file_can:
        csv_file_can.close(); csv_file_can = None; csv_writer_can = None
    if csv_file_esp:
        csv_file_esp.close(); csv_file_esp = None; csv_writer_esp = None
    if csv_file_opt:
        csv_file_opt.close(); csv_file_opt = None; csv_writer_opt = None

    return jsonify({
        "status": "stopped",
        "can_log": log_file_can,
        "esp_log": log_file_esp,
        "opt_log": log_file_opt
    })

@app.route("/action", methods=["POST"])
def action():
    data = request.get_json()
    ev = data.get("event")
    write_event(ev)
    return jsonify({"ok": True})

@app.route("/live")
def live():
    """Return last 100 lines from the CAN log file"""
    try:
        if log_file_can and os.path.exists(log_file_can):
            with open(log_file_can) as f:
                lines = f.readlines()[-100:]
            return jsonify(lines)
        return jsonify([])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/status")
def status():
    """Return current logging status"""
    return jsonify({
        "logging_active": logging_active,
        "can_source": "esp32_serial_passthrough",
        "can_log": log_file_can,
        "esp_log": log_file_esp,
        "opt_log": log_file_opt,
        "esp32_serial_port": ESP32_SERIAL_PORT,
        "esp32_baud_rate": ESP32_BAUD_RATE

    })

@app.route("/stream")
def stream():
    """Server-sent events stream for real-time CAN messages + event tags."""
    def generate():
        last_seq = 0
        while True:
            new_items = [m for m in recent_messages if m["seq"] > last_seq]
            if new_items:
                for m in new_items:
                    yield f"data: {json.dumps(m)}\n\n"
                last_seq = new_items[-1]["seq"]
            time.sleep(0.05)

    return app.response_class(generate(), mimetype='text/event-stream')

# ============================================================
# MQB MESSAGE DECODE
# ============================================================

def decode_mqb_message(msg_id, data):
    """Decode known MQB platform messages"""
    msg_id_int = int(msg_id, 16) if isinstance(msg_id, str) else msg_id
    data_bytes = bytes.fromhex(data) if isinstance(data, str) else data

    decoded = None

    if msg_id_int == 0x77E and len(data_bytes) >= 5:
        if data_bytes[0] == 0x05 and data_bytes[1] == 0x62 and data_bytes[2] == 0x22 and data_bytes[3] == 0xD1:
            rpm = ((data_bytes[4] << 8) | data_bytes[5]) / 4
            decoded = f"RPM: {rpm:.0f}"
        elif data_bytes[0] == 0x04 and data_bytes[1] == 0x62 and data_bytes[2] == 0x22 and data_bytes[3] == 0x4D:
            brightness = data_bytes[4]
            decoded = f"Ambient Light: {brightness}/255"

    elif msg_id_int == 0x7E9 and len(data_bytes) >= 4:
        if data_bytes[0] == 0x04 and data_bytes[1] == 0x62 and data_bytes[2] == 0x38:
            if data_bytes[3] == 0x16:
                gear_map = {0x00: "None", 0x02: "1st", 0x0C: "Reverse"}
                gear = gear_map.get(data_bytes[4], f"Unknown ({data_bytes[4]:02X})")
                decoded = f"Gear: {gear}"
            elif data_bytes[3] == 0x15:
                mode_map = {0x00: "P", 0x01: "R", 0x02: "N", 0x03: "D", 0x04: "S", 0x05: "M"}
                mode = mode_map.get(data_bytes[4], f"Unknown ({data_bytes[4]:02X})")
                decoded = f"Gearbox Mode: {mode}"

    return decoded

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
