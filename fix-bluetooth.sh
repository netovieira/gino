#!/usr/bin/env bash
# Reset do controlador xHCI morto (0000:07:00.1) e reconexao do mouse Bluetooth.
# Uso: sudo bash ~/jarvis/fix-bluetooth.sh
set -u

CTRL="0000:07:00.1"
BRIDGE="0000:03:08.0"
MOUSE_MAC="DC:71:96:6A:5C:97"

if [ "$(id -u)" -ne 0 ]; then
    echo "Rode com sudo: sudo bash $0" >&2
    exit 1
fi

bt_present() {
    lsusb -d 8087: 2>/dev/null | grep -q .
}

echo "== 1/4 resetando controlador $CTRL =="
echo 1 > "/sys/bus/pci/devices/$CTRL/remove" 2>/dev/null
sleep 2
echo 1 > /sys/bus/pci/rescan
sleep 3

if ! bt_present; then
    echo "-- adaptador ausente, recarregando btusb --"
    modprobe -r btusb 2>/dev/null
    sleep 1
    modprobe btusb 2>/dev/null
    sleep 3
fi

if ! bt_present; then
    echo "-- ainda ausente, resetando a bridge $BRIDGE (usb3/4 serao reenumerados) --"
    echo 1 > "/sys/bus/pci/devices/$BRIDGE/remove" 2>/dev/null
    sleep 2
    echo 1 > /sys/bus/pci/rescan
    sleep 5
fi

echo "== 2/4 procurando hci0 =="
for _ in $(seq 1 15); do
    [ -e /sys/class/bluetooth/hci0 ] && break
    sleep 1
done

if [ ! -e /sys/class/bluetooth/hci0 ]; then
    echo "FALHOU: nenhum hci0 apareceu."
    echo "Dispositivos Intel no USB:"; lsusb -d 8087:
    exit 2
fi
echo "hci0 OK: $(cat /sys/class/bluetooth/hci0/address 2>/dev/null)"

echo "== 3/4 ligando adaptador =="
systemctl restart bluetooth
sleep 2
bluetoothctl power on
bluetoothctl show | head -6

echo "== 4/4 reconectando mouse $MOUSE_MAC =="
bluetoothctl connect "$MOUSE_MAC" || true
echo "-- conectados --"
bluetoothctl devices Connected
