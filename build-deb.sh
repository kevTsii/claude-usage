#!/usr/bin/env bash
# Construit le paquet Debian « claude-usage » dans dist/.
#
#   Construire  : ./build-deb.sh [version]
#   Installer   : sudo apt install ./dist/claude-usage_<version>_all.deb
#   Désinstaller: sudo apt remove claude-usage
#
# Une fois installé, le dossier du projet n'est plus nécessaire.
set -euo pipefail

NAME="claude-usage"
VERSION="${1:-1.0.0}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$DIR/dist/${NAME}_${VERSION}_all.deb"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

LIB="$STAGE/usr/lib/$NAME"
ICONS="$STAGE/usr/share/icons/hicolor/scalable/apps"
APPS="$STAGE/usr/share/applications"

install -d "$STAGE/DEBIAN" "$STAGE/usr/bin" "$LIB" "$ICONS" "$APPS"

# Code de l'application et icône
install -m 644 "$DIR/claude_usage.py" "$DIR/status_tray.py" "$LIB/"
install -m 644 "$DIR/claude-usage.svg" "$ICONS/claude-usage.svg"

# Commande « claude-usage » (accepte --hidden et --text)
cat > "$STAGE/usr/bin/$NAME" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/claude-usage/claude_usage.py "$@"
EOF
chmod 755 "$STAGE/usr/bin/$NAME"

# Entrée du menu des applications
cat > "$APPS/local.ClaudeUsage.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=Utilisation Claude
Comment=Suivi de l'utilisation du forfait Claude (session, semaine, crédits)
Exec=claude-usage
Icon=claude-usage
Terminal=false
Categories=Utility;Monitor;
Keywords=claude;usage;utilisation;quota;limite;
StartupWMClass=local.ClaudeUsage
Actions=hidden;

[Desktop Action hidden]
Name=Démarrer dans la barre
Exec=claude-usage --hidden
EOF
chmod 644 "$APPS/local.ClaudeUsage.desktop"

SIZE="$(du -sk "$STAGE/usr" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, python3-cairo, gir1.2-gtk-4.0, gir1.2-adw-1
Maintainer: Local <noreply@localhost>
Description: Suivi de l'utilisation du forfait Claude
 Fenêtre GTK4 et icône dans la barre GNOME qui affichent la session de 5 h,
 l'utilisation hebdomadaire, les crédits et la répartition par produit.
 Les données viennent de la session de Claude Code (connexion via
 « claude auth login »).
EOF

mkdir -p "$DIR/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null
echo "Paquet créé : $OUT"
echo "Installation : sudo apt install \"$OUT\""
