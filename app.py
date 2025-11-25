
from flask import Flask, render_template, jsonify, request
import time
import csv
import threading
import can
import os

app = Flask(__name__)
logging_active = False
log_file = "/home/pi/canlogs/current_log.csv"
can_bus = None
log_thread = None
csv_writer = None
csv_file_handle = None

def ensure_log_directory():
    os.makedirs("/home/pi/canlogs", exist_ok=True)

def write_event(event):
    """Write a manual event tag to the log"""
    if csv_writer and logging_active:
        csv_writer.writerow([time.time(), "EVENT", event, "", "", ""])
        csv_file_handle.flush()

def can_logger_thread():
    """Background thread that captures CAN messages"""
    global csv_writer, csv_file_handle
    while logging_active:
        try:
            msg = can_bus.recv(timeout=1.0)
            if msg and csv_writer:
                # Format: timestamp, type, arbitration_id, dlc, data, is_extended
                csv_writer.writerow([
                    msg.timestamp,
                    "CAN",
                    f"0x{msg.arbitration_id:X}",
                    msg.dlc,
                    msg.data.hex(),
                    msg.is_extended_id
                ])
                csv_file_handle.flush()
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
        
        # Initialize CAN bus (socketcan interface)
        if can_bus is None:
            can_bus = can.interface.Bus(channel='can0', bustype='socketcan')
        
        # Open CSV file with headers
        csv_file_handle = open(log_file, "a", newline='')
        csv_writer = csv.writer(csv_file_handle)
        
        # Write header if file is new
        if os.path.getsize(log_file) == 0:
            csv_writer.writerow(["timestamp", "type", "id_or_event", "dlc", "data", "extended"])
        
        logging_active = True
        
        # Start background CAN capture thread
        log_thread = threading.Thread(target=can_logger_thread, daemon=True)
        log_thread.start()
        
        # Log the start event
        csv_writer.writerow([time.time(), "EVENT", "start_log", "", "", ""])
        csv_file_handle.flush()
        
        return jsonify({"status": "started"})
    except Exception as e:
        logging_active = False
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
