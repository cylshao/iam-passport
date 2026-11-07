# GitLab 接入

本文说明 GitLab 怎么通过 OIDC 接入 authentik：接入前的准备、两边的配置、头像、权限、离职处理、本地验证和常见问题。

OIDC 的原理见博客 [authentik 统一身份认证 · OIDC](https://cylshao.com/posts/authentik-oidc/)，在 authentik 里的通用配置（provider、scope、授权、退出、验收）见 [integrations.md 第 3 节](integrations.md#3-oidc)，本文只写 GitLab 特有的部分。文中的 `auth.company.com`、`gitlab.company.com` 等都是占位地址。以下按 GitLab Omnibus（`gitlab.rb`）说明，Helm 部署的 GitLab 配置项相同、写法不同。

接入后，员工点“公司账号（飞书扫码）”登录 GitLab，第一次登录时 GitLab 自动建号或关联已有账号。

## 1. 接入前准备

GitLab 对账号的处理方式决定了要先做哪些准备：

- GitLab 用 OIDC 的 `sub` 记住“这个 GitLab 账号对应 authentik 的哪个人”，之后改用户名、改邮箱都不影响。
- 第一次登录时，如果开启了按邮箱自动关联（第 3 节的 `omniauth_auto_link_user`），GitLab 找邮箱相同的已有账号并关联上，员工继续用原来的账号、项目和权限；找不到时新建账号，用户名取 authentik 的用户名。
- 没有开启自动关联，或者邮箱对不上时，会新建一个重复账号，比如老账号是 `david.zhang`，新建一个 `david`，项目和权限都留在老账号上。

所以接入前：

1. **确认邮箱齐全**：在 authentik 里查出没有邮箱的同步账号，请飞书管理员补上。没有邮箱时 GitLab 会填一个临时邮箱（如 `temp-email-for-oauth-david@gitlab.localhost`）并让员工自己补填，补填的邮箱没有经过校验，可能填错或冒用别人的邮箱。
2. **比对邮箱**：导出 GitLab 的全部账号（Admin → Users，或 API `GET /api/v4/users`），和 authentik 的邮箱比对。GitLab 里的邮箱和飞书不一致的，先在 GitLab 里改成飞书的邮箱。
3. **清理离职账号**：GitLab 里有、飞书里已经没有的人，借这次机会封禁（Block）。
4. **确认网络**：GitLab 服务器能访问 `auth.company.com`，见 [integrations.md 3.1 节](integrations.md#31-建-provider-和应用)。

## 2. authentik 配置

按 [integrations.md 3.1 节](integrations.md#31-建-provider-和应用) 建 provider 和应用：

| 项 | 值 |
|---|---|
| Provider 名称、应用 Slug | `gitlab` |
| Redirect URI（Strict） | `https://gitlab.company.com/users/auth/openid_connect/callback` |
| Scopes | `openid`、`email`、`profile` |
| Subject mode | 默认的 Based on the User's hashed ID，第 4 节的头像脚本靠它对应账号 |
| 应用绑定 | 允许使用 GitLab 的部门组，如 `飞书/技术中心` |
| Launch URL | `https://gitlab.company.com/` |

## 3. GitLab 配置

在 `/etc/gitlab/gitlab.rb` 里加上，然后执行 `gitlab-ctl reconfigure`：

```ruby
gitlab_rails['omniauth_enabled'] = true
# 允许第一次扫码登录时自动建号；不加时没有 GitLab 账号的人无法登录
gitlab_rails['omniauth_allow_single_sign_on'] = ['openid_connect']
# 自动建的账号直接可用，不用管理员审批：能不能登录已经由 authentik 的应用绑定控制
gitlab_rails['omniauth_block_auto_created_users'] = false
# 按邮箱关联已有账号，老员工继续用原来的账号
gitlab_rails['omniauth_auto_link_user'] = ['openid_connect']
# 每次登录时用 authentik 的姓名和邮箱覆盖 GitLab 里的，员工不能在 GitLab 里自己改
gitlab_rails['omniauth_sync_profile_from_provider'] = ['openid_connect']
gitlab_rails['omniauth_sync_profile_attributes'] = ['name', 'email']

gitlab_rails['omniauth_providers'] = [
  {
    name: 'openid_connect',
    label: '公司账号（飞书扫码）',
    args: {
      name: 'openid_connect',
      scope: ['openid', 'profile', 'email'],
      response_type: 'code',
      issuer: 'https://auth.company.com/application/o/gitlab/',
      discovery: true,
      uid_field: 'sub',
      pkce: true,
      client_options: {
        identifier: '<Client ID>',
        secret: '<Client Secret>',
        redirect_uri: 'https://gitlab.company.com/users/auth/openid_connect/callback'
      }
    }
  }
]
```

注意：

- `uid_field` 用 `sub`。authentik 官方的 GitLab 示例用的是 `preferred_username`，那样管理员在 authentik 里改用户名后，GitLab 会把这个人当成新用户。
- `omniauth_auto_link_user` 按邮箱关联，前提是 authentik 里的邮箱可信：邮箱只来自飞书，并且已经在 authentik 里关闭了员工自己修改邮箱（[authentik.md 第 3 节](authentik.md#3-安装后的安全加固)）。否则员工把邮箱改成管理员的邮箱，就能登录到管理员的 GitLab 账号上。
- `akadmin`、应急管理员这类手工账号不要使用员工本人的邮箱，用 `akadmin@company.com` 这样的专用地址。否则手工账号一旦登录过 GitLab，员工扫码时会按邮箱关联到手工账号建出的 GitLab 账号上，或者因为邮箱重复被拒绝，见第 9 节。
- Client Secret 不要明文写在 `gitlab.rb` 里提交到配置仓库，用公司的密钥管理方式注入。

验证扫码登录正常后，可以再加上下面这行，打开 GitLab 时直接跳到 authentik，不再显示 GitLab 的登录页：

```ruby
gitlab_rails['omniauth_auto_sign_in_with_provider'] = 'openid_connect'
```

管理员需要显示 GitLab 自己的登录页时，访问 `https://gitlab.company.com/users/sign_in?auto_sign_in=false`。

## 4. 头像

GitLab 扫码登录时只同步姓名、邮箱、地点、组织、职位，不读 OIDC 的头像字段 `picture`。默认显示的是 Gravatar 的随机图案，而且 GitLab 会把员工邮箱的哈希发给外部的 `gravatar.com`。处理方法：

1. 在 Admin → Settings → General → Account and limit 里取消 Gravatar enabled，不再访问外部服务。
2. 用 `org-sync/gitlab/avatars.py` 把飞书头像上传到 GitLab。

脚本从 authentik 读出每个在职员工的 `avatar` 属性（飞书同步脚本写入的头像地址），按 `sub` 找到对应的 GitLab 账号，下载头像后通过 GitLab API 上传。上传过的头像地址记在 GitLab 用户的自定义属性 `feishu_avatar` 里，飞书头像不变就不重复上传，所以可以定期运行（如每天一次）：

```bash
cd org-sync
export AK_URL=https://auth.company.com AK_TOKEN=...          # authentik 的 API token，只需要读取用户
export GITLAB_URL=https://gitlab.company.com GITLAB_TOKEN=...  # GitLab 管理员的个人访问令牌，scope 选 api
python3 -m gitlab.avatars --dry-run   # 只打印将要上传的账号
python3 -m gitlab.avatars
```

最后一行是汇总，如 `完成：扫码账号 120，匹配到 authentik 118，上传 3，失败 0`。

- “匹配到 authentik”为 0 时，检查 GitLab 的 provider 的 Subject mode 是不是默认的 hashed ID。
- 只上传 jpeg、png、gif、webp，超过 GitLab 200 KB 限制的计入失败。
- 员工自己在 GitLab 里换了头像的，飞书头像没变时脚本不会覆盖；飞书头像变了会再次上传。

## 5. 管理员和权限

- **谁能登录 GitLab**：由 authentik 的应用绑定决定，见 [integrations.md 3.3 节](integrations.md#33-授权谁能登录)。
- **项目和群组的权限**：继续在 GitLab 里按群组和项目成员管理，和接入前一样。
- **GitLab 管理员**：在 GitLab 里手工授予（Admin → Users → 编辑 → Access level 选 Administrator）。按 OIDC 的组自动授予管理员（`admin_groups`）、限定登录的组（`required_groups`）是 GitLab 付费版的功能，社区版没有，以所用版本的 GitLab 文档为准。社区版靠 authentik 的应用绑定控制登录就够了。

## 6. 收紧 GitLab 自己的登录

扫码登录验证通过、老员工都确认能登录自己的账号后，在 GitLab 的 Admin → Settings → General 里：

1. **Sign-up restrictions**：取消 Sign-up enabled，关闭自助注册。
2. **Sign-in restrictions**：取消 “Allow password authentication for the web interface”，关闭网页上的密码登录。关闭后 root 也不能用密码登录网页，所以先确认至少有一名 GitLab 管理员能扫码登录。应急时在 GitLab 服务器上恢复：

   ```bash
   gitlab-rails runner 'ApplicationSetting.current.update!(password_authentication_enabled_for_web: true)'
   ```

3. **Git over HTTPS**：扫码登录的员工在 GitLab 里没有密码，用 HTTPS 拉代码时要用个人访问令牌（Personal Access Token）或改用 SSH。提前通知员工。确认没人再用密码后，取消 “Allow password authentication for Git over HTTP(S)”。
4. **Account and limit**：Session duration 设成和 authentik 会话相近的时长（如 8 到 12 小时），离职后已登录的会话能尽快失效。

## 7. 离职处理

authentik 禁用账号后，员工不能再扫码登录 GitLab，但 GitLab 社区版不会收到任何通知：

- 已经登录的 GitLab 会话，在会话过期前还能用。
- 个人访问令牌和 SSH 密钥还能继续拉取、推送代码。

所以离职流程里要包含：在 GitLab 里封禁（Block）这个账号，封禁后会话、令牌和 SSH 密钥都会失效。封禁不会删除账号，项目和提交记录都保留。不要删除账号，删除会连带处理他名下的项目和记录。

人工操作容易漏。员工多时，可以定期用脚本比对：authentik 里已禁用的同步账号，在 GitLab 里按 `sub` 对应的账号没有封禁的，就调用 GitLab API 封禁。

## 8. 本地验证

`apps/gitlab/` 是一个本地验证用的 GitLab CE（<http://localhost:8929>），已经按 OIDC 接好本地 authentik，只用于验证配置和账号行为：

1. 在本地 authentik 里按第 2 节建 provider 和应用，Redirect URI 改成 `http://localhost:8929/users/auth/openid_connect/callback`。
2. `cp apps/gitlab/.env.example apps/gitlab/.env`，填入 Client ID、Client Secret 和 root 初始密码，然后 `make gitlab-up`。镜像约 1.5 GB，第一次启动要几分钟。
3. 用无痕窗口打开 GitLab，点“公司账号（飞书扫码）”。

和生产配置的区别：GitLab 容器里的 `localhost` 是它自己，所以 `apps/gitlab/compose.yml` 里浏览器跳转用 `localhost:9000`，GitLab 服务端调用 authentik 用 `host.docker.internal:9000`。authentik 按请求的地址生成 Issuer，两个地址不一致会导致自动发现校验失败，所以本地关闭了 discovery，逐个写明地址。生产环境 GitLab 和浏览器访问的是同一个 `auth.company.com`，按第 3 节开启 discovery 即可。

验证头像脚本时，`GITLAB_URL` 用 `http://localhost:8929`，`GITLAB_TOKEN` 用 root 在 User Settings → Access tokens 里建的令牌（scope 选 api）。

本地验证的结果：

| 验证项 | 结果 |
|---|---|
| 飞书扫码登录 GitLab | 成功，GitLab 自动建号，用户名和姓名与 authentik 一致（如 `david`、“张伟-David”） |
| 部门授权 | 应用只绑定一个部门组（如 `飞书/技术中心`）时，不在组里的 `akadmin` 被拒绝 |
| 邮箱 | authentik 里没有邮箱时，GitLab 填了临时邮箱 `temp-email-for-oauth-david@gitlab.localhost`，并要求员工自己补填 |
| 账号关联 | GitLab 记录的是 authentik 的 `sub`，和用户名无关；改用户名不影响关联 |
| 头像 | 脚本按 `sub` 匹配到扫码账号并上传飞书头像；再次运行时头像没变，不重复上传 |

## 9. 常见问题

**扫码后 GitLab 提示 “Signing in using your OpenID Connect account without a pre-existing GitLab account is not allowed”**

没有配置 `omniauth_allow_single_sign_on`，GitLab 不允许自动建号。见第 3 节。

**扫码后 GitLab 提示账号等待管理员审批**

`omniauth_block_auto_created_users` 没有设为 `false`。已经建出来的账号，在 Admin → Users 的 Pending approval 里批准。

**老员工扫码后进了一个新的空账号**

GitLab 没有按邮箱关联上老账号：没开 `omniauth_auto_link_user`，或者 GitLab 里的邮箱和飞书的不一致。处理方法：管理员封禁新建的空账号，把老账号的邮箱改成飞书的邮箱，在老账号的 Profile → Account → Service sign-in 里关联 OpenID Connect（或删除空账号后让员工重新扫码）。

**扫码后 GitLab 提示 “An account already exists with the email address for your 公司账号（飞书扫码） account”**

GitLab 里已经有一个邮箱相同、但没有关联这次登录身份（`sub`）的账号，又没有开启 `omniauth_auto_link_user`，所以要求先用那个账号的密码登录再关联。先在 GitLab 的 Admin → Users 里按邮箱查出这个账号是谁的：

- **是这个员工的老账号**：正常情况，开启第 3 节的 `omniauth_auto_link_user`，或者让员工用老账号的密码登录后，在 Profile → Account → Service sign-in 里关联。
- **是 `akadmin` 等手工账号登录 GitLab 时建出来的**：手工账号和员工用了同一个邮箱。先把 authentik 里手工账号的邮箱改成专用地址，再在 GitLab 里删除或改掉这个账号的邮箱。这时不要开自动关联，否则员工会登录到这个账号上。
- **是这个员工以前的登录建的，但 authentik 账号被删除重建过**：`sub` 按 authentik 账号生成，账号重建后 `sub` 变了，GitLab 认为是另一个人。在 GitLab 里删除旧账号的 OpenID Connect 关联（Admin → Users → 编辑 → Identities），再让员工重新扫码，按邮箱关联。生产环境不要删除重建员工的 authentik 账号，离职只禁用。

**authentik 报 “Redirect URI Error”**

provider 里的 Redirect URI 和 GitLab 发出的不一致，常见的是 `http` 和 `https`、域名或末尾路径不同。GitLab 的回调地址固定是 `<GitLab 地址>/users/auth/openid_connect/callback`。

**GitLab 报 Issuer 不匹配，或者取不到公钥**

GitLab 服务端访问 authentik 用的地址和 Issuer 不一致，或者 GitLab 服务器连不上 authentik。在 GitLab 服务器上执行 `curl https://auth.company.com/application/o/gitlab/.well-known/openid-configuration`，返回里的 `issuer` 要和 `gitlab.rb` 里的完全一致，包括末尾的 `/`。

**在 authentik 的应用门户里点 GitLab，打开的是 GitLab 的 404 页面**

应用的 Launch URL 填成了 `<GitLab 地址>/users/auth/openid_connect`。为了防止 CSRF，GitLab 只接受从自己登录页上的按钮发起的 POST 请求，浏览器直接打开这个地址是 GET，所以返回 404。Launch URL 改成 GitLab 首页，见第 2 节。需要点开后直接跳到 authentik、不停在 GitLab 登录页时，加上第 3 节的 `omniauth_auto_sign_in_with_provider`。

**扫码后 authentik 提示没有权限访问这个应用**

员工不在应用绑定的组里，见 [integrations.md 3.3 节](integrations.md#33-授权谁能登录)。这是预期行为。

## 10. 上线检查清单

- [ ] GitLab 账号已和 authentik 比对，邮箱不一致的已改正，离职账号已封禁
- [ ] authentik 应用只绑定了允许使用 GitLab 的组
- [ ] `uid_field` 是 `sub`，已开启按邮箱自动关联，authentik 已关闭员工修改邮箱
- [ ] 已用三类账号验证：老员工登录到原来的账号、新员工自动建号、不在绑定组里的人被拒绝
- [ ] 已关闭自助注册和网页密码登录，至少一名 GitLab 管理员能扫码登录，应急恢复命令已验证
- [ ] 已通知员工 HTTPS 拉代码改用个人访问令牌或 SSH
- [ ] 离职流程里已加入“在 GitLab 里封禁账号”
- [ ] 已关闭 Gravatar；需要飞书头像时，头像上传脚本已定期运行
- [ ] 已按 [integrations.md 3.5 节](integrations.md#35-每个-oidc-系统的验收) 完成通用验收
