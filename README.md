# iam-passport

公司统一身份平台（IAM）的部署配置和工具。员工用一个账号登录公司所有系统，入职自动开通，离职自动禁用。

- **身份中心**：[authentik](https://goauthentik.io/)，提供单点登录（OIDC、SAML）、LDAP、RADIUS 和代理认证。
- **人员来源**：飞书通讯录。员工用飞书扫码登录，同步脚本把组织架构同步到 authentik。

```mermaid
---
config:
  theme: base
  themeVariables:
    primaryColor: "#f4f4f5"
    primaryBorderColor: "#d4d4d8"
    primaryTextColor: "#27272a"
    lineColor: "#a1a1aa"
    edgeLabelBackground: "#ffffff"
    fontSize: 14px
  flowchart:
    curve: basis
    padding: 16
---
flowchart LR
    FS("飞书") -->|扫码登录、通讯录同步| AK("authentik")
    AK -->|OIDC| WEB("<div style='width:14em'>GitLab、Jenkins、Grafana 等</div>")
    AK -->|代理认证| MON("<div style='width:14em'>Prometheus、Alertmanager</div>")
    AK -->|LDAP| JS("<div style='width:14em'>JumpServer</div>")
    AK -->|RADIUS| VPN("<div style='width:14em'>VPN、网络设备</div>")
```

## 目录结构

```text
iam-passport/
├── Makefile            # make test / lint / chart / mirror / image-feishu-sync
├── deployments/        # 生产部署：Helm chart、Jenkinsfile、集群初始化、Dockerfile、authentik 版本号
├── authentik/          # 本地验证用的 compose 文件和 .env 模板
├── outposts/           # 本地验证用的 outpost 部署文件
│   └── ldap/           # LDAP outpost（给 JumpServer 等只支持 LDAP 的系统用）
├── org-sync/           # 组织架构同步，每个来源一个子目录
│   └── feishu/         # 飞书通讯录 -> authentik
└── docs/
    ├── authentik.md    # authentik 生产部署与运维指南
    ├── deploy-acs.md   # 在阿里云 Kubernetes 上部署
    └── lark.md         # 飞书扫码登录和通讯录同步
```

## 本地快速体验

需要 Docker 和 Docker Compose。

```bash
cp authentik/.env.example authentik/.env
sed -i '' "s|^PG_PASS=.*|PG_PASS=$(openssl rand -base64 36 | tr -d '\n/+=')|" authentik/.env
sed -i '' "s|^AUTHENTIK_SECRET_KEY=.*|AUTHENTIK_SECRET_KEY=$(openssl rand -base64 60 | tr -d '\n/+=')|" authentik/.env
make up
```

`sed -i ''` 是 macOS 的写法，Linux 上去掉 `''`。authentik 的版本只在 `deployments/chart/Chart.yaml` 里写一处，`make up` 会读出来写到 `authentik/.env`，本地和生产始终是同一个版本。

启动约需 1 分钟，然后打开 <http://localhost:9000/if/flow/initial-setup/> 设置管理员 `akadmin` 的邮箱和密码（路径末尾的 `/` 不能省）。

常用命令（在 `authentik/` 目录下）：

```bash
docker compose ps                                   # 查看状态
docker compose logs -f server worker                # 查看日志
docker compose exec worker ak create_recovery_key 10 akadmin   # 忘记管理员密码时，生成一次性恢复链接
docker compose down                                 # 停止，数据保留
docker compose down -v && rm -rf data               # 清空，从头再来
```

## 飞书通讯录同步

脚本把飞书通讯录同步到 authentik：入职建号，部门变成用户组，离职或冻结禁用账号。

```bash
cd org-sync/feishu
python3 -m venv .venv && . .venv/bin/activate && pip install requests
export FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx
export AK_URL=http://localhost:9000 AK_TOKEN=xxx
python3 feishu_authentik_sync.py --dry-run   # 只打印将要做的变更，不写入
```

运行测试（使用模拟的飞书和 authentik 接口，不需要网络和依赖）：

```bash
make test
```

飞书应用的创建、扫码登录配置、authentik 服务账号和 token 的创建方法，见 [docs/lark.md](docs/lark.md)。

## 生产部署

生产环境用 authentik 官方 Helm chart 部署在阿里云 Kubernetes（ACS 或 ACK）上，使用专用 namespace `iam-passport-prod`，不设单独的测试环境。`deployments/chart/` 把官方 chart、飞书同步和 blueprints 打包成一个 Helm release，由 Jenkins 流水线发布。LDAP 等 outpost 由 authentik 自己在集群里创建和升级。

运维搭建从 [docs/deploy-acs.md](docs/deploy-acs.md) 开始，文档开头有完整的搭建顺序，需要时会链接到另外两篇：

- [docs/deploy-acs.md](docs/deploy-acs.md)：怎么部署。准备工作、首次部署、发布、升级、回滚、监控和应急发布。
- [docs/authentik.md](docs/authentik.md)：怎么用好、管好。安全加固、权限设计、系统接入、高可用与应急、审计合规和上线检查清单。
- [docs/lark.md](docs/lark.md)：飞书扫码登录和通讯录同步的配置、风险和上线检查清单。

## 当前状态

- authentik 2026.8.5 已在本地 Docker 上验证可以启动和登录。
- 飞书扫码登录和同步脚本还没有接真实飞书租户测试，同步脚本目前只通过了模拟测试。
- 本地验证用的 LDAP outpost 部署文件已编写，配置校验通过，但还没有连接 authentik 实际运行过。
- Helm chart 已通过 `helm lint` 和 Kubernetes 1.31 的 schema 校验，还没有在集群上部署过；集群、数据库、域名、GitLab 地址等还是占位项，上线前要替换的完整清单见 [docs/deploy-acs.md 3.1 节](docs/deploy-acs.md#31-上线前需要替换的占位项)。
- 第一个 blueprint（只允许同步的账号扫码登录）已在本地 authentik 上验证。Grafana 试点配置尚未编写。

## 安全提示

- 不要提交 `.env`、`authentik/data/` 等文件，`.gitignore` 已排除。
- 生产环境不要使用本地生成的密钥，要重新生成并加密保存。
- 恢复链接等同于管理员登录权限，用完即弃，不要发到群里。
