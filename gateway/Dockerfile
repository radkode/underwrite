ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1

COPY gateway/artifacts.py /opt/underwrite/artifacts.py
COPY gateway/sandbox_runner.py /opt/underwrite/sandbox_runner.py
RUN chmod 0444 /opt/underwrite/artifacts.py /opt/underwrite/sandbox_runner.py

ENTRYPOINT ["/usr/local/bin/python3", "-I", "-S", "/opt/underwrite/sandbox_runner.py"]
