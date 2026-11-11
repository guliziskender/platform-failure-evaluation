# Builds a small service image. config/app.yaml exists next to this file, but .dockerignore
# excludes config/, so it is not in the build context.
FROM busybox:1.36
WORKDIR /app
COPY app.sh /app/app.sh
COPY config/app.yaml /app/config/app.yaml
CMD ["sh", "/app/app.sh"]
