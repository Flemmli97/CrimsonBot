FROM python:3.12-slim

WORKDIR /bot
COPY . /bot

RUN pip3 install -r requirements.txt
CMD [ "python3", "-u", "./run.py" ]

