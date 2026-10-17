#!/data/data/com.termux/files/usr/bin/bash
# KAIROS Phone Agent - Termux Setup & Run Script
# Run this inside Termux on your Android device.

echo "========================================================"
echo "         KAIROS Phone Agent - Termux Setup"
echo "========================================================"
echo ""

# 1. Update packages and install prerequisites
echo "[1/4] Installing Python and Termux API packages..."
pkg update -y
pkg install -y python termux-api git

# 2. Install Python dependencies
echo "[2/4] Installing websockets..."
pip install --upgrade pip
pip install websockets

# 3. Reminder for Termux:API app
echo ""
echo "========================================================"
echo "IMPORTANT: Termux:API App must be installed on your phone!"
echo "Download from F-Droid: https://f-droid.org/packages/com.termux.api/"
echo "Then grant Termux:API permissions in Android Settings:"
echo "  - Notification Access"
echo "  - SMS permission"
echo "  - Location permission (if using place watchers)"
echo "========================================================"
echo ""

# 4. Prompt for Hub IP
read -p "Enter your PC's IP address (e.g. 192.168.1.100): " PC_IP
if [ -z "$PC_IP" ]; then
    PC_IP="192.168.1.100"
fi

HUB_URL="ws://${PC_IP}:8000"
echo "Connecting to Hub at: ${HUB_URL}"
echo ""

# 5. Launch phone agent
python phone_agent.py --hub "$HUB_URL" --device phone-arjun --chat --location
