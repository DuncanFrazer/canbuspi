from flask import Flask, render_template, jsonify, request
import time
import csv
import threading
import can
import os
import serial
import json
import itertools
import traceback

app = Flask(__name__)
logging_active = False
log_file_can = None
log_file_esp = None
log_file_opt = None
can_bus = None
log_thread_can = None
log_thread_esp = None
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
ESP32_BAUD_RATE = 115200
LOG_DIR = "/home/duncan/canlogs"

# If no CAN frame is received for this long while logging is active,
# the capture is considered *possibly* stalled and a reconnect is
# attempted. This alone can't distinguish a broken capture pipeline
# from a genuinely sleeping car (which is legitimately silent for
# many minutes) - so after a stall-triggered reconnect, further
# reconnect attempts are suppressed until STALL_RECONNECT_COOLDOWN_S
# has passed, unless a real frame arrives in the meantime (which
# resets everything back to normal automatically).
CAN_STALL_TIMEOUT_S = 10.0
STALL_RECONNECT_COOLDOWN_S = 300.0  # 5 min between stall-triggered reconnects

# ============================================================
# OPTIMIZED LOG CONFIGURATION
# ============================================================
# Only these IDs are relevant to car-state detection work (established
# across many analysis sessions). Everything else is background bus
# chatter not currently needed for that analysis.
#
# CONTINUOUS_WATCH_IDS: normally present whenever the bus is awake.
# Presence/absence is tracked so silence can be detected per-ID, not
# just inferred from row gaps.
#   0x3C0       - byte 2 is the ignition state: 00=off 01=transition 03=running
#   0x1B000010  - low-rate keeper, present even in light sleep
#   0x30B       - gateway keeper, also gives an early pre-wake signal
#   0x1B000069  - camera heartbeat
#   0x17F00069  - camera heartbeat
#
# BURST_WATCH_IDS: only appear around specific transition events, not
# continuously. Logged on change, no absence tracking (their absence
# is just normal quiet, not a meaningful state).
#   0x6AF       - camera deploy/stow animation state machine
#   0x5BF       - generic bus wake/sleep transition burst
#   0x17330B00  - MMI diagnostic self-test / fault pair (12C2/12C1)

CONTINUOUS_WATCH_IDS = {0x3C0, 0x1B000010, 0x30B, 0x1B000069, 0x17F00069}
BURST_WATCH_IDS      = {0x6AF, 0x5BF, 0x17330B00}
OPTIMIZED_IDS = CONTINUOUS_WATCH_IDS | BURST_WATCH_IDS

# 0x3C0's full payload changes almost every frame while driving (byte 0
# is an unrelated rolling counter). Only byte 2 is meaningful, so change
# detection for this ID uses byte 2 alone rather than the full payload.
BYTE_SLICE_DEDUP_IDS = {
    0x3C0: (4, 6),   # hex string slice for byte index 2
}

# 0x30B's payload is almost entirely a continuously drifting/rolling
# value with no validated meaningful field (unlike 0x3C0's byte 2).
# Logging it on "payload changed" defeats the point of this file - it
# was found to log at near full bus rate. Until a meaningful field is
# identified, these IDs are tracked for presence/absence only, using a
# constant dedup key so they never trigger on content change - only on
# first appearance and the periodic heartbeat.
PRESENCE_ONLY_IDS = {0x30B}

ABSENCE_TIMEOUT_S    = 8.0    # continuous ID considered "gone quiet" after this long
HEARTBEAT_INTERVAL_S = 30.0   # re-log an unchanged continuous ID at least this often

# Per-session optimized-log state (reset in start_log)
opt_last_key      = {}   # id_str -> last logged dedup key (payload or byte-slice)
opt_last_seen     = {}   # id_str -> timestamp last frame seen (continuous IDs only)
opt_present       = {}   # id_str -> bool, currently considered present
opt_last_heartbeat = {}  # id_str -> timestamp of last periodic re-log
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
    """Internal diagnostic event (stall detected, bus reconnected etc),
    written to the main CAN log and live stream regardless of the
    logging_active check since this can happen mid-recovery."""
    ts = time.time()
    print(f"[DIAG] {event}")
    if csv_writer_can:
        csv_writer_can.writerow([ts, "EVENT", event, "", "", ""])
        csv_file_can.flush()
    push_message("EVENT", {"timestamp": ts, "event": event})

def reconnect_can_bus():
    global can_bus
    write_diagnostic_event("can_bus_reconnect_attempt")
    try:
        if can_bus is not None:
            try:
                can_bus.shutdown()
            except Exception as e:
                print(f"[CAN] Error shutting down old bus object: {e}")
        can_bus = can.interface.Bus(channel='can0', interface='socketcan')
        write_diagnostic_event("can_bus_reconnect_success")
        return True
    except Exception as e:
        print(f"[CAN] Failed to recreate bus: {e}")
        traceback.print_exc()
        write_diagnostic_event(f"can_bus_reconnect_failed:{e}")
        time.sleep(1.0)
        return False

def reset_optimized_state():
    opt_last_key.clear()
    opt_last_seen.clear()
    opt_present.clear()
    opt_last_heartbeat.clear()

def dedup_key(msg_id_int, msg_id_str, data_hex):
    """Return the string used to decide whether this frame represents
    a 'change' worth logging in the optimized file."""
    if msg_id_int in PRESENCE_ONLY_IDS:
        return "PRESENCE_ONLY"  # constant - never triggers on content change
    if msg_id_int in BYTE_SLICE_DEDUP_IDS:
        start, end = BYTE_SLICE_DEDUP_IDS[msg_id_int]
        return data_hex[start:end]
    return data_hex

def handle_optimized_frame(msg, ts, msg_id_str, data_hex):
    """Write a row to the optimized log if this frame represents a
    change, a first appearance, or a periodic heartbeat. Also tracks
    presence for absence detection on continuous IDs."""
    is_continuous = msg.arbitration_id in CONTINUOUS_WATCH_IDS
    key = dedup_key(msg.arbitration_id, msg_id_str, data_hex)

    changed = opt_last_key.get(msg_id_str) != key
    became_present = is_continuous and not opt_present.get(msg_id_str, False)

    should_log = changed or became_present

    if not should_log and is_continuous:
        last_hb = opt_last_heartbeat.get(msg_id_str, 0)
        if ts - last_hb >= HEARTBEAT_INTERVAL_S:
            should_log = True

    if should_log and csv_writer_opt:
        csv_writer_opt.writerow([
            ts, "CAN", msg_id_str, msg.dlc, data_hex, msg.is_extended_id
        ])
        csv_file_opt.flush()
        opt_last_key[msg_id_str] = key
        opt_last_heartbeat[msg_id_str] = ts

    if is_continuous:
        opt_last_seen[msg_id_str] = ts
        opt_present[msg_id_str] = True

def check_optimized_absence(now):
    """Periodic check (called ~1x/sec) - log when a continuous ID has
    gone quiet for longer than ABSENCE_TIMEOUT_S. This is the signal
    that lets the optimized log show per-ID silence, not just the
    presence of rows."""
    if not (csv_writer_opt and logging_active):
        return
    for id_str in CONTINUOUS_WATCH_ID_STRS:
        if opt_present.get(id_str, False):
            last_seen = opt_last_seen.get(id_str, now)
            if now - last_seen > ABSENCE_TIMEOUT_S:
                opt_present[id_str] = False
                csv_writer_opt.writerow([now, "EVENT", f"absent_{id_str}", "", "", ""])
                csv_file_opt.flush()

# ============================================================
# CAPTURE THREADS
# ============================================================

def can_logger_thread():
    """Background thread that captures CAN messages to the full log,
    the optimized log, and the live stream. Includes a stall watchdog
    that recreates the bus object if no frame arrives for
    CAN_STALL_TIMEOUT_S while logging is active."""
    global can_bus

    while can_bus.recv(timeout=0) is not None:
        pass

    last_frame_time = time.time()
    last_absence_check = time.time()
    last_reconnect_attempt = 0.0  # allows the first stall check to fire immediately

    while logging_active:
        try:
            msg = can_bus.recv(timeout=1.0)
            now = time.time()

            if msg and csv_writer_can:
                msg_id = f"0x{msg.arbitration_id:X}"
                data_hex = msg.data.hex()

                csv_writer_can.writerow([
                    now, "CAN", msg_id, msg.dlc, data_hex, msg.is_extended_id
                ])
                csv_file_can.flush()

                decoded = decode_mqb_message(msg_id, data_hex)
                push_message("CAN", {
                    "timestamp": now,
                    "id": msg_id,
                    "dlc": msg.dlc,
                    "data": data_hex,
                    "decoded": decoded
                })

                if msg.arbitration_id in OPTIMIZED_IDS:
                    handle_optimized_frame(msg, now, msg_id, data_hex)

                last_frame_time = now

            elif msg is None:
                silence = now - last_frame_time
                cooldown_elapsed = (now - last_reconnect_attempt) > STALL_RECONNECT_COOLDOWN_S
                if logging_active and silence > CAN_STALL_TIMEOUT_S and cooldown_elapsed:
                    write_diagnostic_event(f"can_bus_stall_detected_{silence:.1f}s")
                    last_reconnect_attempt = now
                    if reconnect_can_bus():
                        while can_bus.recv(timeout=0) is not None:
                            pass
                    # last_frame_time deliberately NOT reset here - if the
                    # bus really is just asleep, the next stall check is
                    # governed by the cooldown, not another 10s timer

            if now - last_absence_check >= 1.0:
                check_optimized_absence(now)
                last_absence_check = now

        except Exception as e:
            if logging_active:
                print(f"CAN recv error: {e}")
                traceback.print_exc()
                write_diagnostic_event(f"can_recv_exception:{e}")
                # Same cooldown as the stall watchdog - protects against
                # a tight reconnect loop if some other exception turns
                # out to be persistent/recurring (as data_length_code
                # was before it was fixed).
                now = time.time()
                if (now - last_reconnect_attempt) > STALL_RECONNECT_COOLDOWN_S:
                    last_reconnect_attempt = now
                    reconnect_can_bus()
                last_frame_time = time.time()
            time.sleep(0.1)

def esp32_logger_thread():
    """Background thread that captures ESP32 serial output"""
    try:
        ser = serial.Serial(ESP32_SERIAL_PORT, ESP32_BAUD_RATE, timeout=1.0)
        print(f"[ESP32] Serial opened on {ESP32_SERIAL_PORT} at {ESP32_BAUD_RATE} baud")
    except Exception as e:
        print(f"[ESP32] Failed to open serial port {ESP32_SERIAL_PORT}: {e}")
        return

    while logging_active:
        try:
            line = ser.readline()
            if line:
                ts = time.time()
                decoded_line = line.decode("utf-8", errors="replace").rstrip()
                if csv_writer_esp and logging_active:
                    csv_writer_esp.writerow([ts, "SERIAL", decoded_line, "", "", ""])
                    csv_file_esp.flush()
        except Exception as e:
            if logging_active:
                print(f"[ESP32] Serial read error: {e}")
            time.sleep(0.1)

    try:
        ser.close()
        print(f"[ESP32] Serial port closed")
    except Exception:
        pass

# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start_log", methods=["POST"])
def start_log():
    global logging_active, can_bus, log_thread_can, log_thread_esp
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

        if can_bus is None:
            print("Initializing CAN bus on can0...")
            can_bus = can.interface.Bus(channel='can0', interface='socketcan')
            print("CAN bus initialized")

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
        csv_writer_can.writerow([ts, "EVENT", "start_log", "", "", ""])
        csv_file_can.flush()
        csv_writer_esp.writerow([ts, "EVENT", "start_log", "", "", ""])
        csv_file_esp.flush()
        csv_writer_opt.writerow([ts, "EVENT", "start_log", "", "", ""])
        csv_file_opt.flush()
        push_message("EVENT", {"timestamp": ts, "event": "start_log"})

        log_thread_can = threading.Thread(target=can_logger_thread, daemon=True)
        log_thread_can.start()

        log_thread_esp = threading.Thread(target=esp32_logger_thread, daemon=True)
        log_thread_esp.start()

        print("Capture threads started")

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
    global logging_active, log_thread_can, log_thread_esp
    global csv_writer_can, csv_writer_esp, csv_writer_opt
    global csv_file_can, csv_file_esp, csv_file_opt

    if not logging_active:
        return jsonify({"status": "not_running"})

    ts = time.time()
    if csv_writer_can:
        csv_writer_can.writerow([ts, "EVENT", "stop_log", "", "", ""])
        csv_file_can.flush()
    if csv_writer_esp:
        csv_writer_esp.writerow([ts, "EVENT", "stop_log", "", "", ""])
        csv_file_esp.flush()
    if csv_writer_opt:
        csv_writer_opt.writerow([ts, "EVENT", "stop_log", "", "", ""])
        csv_file_opt.flush()
    push_message("EVENT", {"timestamp": ts, "event": "stop_log"})

    logging_active = False

    if log_thread_can:
        log_thread_can.join(timeout=2.0)
        log_thread_can = None
    if log_thread_esp:
        log_thread_esp.join(timeout=2.0)
        log_thread_esp = None

    if csv_file_can:
        csv_file_can.close()
        csv_file_can = None
        csv_writer_can = None
    if csv_file_esp:
        csv_file_esp.close()
        csv_file_esp = None
        csv_writer_esp = None
    if csv_file_opt:
        csv_file_opt.close()
        csv_file_opt = None
        csv_writer_opt = None

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
        "can_interface": "can0",
        "can_log": log_file_can,
        "esp_log": log_file_esp,
        "opt_log": log_file_opt,
        "esp32_serial_port": ESP32_SERIAL_PORT
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
