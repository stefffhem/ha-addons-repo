ARG BUILD_FROM
FROM $BUILD_FROM

# Python + pyserial (über Alpine-Paket, spart Kompilierzeit) installieren
RUN apk add --no-cache \
        python3 \
        py3-pip \
        py3-serial \
    && pip3 install --no-cache-dir --break-system-packages \
        paho-mqtt>=1.6,<2.0

COPY run.sh /run.sh
COPY qalcosonic_reader.py /qalcosonic_reader.py

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
