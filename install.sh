#!/usr/bin/env bash
# Ajoute « Utilisation Claude » aux applications Ubuntu (icône + entrée de
# menu) pour l'utilisateur courant. Retrait : ./install.sh --uninstall
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/scalable/apps"
DESKTOP="$APPS/local.ClaudeUsage.desktop"
ICON="$ICONS/claude-usage.svg"

refresh_caches() {
  update-desktop-database "$APPS" 2>/dev/null || true
  gtk-update-icon-cache -f -t "$DATA/icons/hicolor" 2>/dev/null || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$DESKTOP" "$ICON"
  refresh_caches
  echo "Lanceur retiré."
  exit 0
fi

mkdir -p "$APPS" "$ICONS"
install -m 644 "$DIR/claude-usage.svg" "$ICON"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Utilisation Claude
Comment=Suivi de l'utilisation du forfait Claude (session, semaine, crédits)
Exec=/usr/bin/python3 "$DIR/claude_usage.py"
Icon=claude-usage
Terminal=false
Categories=Utility;Monitor;
Keywords=claude;usage;utilisation;quota;limite;
StartupWMClass=local.ClaudeUsage
Actions=hidden;

[Desktop Action hidden]
Name=Démarrer dans la barre
Exec=/usr/bin/python3 "$DIR/claude_usage.py" --hidden
EOF
chmod 644 "$DESKTOP"

refresh_caches
echo "Lanceur installé : $DESKTOP"
echo "Cherchez « Utilisation Claude » dans la vue Activités."
