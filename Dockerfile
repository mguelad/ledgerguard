FROM python:3.13.15-slim-trixie@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY requirements.lock ./
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --require-hashes -r requirements.lock && /opt/venv/bin/pip uninstall --yes pip
COPY . .
RUN STATIC_MANIFEST=1 LEDGERGUARD_ENV=development /opt/venv/bin/python manage.py collectstatic --noinput
RUN python scripts/assemble_runtime.py --output /runtime && mkdir -p /build/var/reports
FROM gcr.io/distroless/base-debian13:nonroot@sha256:d199d20fb09c898d8822ae5cbd5cf3c6d424e9b5e1fc2eb9a719a7752cd9d861
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=build /runtime/ /
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=10001:10001 /build /app
USER 10001:10001
ENTRYPOINT []
EXPOSE 8000
CMD ["gunicorn", "apps.control_plane.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--worker-tmp-dir", "/tmp"]
