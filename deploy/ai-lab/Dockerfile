FROM --platform=linux/amd64 python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579 AS runtime

ENV PYTHONUNBUFFERED=1 \
    MILOCO_HOME=/var/lib/miloco \
    MILOCO_DIRECTORIES__MODELS=/opt/miloco/models \
    MILOCO_SERVER__HOST=0.0.0.0 \
    MILOCO_SERVER__PORT=1810 \
    MILOCO_SERVER__URL=http://127.0.0.1:1810

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 miloco \
    && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/miloco --shell /usr/sbin/nologin miloco \
    && mkdir -p /opt/miloco/models /var/lib/miloco \
    && chown -R 10001:10001 /opt/miloco /var/lib/miloco \
    && chmod 700 /var/lib/miloco

WORKDIR /opt/miloco

COPY requirements/backend.txt requirements/cli.txt /tmp/requirements/
RUN python -m pip install --no-cache-dir --require-hashes \
        -r /tmp/requirements/backend.txt \
        -r /tmp/requirements/cli.txt

COPY wheels/miloco_miot-*-manylinux_2_28_x86_64.whl wheels/
COPY wheels/miloco-*.whl wheels/
COPY wheels/miloco_cli-*.whl wheels/
RUN python -m pip install --no-cache-dir --no-deps wheels/miloco_miot-*-manylinux_2_28_x86_64.whl \
    && python -m pip install --no-cache-dir --no-deps wheels/miloco-*.whl \
    && python -m pip install --no-cache-dir --no-deps wheels/miloco_cli-*.whl \
    && rm -rf wheels /tmp/requirements

COPY models/miloco-models-*.tar.gz /tmp/miloco-models.tar.gz
RUN tar -xzf /tmp/miloco-models.tar.gz -C /opt/miloco/models \
    && rm /tmp/miloco-models.tar.gz \
    && chown -R 10001:10001 /opt/miloco/models

COPY container-entrypoint.sh /usr/local/bin/container-entrypoint.sh
RUN chmod 0555 /usr/local/bin/container-entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1810/health', timeout=3).close()"

USER 10001:10001
ENTRYPOINT ["container-entrypoint.sh"]

FROM runtime AS acceptance

USER root
COPY requirements/acceptance.txt /tmp/requirements/acceptance.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements/acceptance.txt \
    && rm -rf /tmp/requirements

COPY acceptance/ /acceptance/
RUN chown -R 10001:10001 /acceptance

ENV PYTHONPATH=/acceptance/integration \
    MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-acceptance-config \
    MILOCO_SMOKE_PYTHON=/usr/local/bin/python \
    MILOCO_MODEL__OMNI__API_KEY= \
    MILOCO_RESPONSES_API_KEY=

USER 10001:10001
WORKDIR /acceptance
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q", "/acceptance/integration/test_rtsp_perception.py", "/acceptance/integration/test_rtsp_live_view.py::test_fixture_perception_and_uvicorn_live_view_share_one_rtsp_session", "/acceptance/integration/test_responses_perception.py", "-k", "not smoke"]
