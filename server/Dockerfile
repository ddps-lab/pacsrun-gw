# The gateway server's image.
#
# BUILD IT IN CI, NEVER LOCALLY. The tag is the git SHA, so an image built from
# uncommitted code carries a tag that matches no commit and nobody can reproduce
# what the cluster is running. `.github/workflows/ci.yml` builds it on every push.
#
# Two stages so the runtime image carries no compiler and no build cache:
#   1. `builder` installs the package and its dependencies into /install.
#   2. the final stage copies /install onto a clean slim base.
FROM python:3.12-slim AS builder

WORKDIR /src
COPY pyproject.toml ./
COPY ddpsrun_server ./ddpsrun_server
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

# A non-root user. The server reads a mounted Secret and talks HTTPS; it needs
# nothing a root process would have.
RUN useradd --create-home --uid 10001 ddpsrun
COPY --from=builder /install /usr/local
USER ddpsrun
WORKDIR /home/ddpsrun

# uvicorn on 8080. TLS is terminated in front of this (an Ingress or a load
# balancer), so the server itself speaks plain HTTP inside the cluster.
EXPOSE 8080
CMD ["uvicorn", "ddpsrun_server.main:app", "--host", "0.0.0.0", "--port", "8080"]
