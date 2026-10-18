FROM scratch
COPY cloudflared /usr/local/bin/cloudflared
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/cloudflared"]
