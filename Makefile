# `make lint`, the default goal, is what CI runs before building anything.
# Helm runs in a container (alpine/helm), so only Docker is required.

# The authentik release, read from the chart dependency that pins it.
AUTHENTIK_TAG := $(shell sed -n '/- name: authentik/,/version:/s/^ *version: *//p' deployments/chart/Chart.yaml)

APP      ?= iam-passport
# The public endpoint: `mirror` runs from a machine outside the VPC. ACR replicates to the regions the clusters pull from.
REGISTRY ?= iam-registry.ap-southeast-1.cr.aliyuncs.com/iam
# Guarded, since these expand on every invocation, even outside a checkout.
REVISION ?= $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
TAG      ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
CREATED  ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
# Local builds only check the Dockerfile; the images that deploy are CI's.
FEISHU_SYNC_IMAGE := $(APP)-feishu-sync:local-$(TAG)
HELM_IMAGE ?= alpine/helm:3.19.0
HELM := docker run --rm -v "$(CURDIR)/deployments/chart:/chart" -w /chart $(HELM_IMAGE)

.DEFAULT_GOAL := lint

.PHONY: lint chart mirror image-feishu-sync up down ldap-up

# TODO values render fine; the pipeline rejects them.
lint:
	$(HELM) lint . -f values.yaml -f values-prod.yaml --set feishuSync.image.tag=lint

# Re-vendors the authentik chart after its version in Chart.yaml changes;
# commit charts/ and Chart.lock with it.
chart:
	$(HELM) dependency update

# Copies the upstream images, every architecture included, into ACR. Run once
# per authentik version change, before the pipeline deploys it. The outpost names
# must stay authentik-<type>: AUTHENTIK_OUTPOSTS__CONTAINER_IMAGE_BASE in the
# chart's values builds them that way. helm and python are the pipeline's;
# python is also the feishu-sync base image.
OUTPOSTS := ldap radius
PUBLIC_REGISTRY ?= $(dir $(REGISTRY))public
mirror:
	docker buildx imagetools create -t $(REGISTRY)/authentik-server:$(AUTHENTIK_TAG) ghcr.io/goauthentik/server:$(AUTHENTIK_TAG)
	for t in $(OUTPOSTS); do \
		docker buildx imagetools create -t $(REGISTRY)/authentik-$$t:$(AUTHENTIK_TAG) ghcr.io/goauthentik/$$t:$(AUTHENTIK_TAG) || exit 1; \
	done
	docker buildx imagetools create -t $(PUBLIC_REGISTRY)/helm:$(lastword $(subst :, ,$(HELM_IMAGE))) $(HELM_IMAGE)
	docker buildx imagetools create -t $(PUBLIC_REGISTRY)/python:3.13-alpine python:3.13-alpine
	docker buildx imagetools inspect $(REGISTRY)/authentik-server:$(AUTHENTIK_TAG)

# Local only; CI builds with kaniko, passing the same build args by hand.
# ACS nodes are x86_64, developer machines often arm64.
image-feishu-sync:
	docker build --platform linux/amd64 -f deployments/feishu-sync.Dockerfile \
		--build-arg BASE_IMAGE=python:3.13-alpine \
		--build-arg VERSION=local-$(TAG) \
		--build-arg REVISION=$(REVISION) \
		--build-arg CREATED=$(CREATED) \
		-t $(FEISHU_SYNC_IMAGE) .

# Local compose stacks. AUTHENTIK_TAG is written into their .env, so that plain
# `docker compose` commands in those directories interpolate the same version.
define sync-tag
	@test -f $(1)/.env || { echo "$(1)/.env missing: cp $(1)/.env.example $(1)/.env and fill it in"; exit 1; }
	@grep -v '^AUTHENTIK_TAG=' $(1)/.env > $(1)/.env.tmp; echo 'AUTHENTIK_TAG=$(AUTHENTIK_TAG)' >> $(1)/.env.tmp; cat $(1)/.env.tmp > $(1)/.env; rm $(1)/.env.tmp
endef

up:
	$(call sync-tag,authentik)
	cd authentik && docker compose up -d

down:
	cd authentik && docker compose down

ldap-up:
	$(call sync-tag,outposts/ldap)
	cd outposts/ldap && docker compose up -d
