
# CANBus Pi Project

This project provides a Raspberry Pi–based CAN bus sniffer and manual event logging tool, with a browser‑based UI.

## Main Capabilities
- Start/stop data logging
- Tag actions (lock/unlock/gear selection)
- Live CAN message viewer (placeholder)
- Automatic WiFi connection & persistent hostname
- Flask-powered local web UI

## Setup

### Install Dependencies
```bash
sudo apt install python3-flask python3-can python3-socketcan
```

### Configure CAN Interface to Auto-Start on Boot

1. **Load CAN kernel modules on boot:**
```bash
sudo nano /etc/modules
```
Add these lines:
```
can
can_raw
mcp251x
```

2. **Create network interface configuration:**
```bash
sudo nano /etc/network/interfaces.d/can0
```
Add:
```
auto can0
iface can0 inet manual
    pre-up /sbin/ip link set can0 type can bitrate 500000
    up /sbin/ifconfig can0 up
    down /sbin/ifconfig can0 down
```

3. **Enable the interface:**
```bash
sudo chmod 644 /etc/network/interfaces.d/can0
sudo reboot
```

4. **Verify CAN interface after reboot:**
```bash
ip link show can0
# Should show can0 in UP state
```

### Create Systemd Service (Optional)

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
User=pi
WorkingDirectory=/home/pi/canbuspi
ExecStart=/usr/bin/python3 /home/pi/canbuspi/app.py
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

## Running Manually
```bash
python3 app.py
```
Then navigate to `http://<pi-ip-address>:5000`

