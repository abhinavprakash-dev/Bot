FROM alpine:3.22 AS lavasrc

RUN apk add --no-cache curl \
    && curl -fsSL --retry 3 \
        https://github.com/topi314/LavaSrc/releases/download/4.8.3/lavasrc-plugin-4.8.3.jar \
        -o /lavasrc-plugin.jar \
    && echo "4ec389bdae64fd381ee62fcdb902e15d9402823b1ad0b982bd5633619a07c1b3  /lavasrc-plugin.jar" \
        | sha256sum -c -

FROM eclipse-temurin:21-jdk-jammy AS soundy

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /soundy
RUN git clone https://github.com/lowkwrs/soundy.git . && git checkout e2c5da8e96661d2a9eb96ee53e3d5b95903bc194
RUN chmod +x gradlew && ./gradlew build --no-daemon
RUN test -s plugin/build/libs/soundy-plugin-1.1.0.jar

FROM golang:1.24-alpine AS spotify-tokener

RUN CGO_ENABLED=0 go install github.com/topi314/spotify-tokener@e15e48eb142a70d196932fa902363b191492e384

FROM ghcr.io/lavalink-devs/lavalink:4 AS lavalink

FROM eclipse-temurin:21-jre-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg util-linux \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && chmod a+r /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 322 lavalink \
    && useradd --uid 322 --gid lavalink --create-home --shell /usr/sbin/nologin lavalink \
    && groupadd --gid 323 spotify \
    && useradd --uid 323 --gid spotify --create-home --shell /usr/sbin/nologin spotify \
    && mkdir -p /opt/Lavalink/plugins \
    && chown -R 322:322 /opt/Lavalink

WORKDIR /opt/Lavalink
COPY --from=lavalink --chown=322:322 /opt/Lavalink/Lavalink.jar /opt/Lavalink/Lavalink.jar
COPY --from=lavasrc --chown=322:322 /lavasrc-plugin.jar /opt/Lavalink/plugins/lavasrc-plugin.jar
COPY --from=soundy --chown=322:322 /soundy/plugin/build/libs/soundy-plugin-1.1.0.jar /opt/Lavalink/plugins/soundy-plugin.jar
COPY --from=spotify-tokener --chown=322:322 /go/bin/spotify-tokener /usr/local/bin/spotify-tokener
COPY --chown=322:322 application.yml /opt/Lavalink/application.yml
COPY --chown=322:322 start-services.sh /opt/Lavalink/start-services.sh
COPY spotify-chrome.sh /usr/local/bin/spotify-chrome
RUN chmod 755 /usr/local/bin/spotify-chrome

ENV SPOTIFY_TOKENER_ADDR=127.0.0.1:8081 \
    SPOTIFY_TOKENER_CHROME_PATH=/usr/local/bin/spotify-chrome

# Railway rejects the Dockerfile VOLUME instruction ("docker VOLUME ...
# is not supported, use Railway Volumes"), so no VOLUME line here. Supply
# YOUTUBE_OAUTH_REFRESH_TOKEN as a Railway secret variable so it survives
# redeploys without being committed to application.yml.

ENTRYPOINT ["sh", "/opt/Lavalink/start-services.sh"]
