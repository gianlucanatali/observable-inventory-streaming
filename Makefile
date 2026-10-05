# Thin wrappers around docker compose for the demo. Run from overlay/ :  make <target>
# Nothing here provisions cloud resources (no terraform). `up-*` start containers: the human runs them.
#
#   MODE=dev             local rehearsal in the Lima VM `dd-demo`, local Kafka + Schema Registry (compose.dev.yaml)
#   MODE=cloud           the same stack on EC2 through `docker --context $(CTX)`, Confluent Cloud (compose.cloud.yaml)
#                       TOPOLOGY=hybrid (default for cloud) adds compose.hybrid.yaml: only the on-prem VM lane runs here.
#
# Env files (all untracked except compose/dev.env): see compose/compose.yaml header.

# STACK names the stack; DD_ENV=dd-demo-$(STACK), compose project dd-demo-$(STACK), Terraform workspace $(STACK).
# Default dev locally; in cloud mode it must be given explicitly (make MODE=cloud STACK=<name> ...).
MODE ?= dev
ifeq ($(MODE),cloud)
ifeq ($(origin STACK),undefined)
$(error STACK is required in MODE=cloud: make MODE=cloud STACK=<name> <target>)
endif
endif
STACK ?= dev
export STACK
TOPOLOGY ?= $(if $(filter cloud,$(MODE)),hybrid,vm)
export TOPOLOGY
# Docker context of the stack's EC2 host; stack.sh creates it (one per stack, like everything else).
CTX  ?= dd-demo-$(STACK)
LIMA ?= dd-demo
COMPOSE_DIR := $(CURDIR)/compose
# The jr build context can be supplied in this root or by the enclosing checkout.
JR_CONTEXT ?= $(if $(wildcard $(CURDIR)/vendor/jr),../vendor/jr,../../vendor/jr)
JR_DOCKERFILE ?= $(if $(wildcard $(CURDIR)/vendor/jr),../../jr/Dockerfile,../../overlay/jr/Dockerfile)
export JR_CONTEXT JR_DOCKERFILE
# Environment files are portable: explicit ENV_DIR wins, then this repository,
# then an optional parent directory; otherwise this repository is the creation root.
ENV_DIR ?=
ifeq ($(strip $(ENV_DIR)),)
ifneq ($(wildcard $(CURDIR)/.env),)
ENV_DIR := $(CURDIR)
else ifneq ($(wildcard $(CURDIR)/../.env),)
ENV_DIR := $(abspath $(CURDIR)/..)
else
ENV_DIR := $(CURDIR)
endif
endif
export ENV_DIR
# Cloud calibration follows hardware, never STACK. Validate before including any
# file: unknown/untracked sizes fail before credentials or provisioning are touched.
TF_VAR_instance_type ?= t4g.xlarge
FARGATE_SIZE ?= 2048-4096
CALIBRATION_F :=
ifeq ($(MODE),cloud)
CALIBRATION_F := $(shell python3 $(COMPOSE_DIR)/scripts/calibration.py --instance-type '$(TF_VAR_instance_type)' --fargate-size '$(FARGATE_SIZE)')
ifeq ($(strip $(CALIBRATION_F)),)
$(error calibration selection failed; see diagnostic above)
endif
include $(CALIBRATION_F)
export TF_VAR_instance_type FARGATE_SIZE TF_VAR_service_sizing
endif

ifeq ($(MODE),dev)
DOCKER    := limactl shell $(LIMA) -- env STACK=$(STACK) docker
COMPOSE_F := -f $(COMPOSE_DIR)/compose.yaml -f $(COMPOSE_DIR)/compose.dev.yaml
ENV_F     := --env-file $(ENV_DIR)/.env --env-file $(ENV_DIR)/.env.secrets --env-file $(COMPOSE_DIR)/dev.env
else ifeq ($(MODE),cloud)
# compose/scripts/ssh-mux/ssh first in PATH: one multiplexed SSH connection for all docker calls (see the script).
DOCKER    := env STACK=$(STACK) PATH=$(COMPOSE_DIR)/scripts/ssh-mux:$(PATH) docker --context $(CTX)
COMPOSE_F := -f $(COMPOSE_DIR)/compose.yaml -f $(COMPOSE_DIR)/compose.cloud.yaml $(if $(filter hybrid,$(TOPOLOGY)),-f $(COMPOSE_DIR)/compose.hybrid.yaml,)
# .env.cloud-<stack>: written by stack.sh from the Terraform outputs of the stack (cloud + datadog), mode 600.
ENV_F     := --env-file $(ENV_DIR)/.env --env-file $(ENV_DIR)/.env.secrets --env-file $(ENV_DIR)/.env.cloud-$(STACK)
else
$(error MODE must be dev or cloud, got '$(MODE)')
endif

# Allow overriding both (e.g. DOCKER_ENV_FILES for a rehearsal with dummy files outside the repo).
ENV_F := $(if $(ENV_FILES),$(foreach f,$(ENV_FILES),--env-file $(f)),$(ENV_F))
# layer.sh keeps layer-dependent compose variables (RESTOCK_TOPIC, OFFERS_ENABLED) in .state/layers-<stack>.env; always passed last, created empty here.
STATE_ENV := $(CURDIR)/.state/layers-$(STACK).env
$(shell mkdir -p $(CURDIR)/.state && { [ -f $(CURDIR)/.state/.gitignore ] || printf '*\n' > $(CURDIR)/.state/.gitignore; } && touch $(STATE_ENV))
ENV_F += --env-file $(STATE_ENV)
ENV_F += $(foreach f,$(CALIBRATION_F),--env-file $(f))
DC := $(DOCKER) compose -p dd-demo-$(STACK) $(COMPOSE_F) $(ENV_F)
# Compose configuration rendering is daemonless.  Use the host CLI in local
# mode so `make MODE=dev config` also works while the Lima VM is stopped.
CONFIG_DOCKER := $(if $(filter dev,$(MODE)),env STACK=$(STACK) XDG_RUNTIME_DIR=$(or $(XDG_RUNTIME_DIR),/tmp) docker,$(DOCKER))
CONFIG_DC := $(CONFIG_DOCKER) compose -p dd-demo-$(STACK) $(COMPOSE_F) $(ENV_F)
ROUTING := $(CURDIR)/nginx/apply-routing.sh
ROUTING_ENV :=
ifeq ($(TOPOLOGY),hybrid)
ROUTING := $(CURDIR)/compose/scripts/alb-routing.sh
ROUTING_ENV := $(ENV_DIR)/.env.cloud-$(STACK)
endif
# Hybrid routing sources the generated Terraform contract only for the routing command.
ROUTE := $(if $(ROUTING_ENV),set -a; . "$(ROUTING_ENV)"; set +a; )$(ROUTING)
export DC MODE CONFIRM ROUTING ROUTING_ENV
export OVERLAY := $(CURDIR)
# Hybrid builds the cloud-online images for ECR, but `_up` never activates that profile on the VM.
ALL_PROFILES := --profile releases --profile restock --profile offers --profile tools --profile jr $(if $(filter hybrid,$(TOPOLOGY)),--profile control-center --profile cloud-online,)
LAYER := $(COMPOSE_DIR)/scripts/layer.sh

JR_SERVICES := jr-sales-s01 jr-sales-s02 jr-sales-s03 jr-sales-s04 jr-sales-s05
CONNECTORS := inventory-s01 inventory-s02 inventory-s03 inventory-s04 inventory-s05 sellable-redis
SECONDS ?=
SCEN := $(DC) --profile tools run --rm -T scenario
SMOKE_ARGS ?=
PRODUCT ?= P0042
GAP ?= 1.5
LOAD_DURATION ?= 120
LOAD_RPS ?= 5
LOAD_ARGS ?= --duration $(LOAD_DURATION) --rps $(LOAD_RPS)
# Every `make load` writes its summary to /out/last.json (volume scenario-out); `make canary-check` gates on it.
CHECK_ARGS ?= --release-a 1.1.0 --release-b 1.2.0 --verify-file /out/verify.json

.DEFAULT_GOAL := help
.PHONY: help secrets build config config-all up-dev down-dev up-cloud down-cloud register-connector seed reset \
        sell-out verify load route-baseline incident canary-10 canary-50 canary-100 rollback route-check route-show status \
        sales-on sales-off canary-check offers-on offers-off layer-on layer-off layers-status lead-time control \
        store-pause store-resume smoke links stack-preflight stack-up stack-down stack-status account-up account-down

help:
	 @echo "targets: secrets build config config-all up-dev down-dev up-cloud down-cloud register-connector seed reset"
	 @echo "         sell-out verify load route-baseline incident canary-10 canary-50 canary-100 rollback route-check route-show status sales-on sales-off canary-check offers-on offers-off"
	 @echo "         layer-on L=<releases|restock|offers|control-center|dd-synthetics|dd-streams|dd-rum>  layer-off L=...  layers-status  lead-time SECONDS=<n>  control"
	 @echo "         store-pause|store-resume STORE=S03"
	 @echo "         smoke [SMOKE_ARGS=--offers]   (browser + API + Connect + control panel; exit 1 on any FAIL)"

	 @echo "         links   (MODE=cloud STACK=<s>; print state-derived Datadog dashboard, APM and DSM URLs)"
	 @echo "  cloud:  stack-preflight | stack-up [LAYERS=all|core|<comma list>] | stack-status | stack-down   (MODE=cloud STACK=<s>; up/down need CONFIRM=yes)"
	 @echo "  account-down: MODE=cloud STACK=account CONFIRM=yes ACCOUNT_DOWN_DESTROY=yes (permanently deletes account CCM resources after plan review)"
	 @echo "MODE=$(MODE) STACK=$(STACK)  docker='$(DOCKER)'"

secrets:
	 @echo "== create $(ENV_DIR)/.env.secrets with random passwords if absent (never printed)"
	 @$(COMPOSE_DIR)/scripts/gen-secrets.sh

build:
	 @echo "== build all images with '$(DOCKER)' (MODE=$(MODE)); cloud mode builds on the EC2 host through the SSH context"
	 $(DC) $(ALL_PROFILES) build

config:
	 @echo "== validate the $(MODE) compose files (no containers started; output suppressed because it contains secrets)"
	 $(CONFIG_DC) $(ALL_PROFILES) config --quiet

config-all:
	 @echo "== validate dev and cloud variants"
	 $(MAKE) --no-print-directory MODE=dev config
	 $(MAKE) --no-print-directory MODE=cloud config

up-dev:
	 @echo "WARNING: this STARTS containers in the local VM '$(LIMA)' (local only, no cloud cost). Run it yourself."
	 $(MAKE) --no-print-directory MODE=dev _up
down-dev:
	 @echo "WARNING: this STOPS and removes the dev containers. Volumes are kept; add PURGE=1 to delete data too."
	 $(MAKE) --no-print-directory MODE=dev _down

up-cloud:
	 @echo "WARNING: this STARTS containers on the EC2 host (context $(CTX)). The host and Confluent Cloud are billed hourly:"
	 @echo "         give the cost note and get approval first. Run it yourself."
	 $(MAKE) --no-print-directory MODE=cloud _up
down-cloud:
	 @echo "WARNING: this STOPS and removes the cloud-host containers (the EC2 instance and Confluent keep billing until terraform destroy)."
	 $(MAKE) --no-print-directory MODE=cloud _down

_up:
	$(DC) up -d
	@if [ "$(TOPOLOGY)" != hybrid ]; then \
	  echo "== recreate all inventory-api releases so the shared $(IMAGE_TAG) image tag adopts the latest build"; \
	  $(DC) up -d --force-recreate --no-deps inventory-api-100 inventory-api-110 inventory-api-120; \
	else echo "== hybrid online releases run on ECS; no release containers started on the VM"; fi
	@echo "== publish running layers and routing to redis for the control panel"
	 $(LAYER) sync
_down:
	 $(DC) $(ALL_PROFILES) down $(if $(PURGE),-v,)

sales-on:
	 @echo "== start the five jr background sales (250 ms ticks; the rate comes from the control panel sales_per_min_per_store; never touches P0042)"
	 $(DC) --profile jr up -d $(JR_SERVICES)
sales-off:
	 @echo "== stop the jr background sales"
	 $(DC) --profile jr stop $(JR_SERVICES)

offers-on:
	 @test "$(TOPOLOGY)" != hybrid || { echo "offers run on ECS: use make layer-on L=offers" >&2; exit 2; }
	 @echo "== start the optional offer-worker (profile offers; JEV_API_KEY empty = rule default only)"
	 $(DC) --profile offers up -d offer-worker
offers-off:
	 @test "$(TOPOLOGY)" != hybrid || { echo "offers run on ECS: use make layer-off L=offers" >&2; exit 2; }
	 @echo "== stop the offer-worker"
	 $(DC) --profile offers stop offer-worker

register-connector:
	 @echo "== render and PUT inventory-s01..s05 (Debezium), sellable-redis and, when the restock layer runs (or LAYERS=restock), restock-procurement and procurement-orders; waits for RUNNING"
	 LAYERS="$${LAYERS:-$$($(LAYER) running)}" $(CURDIR)/connect/register-connector.sh

seed:
	 @echo "== seed the source (S01..S05 x P0001..P0200, seed 42)"
	 $(SCEN) seed
reset:
	 @echo "== reset: background sales off (they would keep changing stock), healthy 1.0.0 routing, then the demo data to the seeded state; waits for the serving view"
	 $(DC) --profile jr stop $(JR_SERVICES)
	 $(ROUTE) 100 0 0
	 $(SCEN) reset
	 @echo "== background sales are OFF now: make sales-on to restart them"
reset-data:
	 @echo "== reset demo data without changing the already-selected release route"
	 $(DC) --profile jr stop $(JR_SERVICES)
	 $(SCEN) reset
	 @echo "== background sales are OFF now; routing was not changed"
sell-out:
	 @echo "== sell out $(PRODUCT): each store's whole quantity in its own source, $(GAP) s apart"
	 $(SCEN) sell-out --product $(PRODUCT) --gap-s $(GAP)
verify:
	 @echo "== compare source and Redis for all seeded keys"
	 $(SCEN) verify --json-out /out/verify.json
load:
	 @echo "== lookup load through nginx: scenario load $(LOAD_ARGS)   (override with LOAD_ARGS='--duration 300 --rps 40 --output /tmp/x.json')"
	 $(SCEN) load --output /out/last.json $(LOAD_ARGS)
smoke:
	 @echo "== smoke test: Connect, availability API, storefront in a browser, control panel   (SMOKE_ARGS=--offers adds a cart ADD/ABANDON)"
	 $(DC) --profile tools run --rm -T smoke $(SMOKE_ARGS)

# Read-only presenter links. This never loads env files or prints secret outputs.
links:
	 @test "$(MODE)" = cloud || { echo "links: run with MODE=cloud STACK=<name>" >&2; exit 2; }
	 $(CURDIR)/compose/scripts/links.sh
canary-check:
	 @echo "== gate the last load summary: scenario canary-check /out/last.json $(CHECK_ARGS)"
	 $(SCEN) canary-check /out/last.json $(CHECK_ARGS)

# Routing: weights for inventory-api 1.0.0 / 1.1.0 / 1.2.0 (exact split, see nginx/render-routing.sh)
route-baseline:
	 @echo "== route 100/0/0 (healthy 1.0.0)"
	 $(ROUTE) 100 0 0
incident:
	 @echo "== route 0/100/0 (regressed 1.1.0 takes everything: the Incident)"
	 $(ROUTE) 0 100 0
canary-10:
	 @echo "== route 0/90/10 (canary: 10% to fixed 1.2.0)"
	 $(ROUTE) 0 90 10
canary-50:
	 @echo "== route 0/50/50"
	 $(ROUTE) 0 50 50
canary-100:
	 @echo "== route 0/0/100 (all traffic on 1.2.0)"
	 $(ROUTE) 0 0 100
rollback:
	 @echo "== restore the previous routing from .state/routing-$(STACK)"
	 $(ROUTE) --rollback
route-check:
	 @test "$(MODE)" = cloud && test "$(TOPOLOGY)" = hybrid || { echo "route-check: run with MODE=cloud TOPOLOGY=hybrid STACK=<name>" >&2; exit 2; }
	 @$(ROUTE) --sync
route-show:
	 @$(ROUTE) --sync
	 @$(ROUTE) --show

status:
	 @echo "== containers"
	 $(DC) ps
	 @echo "== routing (1.0.0 1.1.0 1.2.0)"
	 @$(ROUTE) --show
	 @echo "== layers"
	 @$(LAYER) status
	 @echo "== connectors ($(CONNECTORS), plus restock-procurement and procurement-orders when the restock layer runs)"
	 @c="$(CONNECTORS)"; case ",$$($(LAYER) running)," in *,restock,*) c="$$c restock-procurement procurement-orders";; esac; \
	 $(DC) exec -T connect python3 -c "import urllib.request; [print(n, urllib.request.urlopen('http://localhost:8083/connectors/'+n+'/status', timeout=5).read().decode()) for n in '$$c'.split()]"

# --- layers: compose profiles, Terraform toggles in cloud mode ---
layer-on:
	 @test -n "$(L)" || { echo "usage: make layer-on L=<releases|restock|offers|dd-synthetics|dd-streams|dd-rum> [STACK=<s>] [MODE=cloud CONFIRM=yes]" >&2; exit 2; }
	 @if [ "$(MODE)" = cloud ]; then echo "WARNING: MODE=cloud: layer '$(L)' runs terraform apply and COSTS MONEY (Confluent/AWS bill hourly). Needs a cost note and explicit approval; add CONFIRM=yes to proceed."; fi
	 $(LAYER) on $(L)
layer-off:
	 @test -n "$(L)" || { echo "usage: make layer-off L=<layer> [STACK=<s>] [MODE=cloud CONFIRM=yes]" >&2; exit 2; }
	 @if [ "$(MODE)" = cloud ]; then echo "WARNING: MODE=cloud: layer '$(L)' off runs terraform apply (enable_*=false). Needs explicit approval; add CONFIRM=yes to proceed."; fi
	 $(LAYER) off $(L)
layers-status:
	 @$(LAYER) status

lead-time:
	 @test -n "$(SECONDS)" || { echo "usage: make lead-time SECONDS=<n>   (restock lead time; default 172800 = 48 h)" >&2; exit 2; }
	 @echo "== procurement lead time -> $(SECONDS) s (applies to every open purchase order on supplier-sim's next cycle)"
	 $(SCEN) lead-time --seconds $(SECONDS)

control:
	 @echo "== control panel (basic auth)"
	 @if [ "$(TOPOLOGY)" = hybrid ]; then echo "URL:      $$(terraform -chdir=$(CURDIR)/terraform/aws output -raw alb_url)/control/  (ALB; the security group admits only the presenter)"; \
	  elif [ "$(MODE)" = cloud ]; then echo "URL:      $$(terraform -chdir=$(CURDIR)/terraform/vm output -raw ingress_base_url)/control/  (the security group admits only the presenter)"; \
	  else echo "URL:      http://localhost:$$(sed -n 's/^INGRESS_PORT=//p' $(COMPOSE_DIR)/dev.env)/control/"; fi
	 @echo "user:     demo"
	 @echo "password: CONTROL_PASSWORD in $(ENV_DIR)/.env.secrets (not printed here)"

# Act 2 (unknown is not zero): stop one store's change stream, then resume. STORE=S01..S05
STORE ?= S03
store-pause store-resume:
	 @echo "== $(subst store-,,$@) the Debezium connector of store $(STORE) (Connect REST)"
	 $(DC) exec -T connect python3 -c "import urllib.request,sys; n='inventory-'+'$(STORE)'.lower(); a='$(subst store-,,$@)'; r=urllib.request.urlopen(urllib.request.Request('http://localhost:8083/connectors/'+n+'/'+a, method='PUT'), timeout=10); print(n, a, r.status)"

# --- cloud stack lifecycle: compose/scripts/stack.sh. Terraform plans are shown and confirmed step by step. ---
STACK_SH := $(COMPOSE_DIR)/scripts/stack.sh
.PHONY: calibration-check
calibration-check:
	@printf '%s\n' $(CALIBRATION_F)
stack-preflight:
	 @test "$(MODE)" = cloud || { echo "stack-preflight: run with MODE=cloud STACK=<name>" >&2; exit 2; }
	 $(STACK_SH) preflight
stack-up:
	 @test "$(MODE)" = cloud || { echo "stack-up: run with MODE=cloud STACK=<name>" >&2; exit 2; }
	 @echo "WARNING: creates billed resources (Confluent Cloud, EC2) for stack $(STACK). Cost note + explicit approval first."
	 $(STACK_SH) up
stack-down:
	 @test "$(MODE)" = cloud || { echo "stack-down: run with MODE=cloud STACK=<name>" >&2; exit 2; }
	 $(STACK_SH) down
# Account-wide Datadog Cloud Cost Management for AWS: applied once, kept across stacks. STACK=account.
account-up:
	 @test "$(MODE)" = cloud -a "$(STACK)" = account || { echo "account-up: run with MODE=cloud STACK=account" >&2; exit 2; }
	 @echo "WARNING: creates AWS resources (S3 bucket, CUR export, IAM role for Datadog). Cost note + explicit approval first."
	 $(STACK_SH) account-up
account-down:
	 @test "$(MODE)" = cloud -a "$(STACK)" = account || { echo "account-down: run with MODE=cloud STACK=account" >&2; exit 2; }
	 $(STACK_SH) account-down
stack-status:
	 @test "$(MODE)" = cloud || { echo "stack-status: run with MODE=cloud STACK=<name>" >&2; exit 2; }
	 $(STACK_SH) status
