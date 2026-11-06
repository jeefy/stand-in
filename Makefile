.PHONY: build test run clean docker-build

BINARY_NAME=stand-in

build:
	CGO_ENABLED=0 go build -ldflags="-w -s" -o bin/$(BINARY_NAME) ./cmd/stand-in

test:
	go test -v -race ./...

test-coverage:
	go test -race -coverprofile=coverage.out -covermode=atomic ./...
	go tool cover -func=coverage.out

run:
	go run ./cmd/stand-in

clean:
	rm -rf bin/ coverage.out

docker-build:
	docker build -t ghcr.io/jeefy/stand-in:latest .
