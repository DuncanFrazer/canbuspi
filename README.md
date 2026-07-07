
# CANBus Pi Project

This project provides a Raspberry Pi–based CAN bus sniffer and manual event logging tool, with a browser‑based UI.

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
sudo apt install python3-flask python3-can
```

### Configure CAN Interface for Modern Raspberry Pi OS

1. **Load CAN kernel modules on boot:**
```bash
sudo nano /etc/modules-load.d/can.conf
```
Add these lines:
```
can
can_raw
mcp251x
```

2. **Create systemd service for CAN interface setup:**
```bash
sudo nano /etc/systemd/system/can0-setup.service
```
Add:
```ini
[Unit]
Description=Setup CAN0 Interface
After=systemd-modules-load.service
Before=network.target
Before=canbuspi.service

[Service]
Type=oneshot
ExecStart=/sbin/ip link set can0 type can bitrate 500000
ExecStart=/sbin/ip link set can0 up
ExecStop=/sbin/ip link set can0 down
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

3. **Enable and start the CAN interface service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable can0-setup.service
sudo systemctl start can0-setup.service
```

4. **Verify CAN interface:**
```bash
ip link show can0
# Should show can0 in UP state
```

5. **Test CAN communication (optional):**
```bash
candump can0
# Press Ctrl+C to stop
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
After=can0-setup.service

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
ip link show can0
sudo systemctl status can0-setup.service
sudo systemctl status canbuspi.service
curl http://localhost:5000
```

4. **Access the web interface:**
Navigate to `http://<pi-ip-address>:5000`

## Hardware Requirements

- Raspberry Pi (tested with Pi Zero 2 W)
- MCP2515 CAN bus module (SPI interface)
- CAN bus connection to vehicle
- Proper CAN bus termination (120Ω resistors)

## Troubleshooting

### CAN Interface Issues

**Interface shows UP but candump says "network is down":**
```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000
sudo ip link set can0 up
```

**No CAN messages when car is locked:**
- Modern cars put CAN bus to sleep when locked
- Unlock car or turn on ignition to see traffic

**MCP2515 not detected:**
```bash
dmesg | grep -i mcp
lsmod | grep mcp
```

**Check SPI connection:**
```bash
ls /dev/spi*
```

### Service Issues

**Check service status:**
```bash
sudo systemctl status can0-setup.service
sudo systemctl status canbuspi.service
```

**View service logs:**
```bash
sudo journalctl -u can0-setup.service -n 20
sudo journalctl -u canbuspi.service -n 20
```

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

