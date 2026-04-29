#!/bin/bash
# ============================================================
# Cell Tower Scanner - Raspberry Pi 5 Setup Script
# Tested on Raspberry Pi OS Bookworm (Debian 12)
# ============================================================

set -e
echo "================================================"
echo " Cell Tower Scanner - Setup"
echo " Nooelec NESDR SMArt XTR + Raspberry Pi 5"
echo "================================================"
echo ""

# Detect if running as root
if [ "$EUID" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

echo "[1/6] Updating package lists..."
$SUDO apt-get update -qq

echo "[2/6] Installing RTL-SDR drivers and dependencies..."
$SUDO apt-get install -y \
  rtl-sdr \
  librtlsdr-dev \
  librtlsdr0 \
  gnuradio \
  gr-gsm \
  python3-pip \
  python3-requests \
  wireshark-common \
  tshark \
  git \
  build-essential \
  autoconf \
  automake \
  libtool \
  libfftw3-dev

echo "[2b/6] Building kalibrate-rtl from source..."
TMPDIR=$(mktemp -d)
git clone https://github.com/steve-m/kalibrate-rtl "$TMPDIR/kalibrate-rtl"
cd "$TMPDIR/kalibrate-rtl"
./bootstrap
./configure
make -j$(nproc)
$SUDO make install
cd -
rm -rf "$TMPDIR"
echo "  kalibrate-rtl installed to /usr/local/bin/kal"

echo "[3/6] Blacklisting DVB-T kernel module (required for RTL-SDR)..."
BLACKLIST="/etc/modprobe.d/blacklist-rtl.conf"
if ! grep -q "rtl2832" "$BLACKLIST" 2>/dev/null; then
  echo "blacklist dvb_usb_rtl28xxu" | $SUDO tee -a "$BLACKLIST"
  echo "blacklist rtl2832" | $SUDO tee -a "$BLACKLIST"
  echo "blacklist rtl2830" | $SUDO tee -a "$BLACKLIST"
  echo "  Blacklist entries added. Reboot required before first scan."
else
  echo "  Already blacklisted."
fi

echo "[4/6] Adding udev rules for RTL-SDR..."
cat << 'EOF' | $SUDO tee /etc/udev/rules.d/20-rtlsdr.rules
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
EOF
$SUDO udevadm control --reload-rules
$SUDO udevadm trigger
$SUDO usermod -a -G plugdev $USER 2>/dev/null || true

echo "[5/6] Installing Python dependencies..."
pip3 install requests --break-system-packages 2>/dev/null || pip3 install requests

echo "[6/6] Verifying RTL-SDR dongle..."
if rtl_test -t 2>&1 | grep -q "No supported"; then
  echo "  WARNING: RTL-SDR dongle not detected. Make sure it's plugged in."
  echo "  Try unplugging and replugging after setup is complete."
elif rtl_test -t 2>&1 | grep -q "Found"; then
  echo "  RTL-SDR dongle detected!"
  # Show device info
  rtl_test -t 2>&1 | grep -E "Found|Tuner|Crystal" || true
else
  echo "  Could not run rtl_test. Dongle may not be connected yet."
fi

echo ""
echo "================================================"
echo " Setup complete!"
echo "================================================"
echo ""
echo "NEXT STEPS:"
echo ""
echo "1. If prompted, REBOOT now (needed once for blacklist to take effect):"
echo "   sudo reboot"
echo ""
echo "2. Find your PPM offset (frequency calibration - do once):"
echo "   kal -s GSM-900 -d 0"
echo "   # Note the 'average absolute error' value (e.g. -12 ppm)"
echo ""
echo "3. Run your first scan (replace coordinates with your location):"
echo "   python3 scan.py --scan \\"
echo "     --lat 50.0755 --lon 14.4378 \\"  
echo "     --bands GSM-900 GSM-1800 \\"
echo "     --ppm 0 \\"
echo "     --gain 40"
echo ""
echo "4. Start the web map:"
echo "   python3 server.py"
echo "   # Open http://localhost:5000 or http://<pi-ip>:5000"
echo ""
echo "OPTIONAL: OpenCelliD API token for better tower coordinates:"
echo "   Register free at https://opencellid.org"
echo "   python3 scan.py --token YOUR_TOKEN_HERE"
echo ""
echo "TIPS:"
echo "  - Antenna: Mount the included antenna as high as possible"
echo "  - The XTR's TCXO gives excellent frequency stability"
echo "  - Run kal -s GSM-900 first to calibrate PPM offset"
echo "  - Use --gain 30-45 for best GSM reception"
echo "  - Outdoor scanning with a laptop will find more towers"
