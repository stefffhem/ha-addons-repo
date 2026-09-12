ARG BUILD_FROM
FROM $BUILD_FROM

# Python installieren; pyserial + paho-mqtt kommen über pip (py3-serial existiert
# nicht in allen Alpine-Repo-Versionen als Paket)
RUN apk add --no-cache \
        python3 \
        py3-pip \
    && pip3 install --no-cache-dir --break-system-packages \
        pyserial \
        "paho-mqtt>=1.6,<2.0"

COPY run.sh /run.sh
COPY qalcosonic_reader.py /qalcosonic_reader.py

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
