FROM golang:alpine AS builder

WORKDIR /app

RUN apk add --no-cache git ca-certificates tzdata

COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o /stand-in ./cmd/stand-in

FROM gcr.io/distroless/static:nonroot

WORKDIR /
COPY --from=builder /stand-in /stand-in

USER 65532:65532

EXPOSE 9101 9102

ENTRYPOINT ["/stand-in"]


