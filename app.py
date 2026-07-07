from flask import Flask, render_template, jsonify, request
import time
import csv
import threading
import can
import os
import serial

app = Flask(__name__)
logging_active = False
log_file_can = None
log_file_esp = None
can_bus = None
log_thread_can = None
log_thread_esp = None
csv_writer_can = None
csv_writer_esp = None
csv_file_can = None
csv_file_esp = None

# ============================================================
# CONFIGURATION
# ============================================================
ESP32_SERIAL_PORT = "/dev/ttyACM0"
ESP32_BAUD_RATE = 115200
LOG_DIR = "/home/duncan/canlogs"

# Store recent messages for live view (circular buffer)
recent_messages = []
MAX_RECENT_MESSAGES = 1000

# ============================================================
# HELPERS
# ============================================================

def generate_log_filenames():
    """Generate a matched pair of timestamped log filenames"""
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    can_file = f"{LOG_DIR}/log_{timestamp}_can.csv"
    esp_file = f"{LOG_DIR}/log_{timestamp}_esp.csv"
    return can_file, esp_file

def ensure_log_directory():
    os.makedirs(LOG_DIR, exist_ok=True)

def write_event(event):
    """Write a manual event tag to both log files simultaneously"""
    ts = time.time()
    if csv_writer_can and logging_active:
        csv_writer_can.writerow([ts, "EVENT", event, "", "", ""])
        csv_file_can.flush()
    if csv_writer_esp and logging_active:
        csv_writer_esp.writerow([ts, "EVENT", event, "", "", ""])
        csv_file_esp.flush()

# ============================================================
# CAPTURE THREADS
# ============================================================

def can_logger_thread():
    """Background thread that captures CAN messages"""
    while logging_active:
        try:
            msg = can_bus.recv(timeout=1.0)
            if msg and csv_writer_can:
                msg_id = f"0x{msg.arbitration_id:X}"
                data_hex = msg.data.hex()

                csv_writer_can.writerow([
                    msg.timestamp,
                    "CAN",
                    msg_id,
                    msg.dlc,
                    data_hex,
                    msg.is_extended_id
                ])
                csv_file_can.flush()

                # Add to recent messages for live view
                decoded = decode_mqb_message(msg_id, data_hex)
                import json
                msg_data = {
                    "timestamp": msg.timestamp,
                    "id": msg_id,
                    "dlc": msg.dlc,
                    "data": data_hex,
                    "decoded": decoded
                }
                recent_messages.append(json.dumps(msg_data))

                if len(recent_messages) > MAX_RECENT_MESSAGES:
                    recent_messages.pop(0)

        except Exception as e:
            if logging_active:
                print(f"CAN recv error: {e}")
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
    global csv_writer_can, csv_writer_esp, csv_file_can, csv_file_esp
    global log_file_can, log_file_esp

    if logging_active:
        return jsonify({"status": "already_running"})

    try:
        ensure_log_directory()

        log_file_can, log_file_esp = generate_log_filenames()
        print(f"CAN log: {log_file_can}")
        print(f"ESP32 log: {log_file_esp}")

        # Initialize CAN bus
        if can_bus is None:
            print("Initializing CAN bus on can0...")
            can_bus = can.interface.Bus(channel='can0', interface='socketcan')
            print("CAN bus initialized")

        # Open CAN log file
        csv_file_can = open(log_file_can, "w", newline='')
        csv_writer_can = csv.writer(csv_file_can)
        csv_writer_can.writerow(["timestamp", "type", "id_or_event", "dlc", "data", "extended"])

        # Open ESP32 log file
        csv_file_esp = open(log_file_esp, "w", newline='')
        csv_writer_esp = csv.writer(csv_file_esp)
        csv_writer_esp.writerow(["timestamp", "type", "message", "", "", ""])

        logging_active = True

        # Write start event to both files
        ts = time.time()
        csv_writer_can.writerow([ts, "EVENT", "start_log", "", "", ""])
        csv_file_can.flush()
        csv_writer_esp.writerow([ts, "EVENT", "start_log", "", "", ""])
        csv_file_esp.flush()

        # Start capture threads
        log_thread_can = threading.Thread(target=can_logger_thread, daemon=True)
        log_thread_can.start()

        log_thread_esp = threading.Thread(target=esp32_logger_thread, daemon=True)
        log_thread_esp.start()

        print("Capture threads started")

        return jsonify({
            "status": "started",
            "can_log": log_file_can,
            "esp_log": log_file_esp
        })

    except Exception as e:
        logging_active = False
        print(f"ERROR in start_log: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stop_log", methods=["POST"])
def stop_log():
    global logging_active, log_thread_can, log_thread_esp
    global csv_writer_can, csv_writer_esp, csv_file_can, csv_file_esp

    if not logging_active:
        return jsonify({"status": "not_running"})

    # Write stop event to both files before shutting down
    ts = time.time()
    if csv_writer_can:
        csv_writer_can.writerow([ts, "EVENT", "stop_log", "", "", ""])
        csv_file_can.flush()
    if csv_writer_esp:
        csv_writer_esp.writerow([ts, "EVENT", "stop_log", "", "", ""])
        csv_file_esp.flush()

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

    return jsonify({
        "status": "stopped",
        "can_log": log_file_can,
        "esp_log": log_file_esp
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
        "esp32_serial_port": ESP32_SERIAL_PORT
    })

@app.route("/stream")
def stream():
    """Server-sent events stream for real-time CAN messages"""
    def generate():
        last_index = 0
        while True:
            if len(recent_messages) > last_index:
                for msg in recent_messages[last_index:]:
                    yield f"data: {msg}\n\n"
                last_index = len(recent_messages)
            time.sleep(0.05)  # 20Hz update rate

    return app.response_class(generate(), mimetype='text/event-stream')

# ============================================================
# MQB MESSAGE DECODE
# ============================================================

def decode_mqb_message(msg_id, data):
    """Decode known MQB platform messages"""
    msg_id_int = int(msg_id, 16) if isinstance(msg_id, str) else msg_id
    data_bytes = bytes.fromhex(data) if isinstance(data, str) else data

    decoded = None

    # Instrument cluster responses
    if msg_id_int == 0x77E and len(data_bytes) >= 5:
        if data_bytes[0] == 0x05 and data_bytes[1] == 0x62 and data_bytes[2] == 0x22 and data_bytes[3] == 0xD1:
            rpm = ((data_bytes[4] << 8) | data_bytes[5]) / 4
            decoded = f"RPM: {rpm:.0f}"
        elif data_bytes[0] == 0x04 and data_bytes[1] == 0x62 and data_bytes[2] == 0x22 and data_bytes[3] == 0x4D:
            brightness = data_bytes[4]
            decoded = f"Ambient Light: {brightness}/255"

    # Gearbox responses
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