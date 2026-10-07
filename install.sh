#!/usr/bin/env bash
# Uso:  ./install.sh          crea el enlace ~/.local/bin/fpga
#       ./install.sh udev     además instala la regla udev del USB-Blaster (pide sudo)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin"
ln -sf "$HERE/fpga" "$HOME/.local/bin/fpga"
echo "Enlace creado: ~/.local/bin/fpga -> $HERE/fpga"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "Agrega a ~/.bashrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
if [ "$1" = "udev" ]; then
  sudo cp "$HERE/99-usbblaster.rules" /etc/udev/rules.d/
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  echo "Regla udev instalada. Desconecta y vuelve a conectar el USB-Blaster."
fi
echo "Siguiente: fpga install-quartus (si aún no tienes Quartus) y luego fpga doctor"
