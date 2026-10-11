# Packaging, not isolation: compose runs this as the host uid with network_mode: host and
# $HERMES_HOME mounted read-only (see README). stdlib only, so no pip step.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --chmod=755 oko-tail.py /app/oko-tail.py
COPY --chmod=644 web/* /app/web/
ENTRYPOINT ["python3", "/app/oko-tail.py"]
CMD ["--serve", "9120"]
