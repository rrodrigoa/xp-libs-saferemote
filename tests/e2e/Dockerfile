FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS wheel
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY skills/ skills/
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS services
WORKDIR /app
COPY --from=wheel /wheels /wheels
COPY requirements-lock.txt /app/requirements-lock.txt
RUN set -- /wheels/*.whl; python -m pip install --no-cache-dir -c requirements-lock.txt "$1[e2e]"
COPY tests/__init__.py tests/__init__.py
COPY tests/e2e/*.py tests/e2e/
COPY tests/e2e/coverage.ini /app/coverage.ini
COPY examples/__init__.py examples/__init__.py
COPY examples/action_packs/ examples/action_packs/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001

FROM services AS endpoint
COPY tests/e2e/fixtures/ /evidence/
