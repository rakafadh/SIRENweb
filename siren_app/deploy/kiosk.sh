#!/usr/bin/env bash
# Opens SIREN full screen once the server answers. Started at login by
# ~/.config/autostart/siren-kiosk.desktop. Close with Alt+F4.
URL="http://127.0.0.1:8000/"

for _ in $(seq 1 90); do
    curl -fsS -o /dev/null "$URL" && break
    sleep 1
done

# Keep the 7-inch screen awake during a transport (X11 only; harmless elsewhere).
if command -v xset >/dev/null 2>&1; then
    xset s off -dpms s noblank 2>/dev/null || true
fi

BROWSER="$(command -v chromium || command -v chromium-browser)"
exec "$BROWSER" \
    --kiosk --app="$URL" \
    --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
    --no-first-run --password-store=basic --disable-features=Translate \
    --overscroll-history-navigation=0 --disable-pinch \
    --check-for-update-interval=31536000
