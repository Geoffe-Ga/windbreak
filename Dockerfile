# windbreak container image (issue #15). Minimal, non-root, runs the CLI.
FROM python:3.12-slim

# Do not buffer stdout/stderr so the JSON log stream is emitted promptly.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install the project (and its console script) from source.
COPY . .
RUN pip install --no-cache-dir .

# Drop root: create an unprivileged user and switch to it after install so a
# compromised process cannot escalate. SPEC-aligned defense in depth.
RUN useradd --create-home --uid 10001 windbreak
USER windbreak

# Default to the pipeline process; compose/systemd override --process per unit.
CMD ["windbreak", "run"]
