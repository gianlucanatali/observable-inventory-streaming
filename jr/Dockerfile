# jr built from the public fork in vendor/jr, branch sql-producer, which adds PostgreSQL, MySQL and SQL outputs.
# Build context = vendor/jr (set in compose). Differs from upstream's Dockerfile on purpose: pinned base images,
# pinned gogen-avro, and no `go get -u` (that upgrades dependencies on every build; go.sum is the pin).
FROM golang:1.26-trixie@sha256:eae2aaa6add2936cbf350dd0d2628b363461542f0c4b3c0b558957e0f2997379 AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download && go install github.com/actgardner/gogen-avro/v10/cmd/...@v10.2.1
COPY . .
RUN go generate pkg/generator/generate.go \
 && CGO_ENABLED=1 GOOS=linux go build -tags static_all \
      -ldflags="-X 'github.com/jrnd-io/jr/pkg/cmd.Version=0.4.0-sql'" -o /out/jr jr.go

FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
RUN useradd --create-home jr
COPY --from=builder /src/templates/ /home/jr/.jr/templates/
COPY --from=builder /src/config/ /home/jr/.jr/
COPY --from=builder /out/jr /usr/local/bin/jr
USER jr
ENV JR_SYSTEM_DIR=/home/jr/.jr
ENTRYPOINT ["jr"]
