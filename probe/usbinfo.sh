#!/usr/bin/env bash
lsusb
for v in /dev/video0 /dev/video2; do
  p=$(udevadm info -q path -n $v); echo "== $v $p"
  udevadm info -q property -n $v | grep -E "ID_VENDOR|ID_MODEL|ID_SERIAL|ID_REVISION|ID_USB_DRIVER"
done
id=$(udevadm info -q property -n /dev/video0 | sed -n 's/^ID_VENDOR_ID=//p'):$(udevadm info -q property -n /dev/video0 | sed -n 's/^ID_MODEL_ID=//p')
echo "vid:pid $id"
lsusb -v -d $id 2>/dev/null | grep -A12 -E "VideoControl Interface Descriptor:" | grep -E "bDescriptorSubtype|guidExtensionCode|bNumControls|bmControls|bControlSize|bUnitID|iExtension" | head -80
which uvcdynctrl; ls /usr/share/uvcdynctrl 2>/dev/null
