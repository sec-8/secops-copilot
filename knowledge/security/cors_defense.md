# CORS 跨域配置错误防御

> 覆盖变体：`Access-Control-Allow-Origin: *` 通配符、`null` Origin 反射、凭证携带过度放行。  
> 防御同源：Origin 严格白名单 + 禁止反射 `null` + 限制携带凭证。

## 漏洞成因

CORS（跨域资源共享）跨域配置错误的根本成因在于：服务端在响应跨域请求时，错误地配置了 `Access-Control-Allow-Origin` 响应头，允许任意域访问敏感资源，或将请求中的 `Origin` 头原样反射回响应中。攻击者可利用恶意网站构造跨域请求，通过浏览器的 CORS 机制合法地读取目标接口返回的敏感数据（如用户信息、Token、内部数据）。其核心问题是服务端未对跨域访问的来源做严格校验，错误地信任了任意域或畸形来源（如 `null`）。

## CORS 跨域配置检测方法

检测目标在于发现服务端对跨域请求的来源校验缺失或配置宽松：

- **通配符检测**：向目标接口发送跨域请求（`Origin: https://attacker.com`），检查响应头是否包含 `Access-Control-Allow-Origin: *` 或原样反射了 `attacker.com`。若同时存在 `Access-Control-Allow-Credentials: true`，则风险升级为凭证可被携带窃取。
- **null Origin 反射检测**：发送 `Origin: null` 的跨域请求（如通过 `data:` URI 或 sandbox iframe 触发），观察服务端是否将 `null` 原样反射至 `Access-Control-Allow-Origin`。若反射且允许携带凭证，则攻击者可构造沙箱环境绕过白名单。
- **自动化检测**：扫描器通过变异 `Origin` 头（随机域、`null`、空白、畸形域），比对响应头中的 CORS 策略，识别配置宽松的接口。

关键原则：所有返回敏感数据（JSON、XML）且携带会话凭证的 API 接口均为检测重点；静态文件或公开资源的 CORS 配置宽松风险较低。

## 防御机制：Origin 校验 + 凭证管控 + 配置收紧（三层）

### 第一层：Origin 严格白名单校验（核心防线）

服务端收到跨域请求时，必须对 `Origin` 头进行严格校验，仅允许预定义的可信域：

- **精确白名单匹配**：在服务端维护固定的域名白名单（如 `https://app.example.com`、`https://admin.example.com`），将请求中的 `Origin` 与白名单逐一比对。匹配时必须精确到协议（HTTP/HTTPS）和端口，不可使用通配符（如 `*.example.com` 应谨慎评估风险，优先使用完整域名列表）。
- **反射策略**：仅当 `Origin` 命中白名单时，在响应头中返回 `Access-Control-Allow-Origin: {请求的 Origin}`；否则不返回该头，或返回 `null`（但应避免反射 `null`）。
- **拒绝无 Origin 请求**：对于未携带 `Origin` 头的请求，若接口涉及敏感数据，应默认拒绝跨域访问，视为非浏览器发起的直接请求（如 curl），由服务端其他认证机制保障。

**实现关键**：白名单配置不在 Web 服务器层（Nginx/Apache）写死，而应在应用层统一管理，便于动态更新和审计。

### 第二层：禁止反射 `null` 与畸形来源

`null` Origin 是一种特殊的来源标识，可通过多种方式伪造（如 `data:` URI、沙箱 iframe、本地文件），服务端不应将 `null` 视为合法来源：

- **拒绝 `null`**：当 `Origin: null` 时，服务端直接返回 403 或忽略 CORS 头，绝不将其写入 `Access-Control-Allow-Origin`。
- **拒绝畸形来源**：对非法的 `Origin` 格式（如包含换行、控制字符、非法协议）一律拒绝，避免绕过解析逻辑。
- **拒绝通配符与反射混合**：禁止同时使用 `*` 和 `Access-Control-Allow-Credentials: true`，前者使凭证被任意域携带，后者允许凭证跨域发送，组合后可直接导致会话泄露。

### 第三层：限制携带凭证（保护敏感会话）

`Access-Control-Allow-Credentials: true` 允许跨域请求携带目标域的 Cookie、客户端证书等凭证，必须严格管控：

- **最小化凭证授权**：仅当业务确实需要跨域携带凭证时（如跨域 SSO 登录、跨域 API 调用），才设置为 `true`，且必须配合精确的 `Access-Control-Allow-Origin`（非 `*`）。
- **默认禁止凭证跨域**：绝大多数公开 API 不应开启凭证携带，请求头中不加 `withCredentials: true` 即可。若接口不涉及用户会话，应明确设置 `Access-Control-Allow-Credentials: false` 或不返回该头。
- **预检请求管控**：对复杂跨域请求（含自定义头、非简单方法），浏览器会先发送 `OPTIONS` 预检。服务端应确保预检响应的 CORS 策略与正式请求一致，避免预检宽松而正式收紧导致策略冲突被绕过。

## 纵深防御（辅助层）

- **禁用 JSONP**：JSONP 通过 `<script>` 标签绕过了 CORS 同源策略，易产生劫持风险。若业务已迁移至 CORS，应完全禁用 JSONP 接口。
- **区分请求类型**：对 GET/HEAD 等简单请求（无自定义头）和 POST（`application/x-www-form-urlencoded`）进行策略收紧，仅允许必要的跨域读取，禁止跨域写操作通过 CORS 放行。
- **响应内容二次校验**：对于敏感接口，在返回数据前额外校验请求头中的 `Referer` 或 `X-Requested-With`（自定义头），作为 CORS 白名单之外的辅助手段。注意 `Referer` 不可全量依赖，且部分浏览器或隐私模式可能删除该头。
- **CORS 配置审计**：定期通过自动化工具扫描所有 API 的 CORS 响应头，识别 `*`、`null` 反射、凭证组合等高危配置，纳入 CI/CD 门禁。
- **最小暴露原则**：仅对需要跨域访问的接口（如前端 SPA 调用的 API）配置 CORS，内部管理接口或微服务间调用不应开放 CORS 头。

## 安全金律

- **Origin 白名单必须精确**：不允许 `*` 通配符，白名单内的域名必须完整（含协议和端口），不可存在任何模糊匹配。
- **永远不反射 `null`**：`Origin: null` 是绕过信号，直接拒绝，绝不将其作为合法来源写入响应头。
- **凭证携带与通配符互斥**：`Access-Control-Allow-Credentials: true` 时，`Access-Control-Allow-Origin` 不能为 `*`，必须为具体域。
- **CORS 不是认证机制**：CORS 仅控制浏览器端的跨域访问策略，服务端仍需独立的身份认证和权限校验，不因跨域而豁免。
- **默认拒绝一切跨域**：除非业务显式需要，所有接口默认不返回 CORS 头，避免配置遗留给攻击者留下可乘之机。