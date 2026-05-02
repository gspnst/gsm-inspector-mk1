#!/bin/bash
# ============================================================
# TowerScan Setup Script v2
# Raspberry Pi OS Bookworm (64-bit) + Nooelec NESDR SMArt XTR
# ============================================================
set -e

SUDO=""
[ "$EUID" -ne 0 ] && SUDO="sudo"

echo "╔══════════════════════════════════════════╗"
echo "║       TowerScan Setup v2                 ║"
echo "║  Raspberry Pi 5 + Nooelec NESDR XTR      ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Step 1: System packages ───────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
$SUDO apt-get update -qq
$SUDO apt-get install -y \
  rtl-sdr librtlsdr-dev librtlsdr0 \
  python3-pip python3-requests \
  git build-essential cmake pkg-config \
  libboost-all-dev libcppunit-dev \
  swig doxygen \
  gnuradio gnuradio-dev \
  gr-osmosdr \
  libosmocore-dev \
  libosmosdr-dev \
  tshark wireshark-common \
  autoconf automake libtool \
  libfftw3-dev

# ── Step 2: kalibrate-rtl from source ────────────────────────────────────────
echo ""
echo "[2/7] Building kalibrate-rtl from source..."
if command -v kal &>/dev/null; then
  echo "  Already installed."
else
  TMP=$(mktemp -d)
  git clone --depth=1 https://github.com/steve-m/kalibrate-rtl "$TMP/kal"
  cd "$TMP/kal"
  ./bootstrap && ./configure && make -j$(nproc)
  $SUDO make install
  cd -
  rm -rf "$TMP"
  echo "  Installed to $(which kal)"
fi

# ── Step 3: gr-gsm from source ───────────────────────────────────────────────
echo ""
echo "[3/7] Building gr-gsm from source (this takes ~10 min on Pi 5)..."
if command -v grgsm_scanner &>/dev/null; then
  echo "  Already installed — skipping."
else
  GR_DIR="$HOME/gr-gsm-src"
  [ -d "$GR_DIR" ] && rm -rf "$GR_DIR"
  git clone --depth=1 https://github.com/ptrkrysik/gr-gsm "$GR_DIR"

  # Apply device.py fix proactively
  DEVPY="$GR_DIR/python/receiver/device.py"
  if [ -f "$DEVPY" ]; then
    python3 - "$DEVPY" << 'PYEOF'
import sys, re
path = sys.argv[1]
with open(path) as f:
    src = f.read()

new_match = '''def match(dev, filters):
    dev_str = dev.to_string()
    if isinstance(filters, dict):
        for k, v in filters.items():
            if k + "=" + v not in dev_str:
                return False
    return True'''

src = re.sub(r'def match\(dev, filters\):.*?return True',
             new_match, src, flags=re.DOTALL)
with open(path, 'w') as f:
    f.write(src)
print("  device.py patched.")
PYEOF
  fi

  mkdir -p "$GR_DIR/build"
  cd "$GR_DIR/build"
  cmake .. \
    -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DCMAKE_BUILD_TYPE=Release \
    -Wno-dev
  make -j$(nproc)
  $SUDO make install
  $SUDO ldconfig
  cd -
  echo "  gr-gsm installed."
fi

# ── Step 4: Kernel module blacklist ──────────────────────────────────────────
echo ""
echo "[4/7] Blacklisting DVB-T kernel modules..."
BFILE="/etc/modprobe.d/blacklist-rtl.conf"
for mod in dvb_usb_rtl28xxu rtl2832 rtl2830; do
  if ! grep -q "$mod" "$BFILE" 2>/dev/null; then
    echo "blacklist $mod" | $SUDO tee -a "$BFILE" > /dev/null
    echo "  Blacklisted: $mod"
  fi
done
$SUDO modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2830 2>/dev/null || true

# ── Step 5: udev rules ────────────────────────────────────────────────────────
echo ""
echo "[5/7] Setting udev rules for RTL-SDR..."
cat << 'EOF' | $SUDO tee /etc/udev/rules.d/20-rtlsdr.rules > /dev/null
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666"
EOF
$SUDO udevadm control --reload-rules && $SUDO udevadm trigger
$SUDO usermod -a -G plugdev "$USER" 2>/dev/null || true
echo "  udev rules set. You may need to re-plug the dongle."

# ── Step 6: USB power (Pi 5 specific) ────────────────────────────────────────
echo ""
echo "[6/7] Configuring USB power for Pi 5..."
CONF="/boot/firmware/config.txt"
if ! grep -q "usb_max_current_enable" "$CONF" 2>/dev/null; then
  echo "usb_max_current_enable=1" | $SUDO tee -a "$CONF" > /dev/null
  echo "  USB max current enabled."
else
  echo "  Already configured."
fi

# ── Step 7: Python deps ───────────────────────────────────────────────────────
echo ""
echo "[7/7] Installing Python dependencies..."
pip3 install requests --break-system-packages 2>/dev/null || pip3 install requests

# ── Verify ────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          Setup complete!                 ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Verifying RTL-SDR dongle..."
if rtl_test 2>&1 | grep -q "Found"; then
  rtl_test 2>&1 | grep -E "Found|Tuner|Crystal" | head -5
else
  echo "  Dongle not detected — plug it in and try: rtl_test"
fi

echo ""
echo "NEXT STEPS:"
echo ""
echo "1. REBOOT (required for USB power + module blacklist):"
echo "   sudo reboot"
echo ""
echo "2. Download OpenCelliD data for Czech Republic (free, ~10MB):"
echo "   Register at https://opencellid.org/register"
echo "   wget -O cell_towers.csv.gz \\"
echo "     'https://opencellid.org/ocid/downloads?token=TOKEN&type=mcc&file=mcc-230.csv.gz'"
echo ""
echo "3. Import nearby towers (instant map population):"
echo "   python3 scan.py --import-ocid --lat 50.0759 --lon 14.4378"
echo ""
echo "4. Run RF scan:"
echo "   python3 scan.py --scan --lat 50.0759 --lon 14.4378 --bands GSM-900 GSM-1800"
echo ""
echo "5. Start map server:"
echo "   python3 server.py"
echo "   → http://localhost:5000"
echo ""
echo "6. (Optional) Calibrate PPM offset:"
echo "   kal -s GSM-900 -g 40"
