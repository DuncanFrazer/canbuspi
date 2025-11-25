
from flask import Flask, render_template, jsonify, request
import time
import csv
import threading
import can
import os

app = Flask(__name__)
logging_active = False
log_file = "/home/duncan/canlogs/current_log.csv"
can_bus = None
log_thread = None
csv_writer = None
csv_file_handle = None

def ensure_log_directory():
    os.makedirs("/home/duncan/canlogs", exist_ok=True)

def write_event(event):
    """Write a manual event tag to the log"""
    if csv_writer and logging_active:
        csv_writer.writerow([time.time(), "EVENT", event, "", "", ""])
        csv_file_handle.flush()

def can_logger_thread():
    """Background thread that captures CAN messages"""
    global csv_writer, csv_file_handle, recent_messages
    while logging_active:
        try:
            msg = can_bus.recv(timeout=1.0)
            if msg and csv_writer:
                msg_id = f"0x{msg.arbitration_id:X}"
                data_hex = msg.data.hex()
                
                # Format: timestamp, type, arbitration_id, dlc, data, is_extended
                csv_writer.writerow([
                    msg.timestamp,
                    "CAN",
                    msg_id,
                    msg.dlc,
                    data_hex,
                    msg.is_extended_id
                ])
                csv_file_handle.flush()
                
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
                
                # Keep buffer size limited
                if len(recent_messages) > MAX_RECENT_MESSAGES:
                    recent_messages.pop(0)
                    
        except Exception as e:
            if logging_active:
                print(f"CAN recv error: {e}")
            time.sleep(0.1)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start_log", methods=["POST"])
def start_log():
    global logging_active, can_bus, log_thread, csv_writer, csv_file_handle
    
    if logging_active:
        return jsonify({"status": "already_running"})
    
    try:
        ensure_log_directory()
        print(f"Log directory ensured: /home/duncan/canlogs")
        
        # Initialize CAN bus (socketcan interface)
        if can_bus is None:
            print("Initializing CAN bus on can0...")
            can_bus = can.interface.Bus(channel='can0', bustype='socketcan')
            print("CAN bus initialized successfully")
        
        # Open CSV file with headers
        print(f"Opening log file: {log_file}")
        csv_file_handle = open(log_file, "a", newline='')
        csv_writer = csv.writer(csv_file_handle)
        
        # Write header if file is new or empty
        try:
            if os.path.getsize(log_file) == 0:
                csv_writer.writerow(["timestamp", "type", "id_or_event", "dlc", "data", "extended"])
                print("Wrote CSV header")
        except FileNotFoundError:
            csv_writer.writerow(["timestamp", "type", "id_or_event", "dlc", "data", "extended"])
            print("Created new file with CSV header")
        
        logging_active = True
        
        # Start background CAN capture thread
        log_thread = threading.Thread(target=can_logger_thread, daemon=True)
        log_thread.start()
        print("CAN logger thread started")
        
        # Log the start event
        csv_writer.writerow([time.time(), "EVENT", "start_log", "", "", ""])
        csv_file_handle.flush()
        
        return jsonify({"status": "started"})
    except Exception as e:
        logging_active = False
        print(f"ERROR in start_log: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stop_log", methods=["POST"])
def stop_log():
    global logging_active, log_thread, csv_writer, csv_file_handle
    
    if not logging_active:
        return jsonify({"status": "not_running"})
    
    # Log stop event before shutting down
    if csv_writer:
        csv_writer.writerow([time.time(), "EVENT", "stop_log", "", "", ""])
        csv_file_handle.flush()
    
    logging_active = False
    
    # Wait for thread to finish
    if log_thread:
        log_thread.join(timeout=2.0)
        log_thread = None
    
    # Close CSV file
    if csv_file_handle:
        csv_file_handle.close()
        csv_file_handle = None
        csv_writer = None
    
    return jsonify({"status": "stopped"})

@app.route("/action", methods=["POST"])
def action():
    data = request.get_json()
    ev = data.get("event")
    write_event(ev)
    return jsonify({"ok": True})

@app.route("/live")
def live():
    """Return last 100 lines from the log file"""
    try:
        if os.path.exists(log_file):
            with open(log_file) as f:
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
        "log_file": log_file
    })

# Store recent messages for live view (circular buffer)
recent_messages = []
MAX_RECENT_MESSAGES = 1000

@app.route("/stream")
def stream():
    """Server-sent events stream for real-time CAN messages"""
    def generate():
        last_index = 0
        while True:
            if len(recent_messages) > last_index:
                # Send new messages
                for msg in recent_messages[last_index:]:
                    yield f"data: {msg}\n\n"
                last_index = len(recent_messages)
            time.sleep(0.05)  # 20Hz update rate
    
    return app.response_class(generate(), mimetype='text/event-stream')

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
