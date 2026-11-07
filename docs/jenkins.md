# Jenkins 接入

本文说明 Jenkins 怎么通过 OIDC 接入 authentik：接入前的准备、两边的配置、权限、应急账号、API token、离职处理、本地验证和常见问题。

OIDC 的原理见博客 [authentik 统一身份认证 · OIDC](https://cylshao.com/posts/authentik-oidc/)，在 authentik 里的通用配置（provider、scope、授权、退出、验收）见 [integrations.md 第 3 节](integrations.md#3-oidc)，本文只写 Jenkins 特有的部分。文中的 `auth.company.com`、`jenkins.company.com` 等都是占位地址。以下用 OpenId Connect Authentication 插件（`oic-auth`）加 Matrix Authorization Strategy 插件（`matrix-auth`），按 Configuration as Code（JCasC）的写法说明；在 Manage Jenkins → Security 界面里配置时，各项名称和 JCasC 的字段一一对应。

接入后，员工打开 Jenkins 时直接跳到 authentik 扫码，回来后按用户名对应到 Jenkins 的账号，权限按 authentik 里的权益（entitlements）映射。

## 1. 接入前准备

Jenkins 对账号的处理方式和 GitLab 不同，决定了要先做哪些准备：

- **Jenkins 只认用户名**。用户名就是 Jenkins 的用户 ID，授权矩阵里按用户授权的条目、构建记录里的“由谁触发”、用户的 API token 都记在这个 ID 下。接入后用户名取 authentik 的用户名（如 `david`）。
- **切换前后用户名一致，就是同一个账号**。原来的 API token、构建记录、按用户授权的条目都继续有效，不需要任何迁移操作（见第 8 节的验证结果）。
- **用户名对不上时，会成为一个新用户**，原来的权限和 token 都留在旧 ID 下。Jenkins 不能给用户改 ID。
- **不按邮箱关联，也不用 `sub`**。`sub` 是一串哈希，用作用户名时授权矩阵和构建记录里都是看不懂的字符串。代价是：authentik 里改了用户名，Jenkins 会把这个人当成新用户，见第 9 节。

所以接入前：

1. **导出 Jenkins 的账号**：在 Manage Jenkins → Script Console 里执行，列出每个用户的 ID、邮箱和 API token 数量：

   ```groovy
   hudson.model.User.getAll().each { u ->
     def mail = u.getProperty(hudson.tasks.Mailer.UserProperty)?.address
     def tokens = u.getProperty(jenkins.security.ApiTokenProperty)?.tokenList?.size() ?: 0
     println "${u.id}\t${mail}\t${tokens}"
   }
   ```

2. **和 authentik 比对用户名**。authentik 的用户名是员工的英文名（重名时带数字，如 `lily2`），见 [feishu.md 5.4 节](feishu.md#54-脚本的行为)。Jenkins 原来用邮箱前缀（如 `david.zhang`）或 LDAP 账号的，基本都对不上。对不上的人，按下面两种情况处理：
   - 只是普通用户、权限都来自组：不用处理。切换后他用新用户名登录，权限按第 4 节的权益获得，需要的话重新生成 API token。
   - 授权矩阵里有按用户授权的条目，或者 API token 被脚本使用：把授权条目改成新用户名，通知本人切换后重新生成 token，并换掉脚本里的 token。
3. **找出按用户授权的条目**。全局的在 Manage Jenkins → Security 的授权矩阵里看；文件夹和任务上的在 `$JENKINS_HOME` 里查 `USER:` 开头的条目：

   ```bash
   grep -rl --include=config.xml 'USER:' $JENKINS_HOME/config.xml $JENKINS_HOME/jobs
   ```

   能改成按权益授权的尽量改掉，见第 4 节。
4. **先改用户名，再接 Jenkins**。员工的 authentik 用户名要调整的（如把 `lily2` 改成 `lily.wang`），在 Jenkins 接入前改好。
5. **清理离职账号**：Jenkins 里有、飞书里已经没有的人，借这次机会删除（People → 用户 → Delete）。删除用户会删除他的 API token，构建记录保留。
6. **确认网络**：Jenkins 服务器能访问 `auth.company.com`，见 [integrations.md 3.1 节](integrations.md#31-建-provider-和应用)。

## 2. authentik 配置

按 [integrations.md 3.1 节](integrations.md#31-建-provider-和应用) 建 provider 和应用：

| 项 | 值 |
|---|---|
| Provider 名称、应用 Slug | `jenkins` |
| Redirect URIs（都选 Strict） | `https://jenkins.company.com/securityRealm/finishLogin`，类型 Authorization；`https://jenkins.company.com/`，类型 Logout |
| Authorization flow | Jenkins 能直接发布生产，建议用要求二次 MFA 的流程，见 [authentik.md 5.1 节](authentik.md#51-高危应用要求二次-mfa) |
| Invalidation flow | `default-invalidation-flow`：退出 Jenkins 时一并退出 authentik，见 [integrations.md 3.4 节](integrations.md#34-退出登录) |
| Scopes | `openid`、`email`、`profile`、`entitlements`、`offline_access` |
| Grant types | `authorization_code`、`refresh_token` |
| Access token validity | 默认的 `hours=1` 即可。离职和权限变更最晚在这个时长加 60 秒后生效，见第 6、7 节 |
| 应用绑定 | 允许使用 Jenkins 的部门组，如 `飞书/技术中心` |
| Entitlements | `Jenkins Admins`，绑定运维负责人的组；需要按团队授权时再加，见第 4 节 |
| Launch URL | `https://jenkins.company.com/` |

`offline_access` 不能省。不带它时 authentik 不发 refresh token，员工的 API token 会在他最后一次网页登录约一小时后失效，见第 6 节。

## 3. Jenkins 配置

安装插件 OpenId Connect Authentication（`oic-auth`）和 Matrix Authorization Strategy（`matrix-auth`），用 JCasC 管理配置时再装 Configuration as Code。本仓库验证过的版本见 `apps/jenkins/plugins.txt`。

JCasC 配置：

```yaml
jenkins:
  securityRealm:
    oic:
      clientId: "${OIDC_CLIENT_ID}"
      clientSecret: "${OIDC_CLIENT_SECRET}"
      serverConfiguration:
        wellKnown:
          wellKnownOpenIDConfigurationUrl: "https://auth.company.com/application/o/jenkins/.well-known/openid-configuration"
          scopesOverride: "openid profile email entitlements offline_access"
      userNameField: "preferred_username"
      fullNameFieldName: "name"
      emailFieldName: "email"
      groupsFieldName: "entitlements"
      logoutFromOpenidProvider: true
      postLogoutRedirectUrl: "https://jenkins.company.com/"
      properties:
        - "pkce"
        - escapeHatch:
            username: "breakglass"
            secret: "${ESCAPE_HATCH_PASSWORD_HASH}"
            group: "breakglass"
  authorizationStrategy:
    globalMatrix:
      entries:
        - group:
            name: "Jenkins Admins"
            permissions:
              - "Overall/Administer"
        - group:
            name: "breakglass"
            permissions:
              - "Overall/Administer"
        - group:
            name: "authenticated"
            permissions:
              - "Overall/Read"
              - "Job/Read"
unclassified:
  location:
    url: "https://jenkins.company.com/"
```

注意：

- `scopesOverride` 写明要请求的 scope。不写时插件请求自动发现里列出的全部 scope，以后 provider 上多挂的 scope mapping 也会被带上。
- `userNameField` 用 `preferred_username`，原因见第 1 节。
- `groupsFieldName` 用 `entitlements`，只带这个应用的权益。用 `groups` 会把员工所在的全部部门组都带进 Jenkins。
- `unclassified.location.url`（界面上是 Manage Jenkins → System → Jenkins URL）决定了 Jenkins 发给 authentik 的回调地址，要和第 2 节的 Redirect URI 完全一致。Jenkins 在反向代理后面时，这里要写 `https` 的外部地址。
- `allowTokenAccessWithoutOicSession` 保持默认的 `false`，原因见第 6 节。
- Client Secret 和应急账号的密码不要明文写进配置仓库。JCasC 的 `${...}` 从环境变量或 Kubernetes Secret 挂载的文件里读取。应急账号的 `secret` 可以直接填 bcrypt 哈希，插件只保存哈希，见第 5 节。

切换时，先确认应急账号能登录，再保存新的认证方式；用无痕窗口验证扫码登录，原来的管理员窗口先不要关。

## 4. 权限

- **谁能登录 Jenkins**：由 authentik 的应用绑定决定，见 [integrations.md 3.3 节](integrations.md#33-授权谁能登录)。不在绑定组里的人停在 authentik 的拒绝页面，不会进到 Jenkins。
- **Jenkins 管理员**：authentik 里有 `Jenkins Admins` 权益的人。权益名就是 Jenkins 授权矩阵里的组名，大小写和空格都要一致。
- **普通用户**：上面的配置给所有登录的人（Jenkins 内置的 `authenticated` 组）只读权限。谁能构建、配置哪些任务，按需要加权益，如在 authentik 里建 `Jenkins Order Team` 权益并绑定订单组，再在对应文件夹的配置里勾选 Enable project-based security，给这个组授权。
- **不要按人授权**。按用户授权的条目要在 Jenkins 里逐个维护，员工换岗、离职时容易漏掉。

权限变更在员工下次登录，或 Jenkins 下一次刷新他的令牌时生效，最晚是 access token 有效期加 60 秒。员工可以访问 `https://jenkins.company.com/whoAmI/` 确认自己拿到了哪些组。

## 5. 应急账号

authentik 出故障时，员工都登录不了 Jenkins，也就不能通过流水线修复 authentik，见 [authentik-deploy.md 第 8 节](authentik-deploy.md#8-应急发布通道)。第 3 节配置里的 `escapeHatch` 是 Jenkins 的应急账号，用本地密码登录，不经过 authentik：

- **网页登录**：打开 `https://jenkins.company.com/login`，用 `breakglass` 和密码登录，拿到 `breakglass` 组的管理员权限。正常访问 Jenkins 时会直接跳到 authentik，只有直接打开 `/login` 才会看到这个页面。
- **脚本或命令行**：用 HTTP Basic 认证，如 `curl -u breakglass:<密码> https://jenkins.company.com/whoAmI/api/json`。
- **保存密码**：生成随机密码（`openssl rand -base64 24`），和 authentik 的应急管理员密码放在一起保管。配置里只填哈希：

  ```bash
  htpasswd -bnBC 10 "" '<密码>' | tr -d ':\n'
  ```

- **控制风险**：这个账号绕过了 authentik 和 MFA，所以密码要足够长，Jenkins 只开放在内网；每季度和应急发布通道一起演练一次，知道密码的人离职时更换密码。

## 6. API token 和脚本

脚本调用 Jenkins API 用的是员工自己的 API token（User → Security → API Token）。插件对 token 请求的处理方式（第 8 节已验证）：

| token 的主人 | 行为 |
|---|---|
| 用 OIDC 登录过 Jenkins 的员工 | Jenkins 保存了他的 OIDC 令牌。access token 过期后，Jenkins 用 refresh token 向 authentik 续期，续期成功 token 照常可用；续期失败（账号被禁用、没有 `offline_access`、refresh token 过期）时请求被重定向到登录页，脚本拿到 302 |
| 从没用 OIDC 登录过的用户 | 没有可检查的令牌，token 一直可用，不受 authentik 影响。切换前在本地用户库里建的 token、管理员用脚本建的账号都属于这种 |

所以：

- authentik 的 scope 要带 `offline_access`。不带时没有 refresh token，token 在员工最后一次网页登录约一小时后失效。
- refresh token 默认 30 天有效，每次续期都会换一个新的。Jenkins 30 天没收到这个人的请求，refresh token 就过期了，员工用浏览器登录一次就能恢复。
- `allowTokenAccessWithoutOicSession` 保持 `false`。设为 `true` 后 token 请求跳过检查，员工在 authentik 被禁用后 token 仍然可用。
- 流水线之间、GitLab 到 Jenkins 的调用，不要用员工的 token。确实需要固定身份时，用管理员在 Script Console 里建一个服务账号（如 `svc-release`）并生成 token。这个账号不受 authentik 控制，要登记用途和负责人，定期轮换 token。服务账号的名字不能和 authentik 里能登录 Jenkins 的用户重名，否则那个人扫码登录后就成了这个服务账号。

## 7. 离职处理

authentik 禁用账号时会删除这个人的全部 refresh token。access token 过期后（最晚 access token 有效期加 60 秒），Jenkins 续期失败，这个人的网页会话和 API token 都会失效，不需要在 Jenkins 里额外操作。

以下几种情况不在这个范围内，离职流程里要单独处理：

- **从没用 OIDC 登录过 Jenkins 的人**：他在切换前生成的 API token 一直可用。在 Jenkins 里删除这个用户（People → 用户 → Delete），token 随之失效，构建记录保留。
- **服务账号**：负责人离职时，把服务账号交接给别人，并轮换 token。
- **应急账号**：知道密码的人离职时更换密码，见第 5 节。
- **Jenkins 的 SSH 服务**：默认关闭。开启了的话，用户配置的 SSH 公钥不走 HTTP 请求，不经过上面的检查，要删除这个用户。

员工多时，可以定期用 Script Console 列出所有有 API token、但没有 OIDC 令牌的用户，逐个确认。

## 8. 本地验证

`apps/jenkins/` 是一个本地验证用的 Jenkins（<http://localhost:8931>），配置文件 `apps/jenkins/casc.yaml` 已经按 OIDC 接好本地 authentik，只用于验证配置和账号行为：

1. 按 [README](../README.md#本地快速体验) 在本机启动 authentik（<http://localhost:9000>）。
2. 在本地 authentik 里按第 2 节建 provider 和应用，Redirect URIs 填 `http://localhost:8931/securityRealm/finishLogin`（Authorization）和 `http://localhost:8931/`（Logout）；建权益 `Jenkins Admins`，绑定你自己所在的组。
3. `cp apps/jenkins/.env.example apps/jenkins/.env`，填入 Client ID、Client Secret 和应急账号密码，然后在仓库根目录 `make jenkins-up`。第一次启动要构建镜像、安装插件，需要一两分钟。
4. 用无痕窗口打开 <http://localhost:8931>，会直接跳到 authentik。应急账号从 <http://localhost:8931/login> 登录。

和生产配置的区别：authentik 按请求的地址生成 Issuer，Jenkins 的服务端和浏览器必须用同一个地址访问 authentik。容器里的 `localhost` 是容器自己，所以 `apps/jenkins/compose.yml` 和示例系统一样加了一个转发容器，把容器里的 `localhost:9000` 转发到宿主机。这样 `casc.yaml` 能和生产一样用自动发现，只有地址不同。

本地的验证结果。验证用的是设置了 TOTP 的本地密码账号，没有用飞书扫码；跳转方式下两者只是在 authentik 里的认证方式不同，Jenkins 这边的流程一样：

| 验证项 | 结果 |
|---|---|
| 跳转登录 | 打开 Jenkins 自动跳到 authentik，认证后回到 Jenkins；用户名、姓名、邮箱取自 authentik |
| 部门授权 | 不在应用绑定组里的账号停在 authentik 的拒绝页面，没有进到 Jenkins |
| 角色 | 有 `Jenkins Admins` 权益的账号能进 Manage Jenkins，`/whoAmI` 显示组 `Jenkins Admins`；没有权益的账号只有只读权限 |
| 老账号迁移 | 先用 Jenkins 自己的用户库建了 `jk-dev`，生成 API token 并触发一次构建；切换到 OIDC 后，同名账号登录进的是原来的用户，原 token 可用，构建记录里的触发人还是 `jk-dev` |
| API token 续期 | access token 过期后，Jenkins 用 refresh token 自动续期，token 照常可用 |
| 禁用账号 | 在 authentik 里禁用后，access token 有效期加 60 秒内，这个人的网页会话和 API token 都被重定向到登录页 |
| 没有用 OIDC 登录过的用户的 token | 不受 authentik 影响，一直可用 |
| 应急账号 | `/login` 页面和 HTTP Basic 认证都能用 `breakglass` 登录并拿到管理员权限，密码错误被拒绝；配置里填 `htpasswd` 生成的 bcrypt 哈希也能登录 |
| 退出 | 退出 Jenkins 时一并结束 authentik 的会话，然后回到 Jenkins，再次访问要重新认证 |

## 9. 常见问题

**authentik 报 “Redirect URI Error”**

Jenkins 发出的回调地址和 provider 里的不一致。回调地址是 `<Jenkins URL>/securityRealm/finishLogin`，Jenkins URL 取自 Manage Jenkins → System → Jenkins URL（JCasC 的 `unclassified.location.url`）。常见的是反向代理后面的 Jenkins URL 还是 `http` 或内网地址。

**员工登录后权限不对，看不到 Manage Jenkins**

访问 `/whoAmI/` 看 Authorities 里有没有 `Jenkins Admins`。没有时检查：authentik 里权益是否绑定了他所在的组；provider 是否挂了 `entitlements` 的 scope mapping；`scopesOverride` 里有没有 `entitlements`；权益名和授权矩阵里的组名是否完全一致。

Jenkins 日志里的 `Could not identify groups in entitlements=[]` 是没有任何权益的员工登录时的提示，不影响登录。

**脚本调用 API 突然收到 302，跳到 `securityRealm/commenceLogin`**

Jenkins 续期这个人的 OIDC 令牌失败了，见第 6 节：账号在 authentik 被禁用；scope 没有 `offline_access`；或者 30 天没有使用，refresh token 过期了。后两种让员工用浏览器登录一次即可。

**员工在 authentik 改了用户名后，Jenkins 报 “User name was not the same after refresh request”，或者登录后成了一个新用户**

Jenkins 按用户名识别人，见第 1 节。已经登录的会话在下一次续期时被拒绝；再次登录时，Jenkins 按新用户名建了一个新用户，原来的 token 和按用户授权的条目都留在旧用户名下。把 authentik 的用户名改回去，或者在 Jenkins 里把授权条目改成新用户名，让员工重新生成 token。

**老员工登录后进了一个新的空账号**

authentik 的用户名和 Jenkins 原来的用户 ID 不一致，见第 1 节第 2 步。

**切换后管理员也登录不了 Jenkins**

用应急账号从 `/login` 登录，修正配置。应急账号也没有配置时：用 JCasC 管理的改配置文件后重启；在界面里配置的，按 Jenkins 的 [Disable security](https://www.jenkins.io/doc/book/security/access-control/disable/) 文档临时关闭安全设置。

**扫码后 authentik 提示没有权限访问这个应用**

员工不在应用绑定的组里，见 [integrations.md 3.3 节](integrations.md#33-授权谁能登录)。这是预期行为。

## 10. 上线检查清单

- [ ] Jenkins 账号已和 authentik 比对，用户名对不上的已确定处理方式，按用户授权的条目已改名或改成按权益授权，离职账号已删除
- [ ] authentik 应用只绑定了允许使用 Jenkins 的组，`Jenkins Admins` 只绑定了运维负责人的组
- [ ] scope 带了 `entitlements` 和 `offline_access`，Grant types 有 `refresh_token`
- [ ] `userNameField` 是 `preferred_username`，`groupsFieldName` 是 `entitlements`，`allowTokenAccessWithoutOicSession` 是 `false`
- [ ] 应急账号已配置并演练过，配置里只有密码的哈希，密码已和 authentik 的应急管理员密码一起保管
- [ ] 已用三类账号验证：老员工登录到原来的账号、管理员能进 Manage Jenkins、不在绑定组里的人被拒绝
- [ ] 用员工的 API token 验证过：过了 access token 有效期仍然可用；在 authentik 禁用后失效
- [ ] 已通知员工：切换后用户名变了的人要重新生成 API token，并换掉脚本里的旧 token
- [ ] 离职流程里已加入“删除从没用 OIDC 登录过的用户”，服务账号已登记负责人
- [ ] 已按 [integrations.md 3.5 节](integrations.md#35-每个-oidc-系统的验收) 完成通用验收
