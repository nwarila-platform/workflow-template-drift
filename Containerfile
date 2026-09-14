# workflow-template-drift runtime: package only; tools/ is intentionally excluded.
FROM ghcr.io/nwarila/ubi9-base-python:base-python@sha256:0ee8545f8ccd37a2aeda5c0dde33c0fcc9fe5969092c8b6e8228efc2c5fe826d

COPY --chown=0:0 --chmod=0755 workflow_template_drift/ /usr/lib/python3.12/site-packages/workflow_template_drift/

USER 65532:65532
ENTRYPOINT ["/usr/bin/python3.12", "-I", "-X", "utf8", "-B", "-m", "workflow_template_drift"]
