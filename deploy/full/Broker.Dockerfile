# Only this trusted service receives Docker control. Compute receives none.
ARG DOCKER_CLI_IMAGE=docker.io/library/docker:27-cli
ARG RUNTIME_IMAGE
FROM ${DOCKER_CLI_IMAGE} AS docker-cli
FROM ${RUNTIME_IMAGE}
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
RUN docker --version
HEALTHCHECK NONE
USER 0:10001
ENTRYPOINT ["/app/.venv/bin/python", "-m", "app.multitenancy.compute_broker"]
