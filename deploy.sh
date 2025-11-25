#!/bin/bash
# Deploy CANBus Pi project to Raspberry Pi
# Usage: ./deploy.sh

PI_USER="duncan"
PI_HOST="192.168.68.78"
PI_PASSWORD="trudi"
PROJECT_DIR="/home/duncan/canbuspi"

echo "Deploying CANBus Pi to ${PI_USER}@${PI_HOST}..."

# Use sshpass for automatic password authentication
# Install with: brew install hudochenkov/sshpass/sshpass (on macOS)

# Create project directory on Pi
sshpass -p "${PI_PASSWORD}" ssh -o StrictHostKeyChecking=no ${PI_USER}@${PI_HOST} "mkdir -p ${PROJECT_DIR}/templates"

# Copy files to Pi
echo "Copying files..."
sshpass -p "${PI_PASSWORD}" scp -o StrictHostKeyChecking=no app.py ${PI_USER}@${PI_HOST}:${PROJECT_DIR}/
sshpass -p "${PI_PASSWORD}" scp -o StrictHostKeyChecking=no templates/index.html ${PI_USER}@${PI_HOST}:${PROJECT_DIR}/templates/
sshpass -p "${PI_PASSWORD}" scp -o StrictHostKeyChecking=no README.md ${PI_USER}@${PI_HOST}:${PROJECT_DIR}/

# Install dependencies and configure
echo "Installing dependencies..."
sshpass -p "${PI_PASSWORD}" ssh ${PI_USER}@${PI_HOST} << 'ENDSSH'
cd /home/duncan/canbuspi
sudo apt update
sudo apt install -y python3-flask python3-can python3-pip
sudo mkdir -p /home/duncan/canlogs
sudo chown duncan:duncan /home/duncan/canlogs

# Check CAN interface status
echo "Checking CAN interface..."
ip link show can0 || echo "WARNING: can0 interface not found - needs configuration"

echo "Deployment complete!"
echo "To start the app: cd /home/duncan/canbuspi && python3 app.py"
ENDSSH

echo ""
echo "Deployment finished!"
echo "Access the web UI at: http://192.168.68.78:5000"
