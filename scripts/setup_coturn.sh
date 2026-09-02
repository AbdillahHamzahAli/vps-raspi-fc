#!/usr/bin/env bash
# scripts/setup_coturn.sh - Setup coturn TURN server di VPS Ubuntu/Debian
# WAJIB untuk jaringan modem/CGNAT biar WebRTC bisa connect (<200ms tetap lewat relay TURN jika P2P gagal)
# Jalankan di VPS sebagai root: sudo bash scripts/setup_coturn.sh
set -e

TURN_USER=${TURN_USERNAME:-raspi}
TURN_PASS=${TURN_CREDENTIAL:-ganti_password_kuat}
VPS_IP=$(curl -s ifconfig.me || hostname -I | awk '{print $1}')

echo "=== Setup coturn ==="
echo "VPS_IP: $VPS_IP"
echo "TURN_USER: $TURN_USER"

apt update
apt install -y coturn

# backup
cp /etc/turnserver.conf /etc/turnserver.conf.bak.$(date +%s) 2>/dev/null || true

cat > /etc/turnserver.conf <<EOF
listening-port=3478
alt-listening-port=3479
listening-ip=0.0.0.0
external-ip=$VPS_IP
realm=$VPS_IP
server-name=$VPS_IP

lt-cred-mech
user=$TURN_USER:$TURN_PASS

# untuk WebRTC, butuh range port relay
min-port=49160
max-port=49200

# security
no-multicast-peers
no-cli
# verbose
verbose
fingerprint
EOF

# enable
sed -i 's/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/' /etc/default/coturn || echo "TURNSERVER_ENABLED=1" >> /etc/default/coturn

systemctl enable coturn
systemctl restart coturn
systemctl status coturn --no-pager -l | head -n 40

echo ""
echo "=== coturn siap ==="
echo "Test dari Raspi/Laptop:"
echo "  turnutils_uclient -u $TURN_USER -w $TURN_PASS -p 3478 $VPS_IP"
echo ""
echo "Buka firewall:"
echo "  ufw allow 3478/tcp; ufw allow 3478/udp; ufw allow 49160:49200/udp"
echo ""
echo "Set di .env kedua sisi:"
echo "  TURN_URL=turn:$VPS_IP:3478"
echo "  TURN_USERNAME=$TURN_USER"
echo "  TURN_CREDENTIAL=$TURN_PASS"
