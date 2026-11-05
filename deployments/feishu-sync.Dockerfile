# Packages org-sync/feishu for the feishu-sync CronJob. Built by kaniko in CI
# from the repository root, or locally with `make image-feishu-sync`.
#
# The default base is the ACR mirror (`make mirror`), since the build VPC does
# not pull from Docker Hub. Local builds override it with BASE_IMAGE.
ARG BASE_IMAGE=iam-registry-vpc.ap-southeast-1.cr.aliyuncs.com/public/python:3.13-alpine
FROM ${BASE_IMAGE}

# Traces a container to its commit; unlike a tag, a label travels with the digest.
ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="iam-passport-feishu-sync" \
      org.opencontainers.image.description="Feishu directory to authentik sync" \
      org.opencontainers.image.source="git@TODO-gitlab-host:TODO-group/iam-passport.git" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

WORKDIR /app

# The Aliyun PyPI mirror is reachable from the build VPC; pypi.org may not be.
COPY org-sync/requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY org-sync/common/*.py common/
COPY org-sync/feishu/__init__.py org-sync/feishu/main.py org-sync/feishu/contacts.py feishu/

USER 65534

# Exec form, so python is PID 1 and receives SIGTERM directly. -m puts the
# working directory on sys.path, which is what resolves `common` and `feishu`.
ENTRYPOINT ["python3", "-m", "feishu.main"]
