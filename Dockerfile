FROM python:3.13.18-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS build
WORKDIR /build
COPY requirements.lock requirements-build.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements-build.lock && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.lock
COPY src ./src
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM python:3.13.18-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BOT_DATABASE=/data/state.sqlite3 ADMIN_SECRET_KEY_FILE=/data/admin-secret.key WAVIOT_SCOPE_FILE=/data/audit-scope.json
RUN groupadd --gid 10001 waviot && useradd --uid 10001 --gid 10001 --no-create-home waviot && mkdir /data && chown 10001:10001 /data && chmod 700 /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels waviot-telegram==0.1.0 && rm -rf /wheels && python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Moscow')"
WORKDIR /app
USER 10001:10001
CMD ["waviot-bot", "run"]

FROM caddy:2.11.7-alpine@sha256:e70b164da81ef698b3ad19af799ea6086ce6d9e48ed4079eb9fe26f6fbf14ee5 AS proxy
RUN setcap -r /usr/bin/caddy && chown -R 10001:10001 /data /config
USER 10001:10001

FROM runtime AS test
USER root
COPY requirements-test.lock /tmp/requirements-test.lock
RUN pip install --no-cache-dir -r /tmp/requirements-test.lock
COPY tests /app/tests
COPY pyproject.toml /app/pyproject.toml
USER 10001:10001
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
