# 系统接入

本文说明各业务系统怎么接入 authentik：接入顺序、已有账号的迁移、OIDC 的配置约定、自研系统，以及代理认证、LDAP、RADIUS 三种兜底方式。各接入方式怎么选见 [authentik.md 第 1 节](authentik.md#1-总体架构)，权限和组的设计见 [authentik.md 第 5 节](authentik.md#5-权限设计)。

具体系统的接入步骤单独成篇：GitLab 见 [gitlab.md](gitlab.md)，Jenkins 见 [jenkins.md](jenkins.md)。文中的 `auth.company.com` 等都是占位地址。

## 1. 接入顺序

按风险从低到高逐个接入，每接一个就验证一个：

1. Grafana（试点，风险最低）
2. GitLab、Jenkins、Harbor、Argo CD
3. Prometheus、Alertmanager
4. JumpServer
5. VPN 和网络设备

每个系统接入时都要做的事：

- 按第 2 节完成已有账号的比对和迁移方案。
- 在 authentik 里配置绑定，确认只有该用的人能登录。
- 验证角色映射：管理员、普通用户、无权限用户各测一次。
- 关闭系统的本地注册和本地密码登录，只保留一个应急管理员（见 [authentik.md 7.3 节](authentik.md#73-应急通道)）。
- 把接入步骤写到 `docs/` 下，方便后续维护。

## 2. 已有账号的迁移

GitLab、Jenkins 等系统里已经有本地账号。接入单点登录后，如果系统认为新的登录身份和原账号不是同一个人，就会新建一个重复账号，员工原有的项目、权限、历史记录都留在旧账号下。这是推广阶段最容易出事故的环节。

每个系统接入前：

1. 导出系统里现有的全部账号（用户名、邮箱、是否管理员）。
2. 和 authentik 里的账号比对。authentik 的用户名是员工的英文名（重名时带数字，如 `lily2`），邮箱取自飞书，见 [feishu.md 5.4 和 5.5 节](feishu.md#54-脚本的行为)。老账号一般是邮箱前缀，按用户名对不上，优先让系统按邮箱关联。
3. 查清这个系统登录时按什么关联老账号：邮箱、用户名，还是 OIDC 的 `sub`。不同系统的行为不同，有的需要单独开启“自动关联已有账号”的配置。
4. 对不上的账号，在系统里先改成和 authentik 一致的用户名或邮箱，再开启单点登录。
5. 找几个不同角色的真实员工先试登录，确认看到的是自己原来的账号和项目。
6. 已离职但系统里还在的账号，借这次机会清理掉。

## 3. OIDC

OIDC 是首选的接入方式。授权码流程、令牌和 scope、authentik 的 OIDC 地址和常见报错见博客 [authentik 统一身份认证 · OIDC](https://cylshao.com/posts/authentik-oidc/)，本节只写本工程的配置约定。只支持“OAuth2 登录”、不支持 OIDC 的系统，也用同一个 OAuth2 provider 接入，用户信息从 userinfo 地址取。

### 3.1 建 provider 和应用

1. Applications → Providers → Create → **OAuth2/OpenID Provider**：

   | 字段 | 值 | 说明 |
   |---|---|---|
   | Name | 如 `gitlab` | |
   | Authorization flow | `default-provider-authorization-implicit-consent` | 内部系统不用每次弹出授权确认页。高危应用改成二次 MFA 的流程，见 [authentik.md 5.1 节](authentik.md#51-高危应用要求二次-mfa) |
   | Client type | Confidential | 系统有服务端，能保存 Client Secret。纯前端应用才选 Public |
   | Redirect URIs | 系统给出的回调地址，匹配方式选 Strict | 不要用正则或通配，避免令牌被跳到别的地址 |
   | Signing key | `authentik Self-signed Certificate` | 用 RS256 签名，系统通过 JWKS 地址取公钥验证。不选时用 Client Secret 做 HS256 签名，很多系统不支持 |
   | Scopes | `openid`、`email`、`profile`，需要角色映射时再加 `entitlements` | 见 3.2 节 |
   | Subject mode | 默认的 Based on the User's hashed ID | `sub` 不随用户名、邮箱变化，改名后系统里的账号关联不断 |

   保存后记下 Client ID 和 Client Secret。Grant types 至少要有 `authorization_code`：在后台或用 API 创建时默认就有，用脚本直接写数据库时默认为空，登录会报 “The request is otherwise malformed”。

2. Applications → Applications → Create：Slug 填系统名（如 `gitlab`，会出现在地址里），Provider 选上一步建的，Launch URL 填系统首页，员工就能在 authentik 的应用门户里点开它。
3. 在应用的 Policy / Group / User Bindings 里**绑定允许登录的组**，见 3.3 节。

Issuer 是 `https://auth.company.com/application/o/<slug>/`，系统支持自动发现时只填 Issuer 就够了。授权地址由员工的浏览器访问，换令牌、取公钥等由系统的服务端访问，所以**系统的服务器也要能解析和访问 `auth.company.com`**。authentik 只在内网时，要确认系统所在的网络能连到内网 ALB。

### 3.2 scope 和员工信息

| scope | 系统拿到的字段 | 来源 |
|---|---|---|
| `openid` | `sub`：账号的唯一标识 | 按 authentik 用户生成的哈希 |
| `profile` | `preferred_username`、`nickname`：用户名，如 `david`；`name`：姓名，如“张伟-David”；`groups`：所在的组 | 同步脚本按飞书写入，见 [feishu.md 5.5 节](feishu.md#55-同步的字段) |
| `email` | `email` | 飞书的邮箱 |
| `entitlements` | `entitlements`：员工在这个应用上的权益，如 `Grafana Admins` | 应用的 Entitlements 设置，见 [authentik.md 第 5 节](authentik.md#5-权限设计) |

关联账号用 `sub`，不要用用户名或邮箱：用户名可能被管理员改（如把 `lily2` 改成 `lily.wang`），邮箱可能在飞书里被修改。`sub` 按 authentik 账号生成，账号被删除后重建，`sub` 就变了，系统会把他当成另一个人，所以员工的 authentik 账号只禁用、不删除。系统只能按用户名识别人时（如 Jenkins），接入前先把要改的用户名改好。

手机号、工号等员工资料默认不会给任何系统。确实需要时，在 Customization → Property Mappings 里建一个 Scope Mapping，只挂到需要的 provider 上，并在接入评审里说明用途。

`groups` 会带上员工所在的全部组，包括所有部门组。系统用它做角色映射时，只认自己关心的组名；更推荐用 `entitlements`，只暴露这个应用相关的权益。

### 3.3 授权：谁能登录

在应用的 Bindings 里绑定部门组（如 `飞书/技术中心`）或角色组，只有组里的人能登录这个系统。不在组里的人扫码后会在 authentik 里被拒绝，不会进到系统里。

**应用没有任何绑定时，所有人都能登录**，见 [authentik.md 第 5 节](authentik.md#5-权限设计)。要开放给全员时，不要留空，绑定一个判断 `feishu_managed` 的表达式策略，见 [feishu.md 5.4 节](feishu.md#54-脚本的行为)。

系统内部的角色（管理员、普通用户）尽量在系统里按 `entitlements` 自动映射，见 3.6 节 Grafana 的例子。系统不支持映射时，在系统里手工授予。

### 3.4 退出登录

员工在系统里退出，只是退出了这个系统，authentik 的会话还在，再点登录会直接进去，不用再扫码。这是单点登录的正常行为。

需要退出时一并退出 authentik 的系统（如公用电脑上使用的系统）：

1. provider 的 Invalidation flow 改成 `default-invalidation-flow`。默认的 `default-provider-invalidation-flow` 不含注销步骤，只结束这个应用，跳到退出登录地址后 authentik 的会话还在。
2. 把系统的“退出后跳转地址”设为 `https://auth.company.com/application/o/<slug>/end-session/`。
3. 要退出后回到系统的登录页，在 provider 的 Redirect URIs 里加一条类型为 Logout、Strict 匹配的地址，系统跳转时带上 `post_logout_redirect_uri` 和 `id_token_hint`（登录时拿到的 ID Token）。authentik 要求两个参数一起出现，只带 `post_logout_redirect_uri` 会报 `invalid_request`。

### 3.5 每个 OIDC 系统的验收

- [ ] Redirect URI 是 Strict 匹配，Signing key 已选
- [ ] 应用绑定了组，用不在组里的账号验证会被拒绝
- [ ] 已有账号按第 2 节比对过，用几个老员工验证登录后看到的是自己原来的账号
- [ ] 管理员、普通用户、无权限用户各验证一次角色
- [ ] 系统的本地注册已关闭，本地密码登录已关闭或只留应急管理员
- [ ] 离职处理已确认：authentik 禁用账号后，系统里已有的会话和个人 token 怎么失效

### 3.6 GitLab、Jenkins、Grafana

- **GitLab**：接入步骤、头像、离职处理和常见问题见 [gitlab.md](gitlab.md)。
- **Jenkins**：按用户名识别账号，账号迁移、权限映射、应急账号和 API token 的处理见 [jenkins.md](jenkins.md)。
- **Grafana**：通过 Generic OAuth 接入，配置方法见 [authentik 的 Grafana 集成文档](https://integrations.goauthentik.io/monitoring/grafana/)。在此基础上，scope 加上 `entitlements`，在 authentik 的 Grafana 应用里建权益 `Grafana Admins`、`Grafana Editors`，分别绑定 `ops-admin`、`dev-lead`，角色映射：

  ```ini
  role_attribute_path = contains(entitlements[*], 'Grafana Admins') && 'Admin' || contains(entitlements[*], 'Grafana Editors') && 'Editor' || 'Viewer'
  ```

  Grafana 适合全员开放查看，应用绑定判断 `feishu_managed` 的表达式策略，见 3.3 节。

其他系统在 [authentik 集成文档目录](https://integrations.goauthentik.io/) 里找对应的配置说明，按本节的约定调整。

## 4. 自研系统

按 3.1 节建 provider 和应用，和通用配置不同的地方：

| 项 | 值 |
|---|---|
| Provider 名称、应用 Slug | 系统标识，如 `order-admin` |
| Redirect URIs | 两条都选 Strict：回调地址，类型 Authorization；退出后跳转地址，类型 Logout |
| Authorization flow | `default-provider-authorization-implicit-consent`。系统要用账号密码登录时不能换成二次 MFA 的流程 |
| Invalidation flow | `default-invalidation-flow`，见 3.4 节 |
| Scopes | `openid`、`email`、`profile`、`entitlements` |
| 应用绑定 | 允许登录的组 |
| Entitlements | 按系统的角色建权益，如 `Order Admin Admins`，绑定对应的组 |

另建一个开发用的应用（如 `order-admin-dev`），配置相同，Redirect URIs 填开发者给的本机地址（如 `http://localhost:8080/auth/callback`），应用只绑定这个系统的开发人员。开发用应用的 Client Secret 会放在开发者的电脑上，不要和正式应用共用。

系统要用账号密码登录时：

- 需要用密码的员工加入 `密码登录例外` 组，并让他设置动态码，见 [authentik.md 第 3 节](authentik.md#3-安装后的安全加固)。联调时给开发者一个设置好 TOTP 的手工测试账号。
- 确认系统服务器的网段在 `AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS` 里，authentik 才会采用系统转来的 `X-Forwarded-For`。

上线前按 3.5 节验收。

## 5. 代理认证：Prometheus、Alertmanager

用 Proxy provider 的 “Forward auth (single application)” 模式，在 Nginx 里加 `auth_request`，参考 [Forward auth 文档](https://docs.goauthentik.io/add-secure-apps/providers/proxy/forward_auth/)。记得把应用加到内置 outpost（embedded outpost）上。

## 6. JumpServer（LDAP）

**先评估用 OIDC 接入 JumpServer。** JumpServer 的认证设置里也支持 OIDC（以你们使用的版本为准）。用 OIDC 接入有三个好处：员工直接扫码登录，不用再单独设置 authentik 密码；可以用 [authentik.md 5.1 节](authentik.md#51-高危应用要求二次-mfa) 的二次 MFA；不需要部署和维护 LDAP outpost。只有 JumpServer 的版本或功能确实不支持时，才按下面的步骤走 LDAP。

1. 在 authentik 里准备 LDAP：
   - 建一个专用的 bind flow，只包含识别、密码、登录三个步骤，不带飞书按钮。
   - 建 LDAP provider：Base DN 填 `dc=ldap,dc=company,dc=com`，选择 `ldap.company.com` 的证书，bind flow 选上一步建的流程。
   - 建 LDAP 应用，绑定 `飞书/技术中心`。
   - 建服务账号 `svc-jumpserver`，授予它对该 LDAP provider 的 “Search full LDAP directory” 权限。
2. 部署 outpost，只对内网开放：
   - **生产环境**：在 Applications → Outposts 里新建 LDAP 类型的 outpost，Integration 选 “Local Kubernetes Cluster”，由 authentik 在集群里创建，前面挂内网 NLB。配置见 [authentik-deploy.md 第 6 节](authentik-deploy.md#6-启用-ldap-outpost)。
   - **本地验证**：Integration 留空（手动部署），保存后点 View Deployment Info 获取 token，再用 `outposts/ldap/` 启动：

     ```bash
     cp outposts/ldap/.env.example outposts/ldap/.env    # 填写 AUTHENTIK_HOST、AUTHENTIK_TOKEN、LDAP_BIND_IP
     make ldap-up
     ```

3. 在 Outposts 页面确认状态为健康，再用 `ldapsearch` 验证：

   ```bash
   ldapsearch -H ldaps://ldap.company.com:636 -x \
     -D 'cn=svc-jumpserver,ou=users,dc=ldap,dc=company,dc=com' -W \
     -b 'ou=users,dc=ldap,dc=company,dc=com' '(cn=david)'
   ```

4. JumpServer 系统设置 → 认证设置 → LDAP：

   | 项 | 值 |
   |---|---|
   | 地址 | `ldaps://ldap.company.com:636` |
   | 绑定 DN | `cn=svc-jumpserver,ou=users,dc=ldap,dc=company,dc=com` |
   | 用户 OU | `ou=users,dc=ldap,dc=company,dc=com` |
   | 用户过滤器 | `(cn=%(user)s)` |
   | 属性映射 | `{"username": "cn", "name": "name", "email": "mail"}` |

5. JumpServer 里开启强制 MFA，按用户组授权服务器。
6. 保留 JumpServer 的本地管理员账号作为应急通道，见 [authentik.md 7.3 节](authentik.md#73-应急通道)。

注意：

- authentik 的 LDAP 只支持认证和查询，不能通过 LDAP 改密码或写入数据。
- 同步脚本建的账号默认没有密码。要用 JumpServer 的人，先扫码登录 authentik，在个人设置里设置密码。
- LDAP 走自己的 bind flow，不受 `password-login-exceptions.yaml` 的限制：员工设置了密码就能通过 LDAP 认证，不需要加入例外组，也不会被要求 MFA。谁能用 LDAP 由 LDAP 应用绑定的组控制，MFA 靠 JumpServer 自己开启。
- 只用 `ldaps://`（636），不要用明文的 389 端口。

## 7. VPN 和网络设备（RADIUS）

**先确认 VPN 是否支持 SAML 或 OIDC。** 很多企业级 VPN 支持浏览器跳转式的单点登录，员工可以直接扫码，也能用 [authentik.md 5.1 节](authentik.md#51-高危应用要求二次-mfa) 的二次 MFA。RADIUS 只能用密码认证，动态码要拼在密码后面输入，员工体验差，适合作为网络设备和不支持单点登录的 VPN 的兜底方案。

- 建 RADIUS provider，配置共享密钥和允许的客户端网段。
- 和 LDAP 一样，新建 RADIUS 类型的 outpost，Integration 选 “Local Kubernetes Cluster”，由 authentik 在集群里创建。Service 改成内网 NLB 的方法见 [authentik-deploy.md 第 6 节](authentik-deploy.md#6-启用-ldap-outpost)，端口是 UDP 1812，NLB 要建 UDP 监听，访问控制只放行 VPN 和网络设备的地址。
- 密码认证只支持 PAP，先确认设备支持。
- 需要动态码时，员工输入 `密码;123456` 格式（密码后加分号和 6 位动态码）。
- VPN 和网络设备都要保留本地应急账号，见 [authentik.md 7.3 节](authentik.md#73-应急通道)。
- 参考 [RADIUS 文档](https://docs.goauthentik.io/add-secure-apps/providers/radius/)。

## 8. outpost 的通用要求

- 生产环境的 outpost 都由 authentik 在集群里创建，版本随 authentik 自动升级，不要手工部署。
- outpost 的 token 等同于一个高权限凭据。由 authentik 创建的 outpost，token 自动存在 namespace 的 Secret 里，不要导出。
- 创建后在管理后台的 Outposts 页面确认状态为健康。
