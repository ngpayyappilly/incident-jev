PYTHON ?= python
PIP := $(PYTHON) -m pip
PORT ?= 8080
HOST ?= 127.0.0.1
IMAGE ?= incident-ai-service

.PHONY: help install compile test check run docker-build docker-run clean

help:
	@echo "Available targets:"
	@echo "  install       Install Python dependencies"
	@echo "  compile       Compile application and test sources"
	@echo "  test          Run the unittest suite"
	@echo "  check         Run compile and test checks"
	@echo "  run           Start the FastAPI service locally"
	@echo "  docker-build  Build the Docker image"
	@echo "  docker-run    Run the Docker image on the configured port"
	@echo "  clean         Remove Python cache files"

install:
	$(PIP) install -r requirements.txt

compile:
	$(PYTHON) -m compileall -q app tests

test:
	$(PYTHON) -m unittest discover -s tests -t .

check: compile test

run:
	$(PYTHON) -m uvicorn app.main:app --host $(HOST) --port $(PORT)

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p $(PORT):8080 --env-file .env $(IMAGE)

clean:
	$(PYTHON) -c "import pathlib; [p.unlink() for p in pathlib.Path('.').rglob('*.pyc')]; [p.rmdir() for p in pathlib.Path('.').rglob('__pycache__') if p.is_dir()]"