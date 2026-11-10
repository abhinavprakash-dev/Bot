#!/bin/sh
set -u

setpriv --reuid=323 --regid=323 --clear-groups \
    env -i HOME=/home/spotify PATH=/usr/bin:/bin \
    SPOTIFY_TOKENER_ADDR=127.0.0.1:8081 \
    SPOTIFY_TOKENER_CHROME_PATH=/usr/local/bin/spotify-chrome \
    /usr/local/bin/spotify-tokener &
tokener_pid=$!

attempt=0
while [ "$attempt" -lt 30 ]; do
    status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
        --max-time 2 http://127.0.0.1:8081/ || true)
    if [ "$status" != "000" ]; then
        break
    fi
    if ! kill -0 "$tokener_pid" 2>/dev/null; then
        echo "Spotify Tokener exited before it became ready." >&2
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done

# -Dlogback.configurationFile is left alone; the loggers for the YouTube OAuth
# handler are raised to INFO in application.yml so the device-code URL and the
# resulting refresh token are always visible in the deploy log.
setpriv --reuid=322 --regid=322 --clear-groups \
    java -Djava.awt.headless=true -jar /opt/Lavalink/Lavalink.jar &
lavalink_pid=$!

echo "[start-services] Lavalink starting. If YouTube OAuth is enabled, watch for"
echo "[start-services] a device-code URL from dev.lavalink.youtube in the log below."

stop_services() {
    kill "$lavalink_pid" "$tokener_pid" 2>/dev/null || true
}
trap stop_services INT TERM

wait "$lavalink_pid"
status=$?
kill "$tokener_pid" 2>/dev/null || true
wait "$tokener_pid" 2>/dev/null || true
exit "$status"
