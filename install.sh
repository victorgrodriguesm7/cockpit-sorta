#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' 'Execute com sudo: sudo sh install.sh' >&2
  exit 1
fi
command -v python3 >/dev/null
command -v findmnt >/dev/null
command -v lsblk >/dev/null
install -d -m 755 /usr/share/cockpit/sorta /usr/local/libexec/cockpit-sorta/migrations
for file in manifest.json index.html sorta.css sorta.js; do
  install -o root -g root -m 644 "$file" "/usr/share/cockpit/sorta/$file"
done
install -o root -g root -m 755 helper.py /usr/local/libexec/cockpit-sorta/helper.py
for file in migrations/*.sql; do
  install -o root -g root -m 644 "$file" /usr/local/libexec/cockpit-sorta/migrations/
done
printf '%s\n' 'Sorta instalado. Atualize o Cockpit e abra Sorta · Mídia.'
