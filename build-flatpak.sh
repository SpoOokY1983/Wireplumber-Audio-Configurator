#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# build-flatpak.sh  –  WirePlumber Audio Konfigurator als Flatpak bauen
# Ergebnis: io.github.wireplumber_audio_cfg.flatpak  (single-file bundle)
# Installieren: sudo flatpak install --system io.github.wireplumber_audio_cfg.flatpak
# ──────────────────────────────────────────────────────────────────────────────
set -e

APP_ID="io.github.wireplumber_audio_cfg"
RUNTIME_VER="50"
REPO_DIR="_repo"
BUILD_DIR="_build"
BUNDLE="${APP_ID}.flatpak"

# ── 1. Abhängigkeiten prüfen ──────────────────────────────────────────────────
echo "→ Prüfe Werkzeuge …"
for cmd in flatpak flatpak-builder; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "  Fehlt: $cmd  →  sudo apt install $cmd"
        MISSING=1
    fi
done
[ "${MISSING}" ] && exit 1

# ── 2. Flathub-Remote und GNOME-Runtime sicherstellen ─────────────────────────
echo "→ Flathub-Remote hinzufügen (falls noch nicht vorhanden) …"
sudo flatpak remote-add --system --if-not-exists flathub \
    https://flathub.org/repo/flathub.flatpakrepo

echo "→ GNOME Platform ${RUNTIME_VER} installieren (falls noch nicht vorhanden) …"
sudo flatpak install --system --noninteractive flathub \
    "org.gnome.Platform//${RUNTIME_VER}" \
    "org.gnome.Sdk//${RUNTIME_VER}" || true

# ── 3. App in lokales Repository bauen (ohne direkte Installation) ─────────────
echo "→ App bauen …"
flatpak-builder \
    --force-clean \
    --repo="${REPO_DIR}" \
    "${BUILD_DIR}" \
    "${APP_ID}.json"

# ── 4. Single-file bundle erzeugen ────────────────────────────────────────────
echo "→ Bundle erstellen …"
flatpak build-bundle \
    "${REPO_DIR}" \
    "${BUNDLE}" \
    "${APP_ID}"

echo ""
echo "✓ Fertig: ${BUNDLE}  ($(du -sh "${BUNDLE}" | cut -f1))"
echo ""
echo "Installieren (systemweit):"
echo "    sudo flatpak install --system ${BUNDLE}"
echo ""
echo "Starten:"
echo "    flatpak run ${APP_ID}"
