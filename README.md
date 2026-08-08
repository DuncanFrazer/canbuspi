
# CANBus Pi Project

This project provides a Raspberry Pi–based CAN bus sniffer and manual event logging tool, with a browser‑based UI.
Serial-log version : all messages are delivered over serial, no direct canbus connection. The Pi will be performing the key task of logging messages to a file, and recording events such as unlock/lock of the car. This vesion is designed to support a matching esp32 app that is monitoring the canbus and will push all messages out of its serial port. The evolution of this project allows the esp32 and its canbus interface to be installed in the car, but the pi can be removed and simply connected via usb to the esp32.

## Main Capabilities
- Start/stop data logging to CSV
- Real-time CAN message viewer with 20Hz updates
- Automatic message decoding (RPM, gear position, gearbox mode, ambient light)
- Manual event tagging (lock/unlock/gear selection/park pilot/camera stow)
- Message rate monitoring and statistics
- Flask-powered local web UI
- Background CAN capture with threading
- Auto-start on boot with systemd service

## Setup

### Install Dependencies
```bash
sudo apt update
sudo apt install python3-flask
```

### Create Systemd Service for Auto-Start

To auto-start the Flask app on boot:

```bash
sudo nano /etc/systemd/system/canbuspi.service
```

Add:
```ini
[Unit]
Description=CANBus Pi Logger
After=network.target

[Service]
Type=simple
User=duncan
WorkingDirectory=/home/duncan/canbuspi
ExecStart=/usr/bin/python3 /home/duncan/canbuspi/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable canbuspi.service
sudo systemctl start canbuspi.service
sudo systemctl status canbuspi.service
```

### Complete Setup and Test

1. **Create log directory:**
```bash
sudo mkdir -p /home/duncan/canlogs
sudo chown duncan:duncan /home/duncan/canlogs
```

2. **Reboot to test auto-start:**
```bash
sudo reboot
```

3. **Verify everything works after reboot:**
```bash
sudo systemctl status canbuspi.service
curl http://localhost:5000
```

4. **Access the web interface:**
Navigate to `http://<pi-ip-address>:5000`

## Web Interface Features

The web interface provides:
- **Logging Control**: Start/stop CAN data logging
- **Live Messages**: Real-time CAN message display with decoding
- **Event Tagging**: Manual event buttons for:
  - Lock/Unlock
  - Gear selection (P/R/N/D)
  - Park Pilot toggle ON/OFF
  - Camera Stow
- **Statistics**: Message rate and total count

## Running Manually
```bash
cd /home/duncan/canbuspi
python3 app.py
```
Then navigate to `http://localhost:5000`

