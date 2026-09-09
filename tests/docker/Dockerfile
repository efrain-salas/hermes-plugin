FROM nousresearch/hermes-agent@sha256:2b11efef5e54c8099f1d5410f846672eb222232735ff7a8656a5eaa4dc20bd45

USER root
COPY . /tmp/hermes-mobile
RUN uv pip install --python /opt/hermes/.venv/bin/python '/tmp/hermes-mobile[documents,test]' && \
    mkdir -p /opt/test && \
    cp /tmp/hermes-mobile/tests/docker/fake_services.py /opt/test/fake_services.py && \
    cp /tmp/hermes-mobile/tests/docker/bootstrap.sh /opt/test/bootstrap.sh && \
    cp /tmp/hermes-mobile/tests/docker/start-hermes.sh /opt/test/start-hermes.sh && \
    chmod 0755 /opt/test/bootstrap.sh /opt/test/start-hermes.sh && \
    rm -rf /tmp/hermes-mobile
