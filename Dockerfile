FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY requirements.lock ./
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --require-hashes -r requirements.lock
COPY . .
RUN STATIC_MANIFEST=1 LEDGERGUARD_ENV=development /opt/venv/bin/python manage.py collectstatic --noinput
FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 ledgerguard && useradd --uid 10001 --gid 10001 --no-create-home ledgerguard
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=10001:10001 /build /app
RUN mkdir -p /app/var/reports && chown -R 10001:10001 /app/var
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "apps.control_plane.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--worker-tmp-dir", "/tmp"]
